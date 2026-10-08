"""Areas 5-6 and 9: fact extraction, provenance, verification and contradictions."""

from newsbot.facts import (
    compose_view,
    fact_cache_key,
    parse_fact_sheet,
    quote_in_text,
    story_status,
    value_in_text,
    verify_fact_sheet,
)
from newsbot.models import SourceDoc

OFFICIAL = SourceDoc("S1", "https://dev.example/news", "Northlight Forge", "official", "high", "Release date",
                     "Northlight Forge today announced that Ironvale Chronicles will launch on March 19, 2027 for PC "
                     "and PlayStation 5. The standard edition is priced at $59.99.", origin_group=1)
OUTLET_A = SourceDoc("S2", "https://a.example/x", "Outlet A", "publication", "medium", "Ironvale dated",
                     "Ironvale Chronicles is coming on March 19, 2027, the studio confirmed. It will cost $69.99, "
                     "according to a retailer listing. Reportedly a Switch 2 version could follow.", origin_group=2)
OUTLET_B = SourceDoc("S3", "https://b.example/y", "Outlet B", "publication", "high", "Ironvale on March 12",
                     "Ironvale Chronicles will be released on March 12, 2027 according to our sources.", origin_group=3)


def raw(claims, contradictions=None, sources=None):
    return {"headline_en": "h", "event_type": "release_date", "kind": "news",
            "entities": {"games": ["Ironvale Chronicles"], "companies": [], "platforms": [], "products": []},
            "claims": claims, "contradictions": contradictions or [], "sources": sources or [],
            "newsworthiness": {"is_news": True, "significance": "high", "reader_value": "", "reasons": []},
            "open_questions": [], "injection_detected": False}


def c(cid, value, status, quote_by_source, category="release_date", importance="core", subject="Ironvale Chronicles"):
    return {"id": cid, "text_en": f"claim {cid}", "category": category, "subject": subject, "value": value,
            "status": status, "confidence": "high", "importance": importance,
            "support": [{"source_id": s, "quote": q} for s, q in quote_by_source.items()]}


def test_quote_matching_is_verbatim_tolerant():
    text = OFFICIAL.text
    assert quote_in_text("Ironvale Chronicles will launch on March 19, 2027", text)
    assert quote_in_text("ironvale chronicles will launch on march 19, 2027 for PC", text)     # case
    assert quote_in_text("Northlight Forge today announced that Ironvale Chronicles will launch on March 19 2027",
                         text)                                                              # punctuation
    assert not quote_in_text("Ironvale Chronicles will launch on March 26, 2027", text)       # wrong date
    assert not quote_in_text("The game supports Xbox Series X|S at launch", text)            # invented
    assert not quote_in_text("short", text)


def test_value_must_appear_in_source():
    assert value_in_text("March 19, 2027", OFFICIAL.text)
    assert not value_in_text("March 26, 2027", OFFICIAL.text)
    assert value_in_text("$59.99", OFFICIAL.text)
    assert not value_in_text("$49.99", OFFICIAL.text)
    assert value_in_text("PC, PlayStation 5", OFFICIAL.text)


def test_provenance_and_verification_levels():
    sheet = parse_fact_sheet(raw([
        c("C1", "March 19, 2027", "confirmed", {"S1": "will launch on March 19, 2027 for PC",
                                                 "S2": "Ironvale Chronicles is coming on March 19, 2027"}),
        c("C2", "", "confirmed", {"S2": "the studio confirmed"}, category="other", importance="supporting"),
        c("C3", "Xbox Series X|S", "confirmed", {"S1": "available on Xbox Series X|S"}, category="platform"),
        c("C4", "", "confirmed", {"S9": "anything at all here"}, category="other", importance="supporting"),
    ]))
    verify_fact_sheet(sheet, [OFFICIAL, OUTLET_A])
    by = {cl.id: cl for cl in sheet.claims}
    assert by["C1"].verification == "official" and by["C1"].verified_sources == ["S1", "S2"]
    assert by["C1"].independent_groups == 2
    assert by["C2"].verification == "single_source"
    assert by["C3"].verification == "unverified" and "quote not found in S1" in by["C3"].problems
    assert by["C4"].verification == "unverified" and "cites unknown source S9" in by["C4"].problems
    view = compose_view(sheet, [OFFICIAL, OUTLET_A])
    assert {cl["id"] for cl in view["claims"]} == {"C1", "C2"}          # unverified claims never reach the writer
    assert view["claims"][0]["sources"] == ["Northlight Forge", "Outlet A"]


