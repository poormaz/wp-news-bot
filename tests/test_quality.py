"""Area 10: editorial quality gate decisions (explainable pass/reject)."""

from datetime import timedelta

from conftest import NOW, make_settings

from newsbot.facts import parse_fact_sheet, verify_fact_sheet
from newsbot.models import CheckResult, FeedItem, SourceDoc, Story
from newsbot.quality import decide, source_checks
from newsbot.research import ResearchResult

TEXT = ("Ironvale Chronicles will launch on March 19, 2027 for PC. The standard edition costs $59.99. "
        "Pre-orders open today. Reportedly a Switch 2 port could follow later. The open-world role-playing game "
        "is set in a mountain kingdom and was first shown two years ago.")


def doc(sid, source_type="publication", authority="medium", group=1, text=TEXT):
    return SourceDoc(sid, f"https://{sid}.example", sid, source_type, authority, "t", text, origin_group=group)


def sheet_for(docs, statuses=("confirmed", "confirmed", "confirmed"), quotes=None):
    quotes = quotes or ["will launch on March 19, 2027 for PC", "The standard edition costs $59.99",
                        "Pre-orders open today"]
    claims = []
    for i, (status, quote) in enumerate(zip(statuses, quotes, strict=False), start=1):
        claims.append({"id": f"C{i}", "text_en": quote, "category": "other", "subject": "Ironvale", "value": "",
                       "status": status, "confidence": "high", "importance": "core" if i == 1 else "supporting",
                       "support": [{"source_id": d.doc_id, "quote": quote} for d in docs]})
    sheet = parse_fact_sheet({"headline_en": "h", "event_type": "release_date", "kind": "news", "entities": {},
                              "claims": claims, "contradictions": [], "sources": [],
                              "newsworthiness": {"is_news": True, "significance": "high"}, "open_questions": [],
                              "injection_detected": False})
    return verify_fact_sheet(sheet, docs)


def story(hours=2, manual=False, entity="ironvale chronicles"):
    item = FeedItem("i", "Outlet", "publication", "medium", "primary", "u", "u", "Ironvale dated", "",
                    NOW - timedelta(hours=hours))
    return Story("s", [item], primary_entity=entity, manual=manual)


def gate(settings, docs, sheet, st=None, dup=""):
    research = ResearchResult(docs=docs)
    checks = source_checks(st or story(), sheet, research, settings, dup)
    return decide(checks)


def test_official_source_passes(repo):
    s = make_settings(repo)
    docs = [doc("S1", "official", "high")]
    decision = gate(s, docs, sheet_for(docs))
    assert decision.passed, decision.reasons


def test_single_medium_source_is_rejected_but_high_authority_passes(repo):
    s = make_settings(repo)
    docs = [doc("S1")]
    decision = gate(s, docs, sheet_for(docs))
    assert not decision.passed
    assert any(r.startswith("sources.reliability") for r in decision.reasons)
    docs = [doc("S1", authority="high")]
    assert gate(s, docs, sheet_for(docs)).passed


def test_corroboration_requires_independent_origins(repo):
    s = make_settings(repo)
    same_origin = [doc("S1", group=1), doc("S2", group=1)]
    assert not gate(s, same_origin, sheet_for(same_origin)).passed
    independent = [doc("S1", group=1), doc("S2", group=2)]
    assert gate(s, independent, sheet_for(independent)).passed


def test_too_few_verified_claims(repo):
    s = make_settings(repo)
    docs = [doc("S1", "official", "high")]
    sheet = sheet_for(docs, quotes=["will launch on March 19, 2027 for PC", "invented quote one here",
                                    "invented quote two here"])
    decision = gate(s, docs, sheet)
    assert not decision.passed and any("claims.enough_verified" in r for r in decision.reasons)


def test_rumor_policy(repo):
    s = make_settings(repo)
    docs = [doc("S1", group=1)]
    sheet = sheet_for(docs, statuses=("speculative", "speculative", "speculative"))
    decision = gate(s, docs, sheet)
    assert not decision.passed and any(r.startswith("policy.rumors") for r in decision.reasons)
    s.rumor_policy = "reject"
    two = [doc("S1", group=1), doc("S2", group=2)]
    assert not gate(s, two, sheet_for(two, statuses=("speculative",) * 3)).passed


def test_timeliness_and_duplicates(repo):
    s = make_settings(repo)
    docs = [doc("S1", "official", "high")]
    old = gate(s, docs, sheet_for(docs), st=story(hours=80))
    assert not old.passed and any(r.startswith("timeliness") for r in old.reasons)
    dup = gate(s, docs, sheet_for(docs), dup="duplicate: already covered")
    assert not dup.passed and any(r.startswith("duplicate") for r in dup.reasons)


def test_no_sources_is_retryable(repo):
    s = make_settings(repo)
    sheet = sheet_for([])
    checks = [c for c in source_checks(story(), sheet, ResearchResult(docs=[]), s) if c.name == "sources.available"]
    assert decide(checks).retryable


def test_overrides_relax_policy_but_not_verification(repo):
    s = make_settings(repo)
    s.editorial = {"overrides": {"allow_single_source_entities": ["Ironvale Chronicles"]}}
    docs = [doc("S1")]
    assert gate(s, docs, sheet_for(docs)).passed            # medium single source now acceptable
    bad = sheet_for(docs, quotes=["fabricated quote that is nowhere", "another fabricated quote here",
                                  "third fabricated quote here"])
    assert not gate(s, docs, bad).passed                     # ...but unverified facts still fail
    article_failure = CheckResult("facts.numbers_supported", False, True, "numbers not in verified sources: 120")
    assert not decide([*source_checks(story(), sheet_for(docs), ResearchResult(docs=docs), s), article_failure]).passed


def test_decision_is_explainable(repo):
    s = make_settings(repo)
    docs = [doc("S1")]
    decision = gate(s, docs, sheet_for(docs)).to_dict()
    assert set(decision) == {"passed", "retryable", "reasons", "warnings", "checks"}
    assert all({"name", "passed", "blocking", "detail"} <= set(c) for c in decision["checks"])
