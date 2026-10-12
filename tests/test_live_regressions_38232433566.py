"""Regressions from the live `samples` run 38232433566 (tests/fixtures/live_samples_38232433566.json).

* Squadron 42 passed `sources.reliability` because ONE core claim (the Q2 2027 window on Steam) was
  official, while the trailer and wishlist core claims were reported by Gamingbolt alone.
* Its source title "... Receives New Gameplay Trailer ..." was cut to "... Receives New Game ...".
* The God of War 3 port article was tagged "Claude Opus 5.5", a supporting detail of the story.
"""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import NOW, FakeOpenAI, build, make_settings

from newsbot.enrich import central_tags
from newsbot.facts import parse_fact_sheet, verify_fact_sheet
from newsbot.models import Article, Claim, FeedItem, SourceDoc, Story
from newsbot.quality import decide, source_checks
from newsbot.research import ResearchResult, steam_store_doc
from newsbot.textutil import clean_source_title

FIXTURE = Path(__file__).parent / "fixtures" / "live_samples_38232433566.json"
LIVE = {r["headline"].split()[0]: r for r in json.loads(FIXTURE.read_text(encoding="utf-8"))["records"]}
SQUADRON = LIVE["Squadron"]
GOW = LIVE["God"]


def live_docs(record: dict, gamingbolt_authority: str = "medium") -> list[SourceDoc]:
    """The run's research documents (Gamingbolt is `authority: medium` in sources.yaml)."""
    docs = []
    for row in record["research"]["documents"]:
        authority = gamingbolt_authority if row["outlet"] == "Gamingbolt" else (
            "high" if row["type"] != "publication" else "medium")
        docs.append(SourceDoc(row["id"], row["url"], row["outlet"], row["type"], authority, row["outlet"],
                              "x" * max(row["chars"], 300), origin_group=row["group"]))
    return docs


def live_sheet(record: dict, docs: list[SourceDoc]):
    """The run's verified fact sheet: each claim with the verification and sources it actually had."""
    outlet_to_id = {d.outlet: d.doc_id for d in docs}
    claims = []
    for row in record["facts"]["claims"]:
        sources = [outlet_to_id[o] for o in row["sources"] if o in outlet_to_id]
        claims.append(Claim(id=row["id"], text_en=row["text"], category=row["category"], subject="",
                            value=row["value"], status=row["status"], confidence="high", importance=row["importance"],
                            verified_sources=sources, verification=row["verification"],
                            independent_groups=len({d.origin_group for d in docs if d.doc_id in sources})))
    sheet = parse_fact_sheet({"claims": []})
    sheet.claims = claims
    return sheet


def live_story(record: dict, primary: str, display: str) -> Story:
    items = [FeedItem(f"i{n}", row["source"], "publication", "medium", "primary", row["url"], row["url"],
                      row["title"], "", NOW - timedelta(hours=3)) for n, row in enumerate(record["items"])]
    return Story("live", items, primary_entity=primary, primary_display=display, event_type=record["event_type"])


def reliability(checks):
    return next(c for c in checks if c.name == "sources.reliability")


# ---------------------------------------------------------------------------
# sources.reliability is decided per core claim
# ---------------------------------------------------------------------------
def test_live_squadron42_fact_sheet_is_the_regression():
    claims = {c["id"]: c for c in SQUADRON["facts"]["claims"]}
    assert [cid for cid, c in claims.items() if c["importance"] == "core"] == ["C1", "C2", "C3"]
    assert (claims["C1"]["verification"], claims["C1"]["sources"]) == ("single_source", ["Gamingbolt"])
    assert (claims["C2"]["verification"], claims["C2"]["sources"]) == ("single_source", ["Gamingbolt"])
    assert claims["C3"]["verification"] == "official" and "Steam" in claims["C3"]["sources"]
    live_check = next(c for c in SQUADRON["gate"]["checks"] if c["name"] == "sources.reliability")
    assert live_check["passed"] and live_check["detail"] == "core news confirmed by an official source"


def test_official_steam_window_does_not_validate_the_other_core_claims(repo):
    docs = live_docs(SQUADRON)
    sheet = live_sheet(SQUADRON, docs)
    checks = source_checks(live_story(SQUADRON, "squadron 42", "Squadron 42"), sheet, ResearchResult(docs=docs),
                           make_settings(repo))
    check = reliability(checks)
    assert not check.passed
    assert "C1 only Gamingbolt" in check.detail and "C2 only Gamingbolt" in check.detail
    assert "C3" not in check.detail                               # the Steam-confirmed window is fine on its own
    assert not decide(checks).passed


@pytest.mark.parametrize("how", ["high_authority_outlet", "lower_threshold", "editorial_override"])
def test_authoritative_single_source_can_still_pass(repo, how):
    docs = live_docs(SQUADRON, "high" if how == "high_authority_outlet" else "medium")
    settings = make_settings(repo)
    if how == "lower_threshold":
        settings.single_source_min_authority = "medium"
    if how == "editorial_override":
        settings.editorial = {"overrides": {"allow_single_source_entities": ["Squadron 42"]}}
    checks = source_checks(live_story(SQUADRON, "squadron 42", "Squadron 42"), live_sheet(SQUADRON, docs),
                           ResearchResult(docs=docs), settings)
    check = reliability(checks)
    assert check.passed, check.detail
    assert "C3 official (Steam)" in check.detail