def test_fabricated_value_is_dropped_even_with_real_quote():
    sheet = parse_fact_sheet(raw([c("C1", "March 26, 2027", "confirmed",
                                    {"S1": "Ironvale Chronicles will launch on March 19, 2027"})]))
    verify_fact_sheet(sheet, [OFFICIAL])
    assert not sheet.claims[0].usable
    assert any("not present" in p for p in sheet.claims[0].problems)


def test_hedged_quote_downgrades_confirmed_status():
    sheet = parse_fact_sheet(raw([c("C1", "Switch 2", "confirmed", {"S2": "Reportedly a Switch 2 version could follow"},
                                    category="platform")]))
    verify_fact_sheet(sheet, [OUTLET_A])
    assert sheet.claims[0].status == "reported"
    assert story_status(sheet) == "reported"


def test_contradiction_resolved_by_official_source():
    sheet = parse_fact_sheet(raw([
        c("C1", "$59.99", "confirmed", {"S1": "The standard edition is priced at $59.99"}, category="price"),
        c("C2", "$69.99", "reported", {"S2": "It will cost $69.99, according to a retailer listing"}, category="price"),
    ]))
    verify_fact_sheet(sheet, [OFFICIAL, OUTLET_A])
    by = {cl.id: cl for cl in sheet.claims}
    assert by["C1"].usable and not by["C2"].usable
    assert "contradicted by an official source" in by["C2"].problems
    assert any(x["resolved"] for x in sheet.contradictions)


def test_unresolved_contradiction_is_kept_not_merged():
    sheet = parse_fact_sheet(raw([
        c("C1", "March 19, 2027", "reported", {"S2": "Ironvale Chronicles is coming on March 19, 2027"}),
        c("C2", "March 12, 2027", "reported", {"S3": "will be released on March 12, 2027"}),
    ], contradictions=[{"topic": "release date", "claim_ids": ["C1", "C2"], "explanation": "dates differ"}]))
    verify_fact_sheet(sheet, [OUTLET_A, OUTLET_B])
    assert all(cl.usable for cl in sheet.claims)
    open_items = [x for x in sheet.contradictions if not x["resolved"]]
    assert len(open_items) == 2          # model-reported + rule-detected
    view = compose_view(sheet, [OUTLET_A, OUTLET_B])
    assert view["contradictions"]       # the writer is told; it must explain, not merge


def test_press_release_copies_are_not_independent():
    copy = SourceDoc("S2", "https://c.example", "Copy Outlet", "publication", "medium", "x", OFFICIAL.text,
                     origin_group=2)
    sheet = parse_fact_sheet(raw([c("C1", "March 19, 2027", "confirmed",
                                    {"S2": "will launch on March 19, 2027 for PC"})],
                                 sources=[{"source_id": "S2", "origin": "Northlight Forge press release",
                                           "repeats_press_release": True, "is_primary_source": False}]))
    verify_fact_sheet(sheet, [OFFICIAL, copy])
    assert copy.origin_group == OFFICIAL.origin_group


def test_cache_key_changes_with_sources_and_model():
    key = fact_cache_key([OFFICIAL, OUTLET_A], "gpt-6-luna")
    assert key == fact_cache_key([OUTLET_A, OFFICIAL], "gpt-6-luna")
    assert key != fact_cache_key([OFFICIAL], "gpt-6-luna")
    assert key != fact_cache_key([OFFICIAL, OUTLET_A], "gpt-4o-mini")


def test_ids_are_renumbered_consistently():
    sheet = parse_fact_sheet(raw([c("x", "", "confirmed", {}), c("y", "", "confirmed", {})],
                                 contradictions=[{"topic": "t", "claim_ids": ["x", "y"], "explanation": ""}]))
    assert [cl.id for cl in sheet.claims] == ["C1", "C2"]
    assert sheet.contradictions[0]["claim_ids"] == ["C1", "C2"]
