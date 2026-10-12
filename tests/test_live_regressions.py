"""Regression tests built from the live `samples` run (GitHub Actions run 37851559690).

tests/fixtures/live_samples_37851559690.json holds the four SAMPLE_JSON records exactly as the
run printed them. Three of them (Forza Horizon 6, Game Pass, Monster Hunter Stories 3) had a
model fact-check verdict of "revise" but still passed the old gate; the GTA 6 radio story was
clustered with an unrelated merch article and an unrelated PC Gamer column.
"""

import copy
import json
from pathlib import Path

import pytest
from conftest import IRONVALE_ARTICLE, FakeOpenAI, build

from newsbot.factcheck import model_check_results, revisable, revision_feedback
from newsbot.models import CheckResult
from newsbot.quality import decide

FIXTURE = Path(__file__).parent / "fixtures" / "live_samples_37851559690.json"
LIVE = json.loads(FIXTURE.read_text(encoding="utf-8"))["records"]
BY_GAME = {
    "gta6": LIVE[0],
    "forza": LIVE[1],
    "gamepass": LIVE[2],
    "mhs3": LIVE[3],
}
OLD_MODEL_CHECKS = ("factcheck.", "language.fluent")


def recorded_gate_checks(record: dict) -> list[CheckResult]:
    """All checks the live run recorded, minus the old model-check rows (re-derived below)."""
    return [CheckResult(c["name"], c["passed"], c["blocking"], c["detail"]) for c in record["gate"]["checks"]
            if not c["name"].startswith(OLD_MODEL_CHECKS)]


def test_fixture_is_the_live_run():
    assert [r["headline"] for r in LIVE] == [
        "GTA 6 Reveals 6 Radio Stations, Plus Podcasts You Can Listen To On Foot",
        "Forza Horizon 6 Is Finally Coming to PS5 in January Alongside First Expansion",
        "Coming to XBOX Game Pass: Battlefield 6, Valor Mortis, Forever Ago, and More",
        "Monster Hunter Stories 3: Twisted Reflection Heading to XBOX on PC November 18",
    ]
    # The defect being fixed: the old gate let "revise" verdicts through.
    assert [r["model_factcheck"]["verdict"] for r in LIVE] == ["pass", "revise", "revise", "revise"]
    assert all(r["gate"]["passed"] for r in LIVE)


@pytest.mark.parametrize("name", ["forza", "gamepass", "mhs3"])
def test_revise_verdict_samples_can_never_pass_the_gate(name):
    record = BY_GAME[name]
    model = model_check_results(record["model_factcheck"])
    decision = decide(recorded_gate_checks(record) + model)
    assert not decision.passed
    assert any(r.startswith("factcheck.model_verdict: verdict=revise") for r in decision.reasons)
    # The finding itself (excerpt + explanation) reaches the writer as revision feedback.
    failed = [c for c in model if c.blocking and not c.passed]
    assert revisable(failed)
    feedback = " ".join(revision_feedback(failed))
    for issue in record["model_factcheck"]["issues"]:
        assert issue["excerpt_fa"][:40] in feedback


def test_medium_issue_blocks_even_with_pass_verdict():
    data = copy.deepcopy(BY_GAME["forza"]["model_factcheck"])
    data["verdict"] = "pass"
    checks = model_check_results(data)
    names = {c.name for c in checks if not c.passed}
    assert names == {"factcheck.model_issues"}


def test_clean_pass_verdict_passes_model_criteria():
    record = BY_GAME["gta6"]
    assert all(c.passed for c in model_check_results(record["model_factcheck"]))


@pytest.mark.parametrize("data", [{}, {"verdict": "maybe"}, {"verdict": None, "issues": []}, None])
def test_missing_or_unknown_verdict_fails_closed(data):
    checks = model_check_results(data)
    assert not decide(checks).passed


def test_reject_verdict_is_final():
    data = copy.deepcopy(BY_GAME["mhs3"]["model_factcheck"])
    data["verdict"] = "reject"
    failed = [c for c in model_check_results(data) if not c.passed]
    assert "factcheck.model_rejected" in {c.name for c in failed}
    assert not revisable(failed)


# ---------------------------------------------------------------------------
# Pipeline: revise -> corrective revision -> both checks re-run
# ---------------------------------------------------------------------------
def _sequence(*items):
    it = iter(items)
    last = {"value": None}

    def handler(kw):
        try:
            last["value"] = next(it)
        except StopIteration:
            pass
        return last["value"]
    return handler


def test_persistent_revise_is_rejected_after_the_revision_budget(repo):
    """Forza Horizon 6's live verdict on every check: revised twice, re-checked twice, never published."""
    verdict = BY_GAME["forza"]["model_factcheck"]
    ai = FakeOpenAI({"fact_check": lambda kw: verdict})
    pipeline, web, fake_wp, _ = build(repo, "publish", openai_client=ai, max_revisions=2)
    report = pipeline.run()
    assert fake_wp.posts == {} and report.published == [] and web.writes() == []
    story = next(s for s in report.stories if "Ironvale" in s["headline"])
    assert story["decision"] == "rejected"
    assert any(r.startswith("factcheck.model_verdict: verdict=revise") for r in story["reasons"])
    calls = ai.schema_calls()
    assert calls.count("article") == 3 and calls.count("fact_check") == 3   # 1 + 2 revisions, each re-checked
    assert [h["revision"] for h in story["model_factcheck_history"]] == [0, 1, 2]
    second_compose = [c for c in ai.calls if c["text"]["format"]["name"] == "article"][1]
    assert verdict["issues"][0]["excerpt_fa"][:30] in second_compose["input"]