def test_single_source_policy_switch_still_applies(repo):
    docs = live_docs(SQUADRON, "high")
    settings = make_settings(repo, allow_single_source=False)
    checks = source_checks(live_story(SQUADRON, "squadron 42", "Squadron 42"), live_sheet(SQUADRON, docs),
                           ResearchResult(docs=docs), settings)
    assert not reliability(checks).passed


def test_corroborated_or_official_core_claims_pass(repo):
    docs = live_docs(GOW)                                         # DSOGaming + Wccftech, both medium
    checks = source_checks(live_story(GOW, "god of war 3 remastered", "God of War 3 Remastered"),
                           live_sheet(GOW, docs), ResearchResult(docs=docs), make_settings(repo))
    check = reliability(checks)
    assert check.passed and "C1 corroborated by 2 independent origins" in check.detail


# ---------------------------------------------------------------------------
# A Steam listing confirms only what it states
# ---------------------------------------------------------------------------
STEAM_APPDETAILS = json.dumps({"4045720": {"success": True, "data": {
    "name": "Squadron 42", "developers": ["Cloud Imperium Games Ltd", "Cloud Imperium Games Texas LLC"],
    "publishers": ["Roberts Space Industries International Ltd", "Roberts Space Industries LLC"],
    "release_date": {"coming_soon": True, "date": "Q2 2027"}, "platforms": {"windows": True},
    "short_description": "Squadron 42 is a single-player cinematic adventure set in the Star Citizen universe."}}})
GAMINGBOLT_TEXT = ("Cloud Imperium Games has released a new gameplay trailer for Squadron 42. The game is now "
                   "available to wishlist on Steam, where its release date is listed as Q2 2027. The Steam page "
                   "lists Windows 11 and 16 GB of RAM in the minimum requirements, and 150 GB of storage.")


def _make_doc(url, outlet, source_type, authority, title, text, status, published="", links=None, image_url="",
              image_rights="none"):
    return SourceDoc("", url, outlet, source_type, authority, title, text, published, status)


def verified_squadron(steam_quotes: dict[str, str]):
    steam = steam_store_doc(4045720, STEAM_APPDETAILS, _make_doc)
    steam.doc_id, steam.origin_group = "S2", 2
    gb = SourceDoc("S1", "https://gamingbolt.com/squadron-42", "Gamingbolt", "publication", "medium",
                   "Squadron 42 Receives New Gameplay Trailer", GAMINGBOLT_TEXT, origin_group=1)

    def claim(cid, text, category, value, gb_quote, importance="core"):
        support = [{"source_id": "S1", "quote": gb_quote}] if gb_quote else []
        if cid in steam_quotes:
            support.append({"source_id": "S2", "quote": steam_quotes[cid]})
        return {"id": cid, "text_en": text, "category": category, "subject": "Squadron 42", "value": value,
                "status": "reported", "confidence": "high", "importance": importance, "support": support}

    raw = {"headline_en": "Squadron 42 trailer", "event_type": "trailer", "kind": "news",
           "entities": {"games": ["Squadron 42"], "companies": ["Cloud Imperium Games"], "platforms": ["Windows"],
                        "products": []},
           "claims": [
               claim("C1", "Cloud Imperium Games released a new trailer for Squadron 42.", "other", "",
                     "Cloud Imperium Games has released a new gameplay trailer for Squadron 42"),
               claim("C2", "Squadron 42 became available to wishlist on Steam.", "availability", "Steam",
                     "The game is now available to wishlist on Steam"),
               claim("C3", "Squadron 42 has a Steam release date of Q2 2027.", "release_window", "Q2 2027",
                     "its release date is listed as Q2 2027"),
               claim("C4", "The Steam listing lists Windows as a platform for Squadron 42.", "platform", "Windows",
                     "", "supporting"),
           ],
           "contradictions": [], "sources": [], "newsworthiness": {"is_news": True, "significance": "medium"},
           "open_questions": [], "injection_detected": False}
    docs = [gb, steam]
    return verify_fact_sheet(parse_fact_sheet(raw), docs), docs


