"""Stage 11: editorial quality gate.

An explainable pass/reject decision. Every criterion is a named check with a reason;
the story is published only when all blocking checks pass. Editorial overrides from
config/editorial.yaml may relax reliability/significance/rumor policy for named
entities, but can never relax fact verification (NON_OVERRIDABLE).
"""

from __future__ import annotations

from datetime import timedelta

from .config import Settings
from .facts import story_status
from .models import AUTHORITY_RANK, CheckResult, FactSheet, GateDecision, Story, utcnow
from .research import ResearchResult
from .textutil import entity_key

NON_OVERRIDABLE_PREFIXES = ("citations.", "facts.", "fabrication.", "status.", "factcheck.", "claims.",
                            "language.", "persian.", "duplicate.")
RETRYABLE_CHECKS = {"sources.available"}


def overrides_for(story: Story, settings: Settings) -> dict:
    cfg = (settings.editorial or {}).get("overrides") or {}
    keys = story.entity_keys | ({story.primary_entity} if story.primary_entity else set())

    def matches(name: str) -> bool:
        return any(entity_key(e) in keys for e in cfg.get(name) or [])

    return {
        "single_source_ok": matches("allow_single_source_entities"),
        "rumor_ok": matches("allow_rumor_entities"),
        "priority": matches("priority_entities"),
        "force": story.manual,
    }


def source_checks(story: Story, sheet: FactSheet, research: ResearchResult, settings: Settings,
                  duplicate_reason: str = "") -> list[CheckResult]:
    checks: list[CheckResult] = []
    add = checks.append
    ov = overrides_for(story, settings)
    usable_docs = research.usable_docs
    add(CheckResult("sources.available", bool(usable_docs), True,
                    "no source document could be retrieved" if not usable_docs else f"{len(usable_docs)} document(s)"))
    usable = sheet.usable_claims
    add(CheckResult("claims.enough_verified", len(usable) >= settings.min_verified_claims, True,
                    f"{len(usable)} verified claim(s) (minimum {settings.min_verified_claims})"))
    core = sheet.core_claims
    add(CheckResult("claims.core_verified", bool(core), True,
                    "the core news could not be verified in the sources" if not core else f"{len(core)} core claim(s)"))

    # Reliability ------------------------------------------------------------------
    levels = {c.verification for c in core}
    by_id = {d.doc_id: d for d in research.docs}
    if "official" in levels:
        add(CheckResult("sources.reliability", True, True, "core news confirmed by an official source"))
    elif "corroborated" in levels:
        add(CheckResult("sources.reliability", True, True, "core news corroborated by independent sources"))
    elif "single_source" in levels:
        best = max((AUTHORITY_RANK.get(by_id[s].authority, 1) for c in core for s in c.verified_sources
                    if s in by_id), default=0)
        needed = AUTHORITY_RANK.get("medium" if ov["single_source_ok"] else settings.single_source_min_authority, 3)
        ok = settings.allow_single_source and best >= needed
        add(CheckResult("sources.reliability", ok, True,
                        "single source with sufficient authority" if ok else
                        f"only one non-official source (authority {best} < required {needed})"))
    else:
        add(CheckResult("sources.reliability", False, True, "no verified support for the core news"))

    # Contradictions -----------------------------------------------------------------
    core_ids = {c.id for c in core}
    open_core = [c for c in sheet.contradictions if not c.get("resolved") and core_ids & set(c.get("claim_ids", []))]
    add(CheckResult("claims.no_unresolved_core_contradiction", not open_core, True,
                    "; ".join(c.get("topic", "") for c in open_core)[:200] if open_core else ""))

    # Rumors --------------------------------------------------------------------------
    status = story_status(sheet)
    if status == "rumor":
        if settings.rumor_policy == "reject" and not (ov["rumor_ok"] or ov["force"]):
            add(CheckResult("policy.rumors", False, True, "rumor stories are disabled (NEWSBOT_RUMOR_POLICY=reject)"))
        else:
            groups = {by_id[s].origin_group for c in core for s in c.verified_sources if s in by_id}
            ok = len(groups) >= 2 or ov["rumor_ok"]
            add(CheckResult("policy.rumors", ok, True,
                            f"rumor from {len(groups)} independent origin(s); at least 2 required" if not ok else
                            f"rumor corroborated by {len(groups)} independent origin(s)"))

    # Timeliness / duplication ---------------------------------------------------------
    age = utcnow() - story.first_published
    fresh = age <= timedelta(hours=settings.max_story_age_hours) or ov["force"]
    add(CheckResult("timeliness", fresh, True, f"first reported {age.total_seconds() / 3600:.0f}h ago"))
    add(CheckResult("duplicate.not_published", not duplicate_reason, True, duplicate_reason))
    if sheet.injection_detected or any("instruction-like" in n for d in research.docs for n in d.notes):
        add(CheckResult("security.injection_neutralised", True, False,
                        "instruction-like text found in a source and ignored"))
    return checks


def decide(checks: list[CheckResult]) -> GateDecision:
    passed = all(c.passed for c in checks if c.blocking)
    failed = {c.name for c in checks if c.blocking and not c.passed}
    retryable = bool(failed) and failed <= RETRYABLE_CHECKS
    return GateDecision(passed=passed, checks=checks, retryable=retryable)