def test_revise_then_pass_publishes_only_the_rechecked_revision(repo):
    revise = BY_GAME["gamepass"]["model_factcheck"]
    passing = {"issues": [], "language": {"fluent": True, "translationese": False, "problems": []},
               "verdict": "pass"}
    revised = copy.deepcopy(IRONVALE_ARTICLE)
    revised["title_fa"] = "Ironvale Chronicles روز ۱۹ مارس ۲۰۲۷ منتشر می‌شود"
    ai = FakeOpenAI({"fact_check": _sequence(revise, passing),
                     "article": _sequence(IRONVALE_ARTICLE, revised)})
    pipeline, _, fake_wp, _ = build(repo, "publish", openai_client=ai, max_revisions=2)
    report = pipeline.run()
    assert len(report.published) == 1
    post = next(iter(fake_wp.posts.values()))
    assert post["title"] == revised["title_fa"]                  # the text that passed the second check
    assert ai.schema_calls().count("fact_check") == 2


def test_revise_with_zero_revision_budget_is_rejected(repo):
    ai = FakeOpenAI({"fact_check": lambda kw: BY_GAME["mhs3"]["model_factcheck"]})
    pipeline, _, fake_wp, _ = build(repo, "publish", openai_client=ai, max_revisions=0)
    report = pipeline.run()
    assert fake_wp.posts == {} and report.published == []
    assert ai.schema_calls().count("article") == 1


def test_editorial_overrides_cannot_bypass_the_model_verdict(repo):
    editorial = {"overrides": {"allow_single_source_entities": ["Ironvale Chronicles"],
                               "allow_rumor_entities": ["Ironvale Chronicles"],
                               "priority_entities": ["Ironvale Chronicles"],
                               "force_include_urls": ["https://gamingbolt.example/ironvale-chronicles-release-date"]}}
    ai = FakeOpenAI({"fact_check": lambda kw: BY_GAME["forza"]["model_factcheck"]})
    pipeline, _, fake_wp, _ = build(repo, "publish", openai_client=ai, max_revisions=1, editorial=editorial)
    report = pipeline.run()
    assert fake_wp.posts == {} and report.published == []


def test_draft_and_publish_require_the_model_fact_check(repo):
    from conftest import make_settings
    for mode in ("draft", "publish"):
        problems = make_settings(repo, mode, llm_factcheck=False).validate()
        assert any("NEWSBOT_LLM_FACTCHECK" in p for p in problems)
    assert not any("NEWSBOT_LLM_FACTCHECK" in p for p in make_settings(repo, "dry-run",
                                                                       llm_factcheck=False).validate())


def test_gta6_live_cluster_is_split_by_event():
    """The live record's own four items, re-clustered: radio (2 outlets) / merch / column."""
    from datetime import timedelta

    import yaml
    from conftest import NOW, ROOT

    from newsbot.cluster import Clusterer
    from newsbot.entities import EntityExtractor, Gazetteer
    from newsbot.ingest import annotate
    from newsbot.models import FeedItem

    gazetteer = Gazetteer(yaml.safe_load((ROOT / "config" / "entities.yaml").read_text(encoding="utf-8")))
    extractor = EntityExtractor(gazetteer)
    items = []
    for n, row in enumerate(BY_GAME["gta6"]["items"]):
        item = FeedItem(f"live{n}", row["source"], "publication", "medium", "primary", row["url"], row["url"],
                        row["title"], "", NOW - timedelta(hours=8 - n))
        annotate(item, extractor)
        items.append(item)
    assert len(items) == 4 and BY_GAME["gta6"]["research"]["independent_groups"] == 3   # the live defect
    clusters = sorted(sorted(i.title for i in c) for c in Clusterer(gazetteer=gazetteer).cluster(items))
    titles = [r["title"] for r in BY_GAME["gta6"]["items"]]
    assert clusters == sorted([sorted(titles[:2]), [titles[2]], [titles[3]]])


# ---------------------------------------------------------------------------
# Presentation defects found in the live articles
# ---------------------------------------------------------------------------
def live_article(record: dict):
    from newsbot.models import Article
    art = record["article"]
    lines = art["plain_text"].split("\n")
    unc = list(art["uncertainties"])
    body = lines[: len(lines) - len(unc)] if unc and lines[-len(unc):] == unc else lines
    return Article(title_fa=art["title"], meta_title_fa=art["meta_title"], meta_description_fa=art["meta_description"],
                   focus_keyword_fa=art["focus_keyword"], slug_en=art["slug_suggestion"],
                   lead={"text": body[0], "claim_ids": ["C1"]},
                   sections=[{"heading_fa": "", "paragraphs": [{"text": t, "claim_ids": ["C1"]} for t in body[1:]]}],
                   uncertainties=unc, entity_tags=list(art["entity_tags"]), content_type="gaming", coverage={})