def test_steam_listing_confirms_only_what_it_states(repo):
    sheet, docs = verified_squadron({
        # What the extraction model could plausibly attach: the listing's opening line names the game only.
        "C1": "Official Steam store listing for Squadron 42",
        "C2": "Official Steam store listing for Squadron 42",
        "C3": "Steam release date: Q2 2027",
        "C4": "Platforms listed on Steam: Windows.",
    })
    c = {cl.id: cl for cl in sheet.claims}
    assert (c["C1"].verification, c["C1"].verified_sources) == ("single_source", ["S1"])
    assert (c["C2"].verification, c["C2"].verified_sources) == ("single_source", ["S1"])
    assert any("does not state this claim" in p for p in c["C1"].problems + c["C2"].problems)
    assert c["C3"].verification == "official" and c["C3"].status == "confirmed"
    assert c["C4"].verification == "official"
    checks = source_checks(Story("s", [FeedItem("i", "Gamingbolt", "publication", "medium", "primary", "u", "u",
                                                SQUADRON["headline"], "", NOW - timedelta(hours=2))],
                                 primary_entity="squadron 42", primary_display="Squadron 42"),
                           sheet, ResearchResult(docs=docs), make_settings(repo))
    assert not reliability(checks).passed


def test_steam_developer_line_does_not_confirm_a_trailer():
    sheet, _ = verified_squadron({"C1": "Developer: Cloud Imperium Games Ltd, Cloud Imperium Games Texas LLC."})
    assert sheet.claim("C1").verification == "single_source"


# ---------------------------------------------------------------------------
# Source titles and tags
# ---------------------------------------------------------------------------
def test_gameplay_trailer_title_is_not_cut():
    live = SQUADRON["article_html"]
    assert "Receives New Game , Now Available" in live                     # the live defect
    title = SQUADRON["items"][0]["title"]
    assert clean_source_title(title, "Gamingbolt") == title
    assert clean_source_title("Forza Horizon 6Add to WishlistAdd to Wishlist", "PlayStation Store") == \
        "Forza Horizon 6"
    for headline in ("Diablo 4 Login Issues Fixed", "Watch the Trailer for Ghost of Yotei Legends",
                     "Free DLC Available to Download Now", "Halo Infinite Gameplay Trailer Shows Co-op"):
        assert clean_source_title(headline) == headline


def _article(record: dict) -> Article:
    art = record["article"]
    return Article(title_fa=art["title"], meta_title_fa="", meta_description_fa="", focus_keyword_fa="", slug_en="",
                   lead={"text": art["plain_text"].split("\n")[0], "claim_ids": []}, sections=[], uncertainties=[],
                   entity_tags=list(art["entity_tags"]), content_type="gaming", coverage={})


def test_tangential_entity_is_not_tagged():
    assert GOW["tags"] == [788] and "Claude Opus 5.5: existing tag 788" in GOW["tag_notes"]   # the live defect
    docs = live_docs(GOW)
    kept, notes = central_tags(GOW["article"]["entity_tags"], live_story(GOW, "god of war 3 remastered",
                                                                         "God of War 3 Remastered"),
                               live_sheet(GOW, docs), _article(GOW))
    assert kept == ["God of War 3 Remastered"]
    assert notes == ["Claude Opus 5.5: not central to the story (no tag)"]
    docs = live_docs(SQUADRON)
    kept, _ = central_tags(SQUADRON["article"]["entity_tags"], live_story(SQUADRON, "squadron 42", "Squadron 42"),
                           live_sheet(SQUADRON, docs), _article(SQUADRON))
    assert kept == ["Squadron 42", "Cloud Imperium Games", "Steam"]          # the developer is in the lead


def test_published_post_skips_tangential_existing_tag(repo):
    from conftest import IRONVALE_ARTICLE
    article = dict(IRONVALE_ARTICLE, entity_tags=["Ironvale Chronicles", "Claude Opus 5.5", "Northlight Forge"])
    pipeline, _, fake_wp, _ = build(repo, "publish", openai_client=FakeOpenAI({"article": lambda kw: article}))
    fake_wp.tags[788] = "Claude Opus 5.5"
    report = pipeline.run()
    post = next(iter(fake_wp.posts.values()))
    assert 788 not in post["tags"] and 1 in post["tags"]            # Northlight Forge (in the lead) stays
    story = next(s for s in report.stories if s["decision"] == "published")
    assert "Claude Opus 5.5: not central to the story (no tag)" in story["tag_notes"]


def test_listing_lines_still_confirm_what_they_state():
    from newsbot.facts import quote_supports_claim

    def claim(text, category, value=""):
        return Claim("C9", text, category, "Squadron 42", value, "reported", "high", "supporting")
    names = {"squadron", "42", "cloud", "imperium", "game", "windows"}
    assert quote_supports_claim(claim("Squadron 42 launches on Windows.", "platform", "Windows"),
                                "Platforms listed on Steam: Windows.", names)
    assert quote_supports_claim(claim("The game is published by Roberts Space Industries.", "business",
                                      "Roberts Space Industries"),
                                "Publisher: Roberts Space Industries International Ltd.", names)
    assert not quote_supports_claim(claim("Squadron 42 is available now on Steam.", "availability"),
                                    "Official Steam store listing for Squadron 42 (Steam app 4045720).", names)
    assert not quote_supports_claim(claim("Squadron 42 became available to wishlist on Steam.", "availability",
                                          "Steam"),
                                    "Official Steam store listing for Squadron 42 (Steam app 4045720).", names)