@pytest.mark.parametrize("name,expected", [
    ("gta6", {"style.no_meta_filler", "links.no_pointer_sentences"}),   # «اطلاعات موجود», «جزئیات پیشین»
    ("forza", {"style.no_meta_filler"}),                                 # «طبق اطلاعات تأییدشده», «اطلاعات موجود»
    ("gamepass", {"links.no_pointer_sentences"}),                        # «... پوشش پیشین ما، ... را ببینند»
    ("mhs3", {"links.no_pointer_sentences", "style.no_meta_filler"}),   # «... را هم ببینید», «اطلاعات اعلام‌شده»
])
def test_live_filler_and_see_also_sentences_are_blocking_and_fixable(name, expected):
    from newsbot.factcheck import presentation_checks
    failed = [c for c in presentation_checks(live_article(BY_GAME[name]), []) if c.blocking and not c.passed]
    assert {c.name for c in failed} == expected
    assert revisable(failed) and len(revision_feedback(failed)) == len(expected)


@pytest.mark.parametrize("name,dropped", [
    # Each of these is already said in the live body (often twice); the list repeated it again.
    ("gta6", ["نام همه شش ایستگاه رادیویی اعلام نشده است.",
              "عنوان پادکست‌ها و نام میزبان‌های آن‌ها مشخص نیست."]),
    ("forza", ["محتوای نخستین بسته‌الحاقی، قیمت آن و امکان خرید جداگانه‌اش مشخص نیست.",
               "قیمت‌های گزارش‌شده برای نسخه‌های PS5 هنوز تأیید رسمی نشده‌اند."]),
    ("mhs3", ["هنوز مشخص نیست نسخه XBOX on PC در فروشگاه‌های رایانه‌ای دیگری هم عرضه خواهد شد یا نه."]),
    ("gamepass", []),
])
def test_live_uncertainties_already_in_the_body_are_not_repeated(name, dropped):
    from newsbot.compose import distinct_uncertainties
    article = live_article(BY_GAME[name])
    kept = distinct_uncertainties(article.uncertainties, [p["text"] for p in article.paragraphs()])
    assert [u for u in article.uncertainties if u not in kept] == dropped


def test_live_forza_render_has_no_redundant_uncertainty_section(repo):
    from conftest import make_settings

    from newsbot.compose import render_article
    from newsbot.facts import parse_fact_sheet
    from newsbot.models import Story
    article = live_article(BY_GAME["forza"])
    out = render_article(article, Story("f00d", []), parse_fact_sheet({"claims": []}), [], [], make_settings(repo))
    assert "آنچه هنوز مشخص نیست" not in out.html
    assert out.plain_text.count("امکان خرید جداگانه‌اش") == 1      # said once, in the body
    assert out.uncertainties == []


def test_live_store_page_title_chrome_is_removed():
    from newsbot.htmlextract import extract_page
    from newsbot.textutil import clean_source_title, outlet_display
    live = BY_GAME["forza"]["article_html"]
    shown = live.split("store.playstation.com</a> — ")[1].split("</li>")[0]
    assert shown.startswith("Forza Horizon 6Add to Wishlist")                 # the live defect
    cleaned = clean_source_title(shown, "PlayStation Store")
    assert cleaned.startswith("Forza Horizon 6") and "Wishlist" not in cleaned
    page = extract_page("<html><head><title>Forza Horizon 6 | PlayStation Store</title></head><body>"
                        + "<button><svg><title>Add to Wishlist</title></svg></button>" * 8
                        + "<main><p>" + "Forza Horizon 6 PS5 details. " * 20 + "</p></main></body></html>")
    assert page.title == "Forza Horizon 6 | PlayStation Store"
    assert clean_source_title(page.title, "PlayStation Store") == "Forza Horizon 6"
    assert outlet_display("https://store.playstation.com/en-us/concept/10016335") == "PlayStation Store"
    assert outlet_display("https://news.xbox.com/en-us/2026/10/07/x/") == "Xbox Wire"


def test_live_name_spelling_is_made_consistent(repo):
    from conftest import make_settings

    from newsbot.compose import display_names, parse_article
    names = display_names(make_settings(repo))
    record = BY_GAME["mhs3"]["article"]
    assert "XBOX on PC" in record["title"] and "Xbox Play Anywhere" in record["plain_text"]   # the live mix
    data = {"title_fa": record["title"], "lead": {"text": record["plain_text"].split("\n")[0], "claim_ids": []},
            "sections": [], "uncertainties_fa": [], "entity_tags": ["XBOX on PC"]}
    article = parse_article(data, names)
    assert "XBOX" not in article.title_fa + article.lead["text"] + " ".join(article.entity_tags)
    assert "Xbox on PC" in article.title_fa
