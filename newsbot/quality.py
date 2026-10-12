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
from .models import AUTHORITY_RANK, CheckResult, Claim, FactSheet, GateDecision, Story, utcnow
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


def claim_reliability(claim: Claim, by_id: dict, allow_single_source: bool, needed: int) -> tuple[bool, str]:
    """Is this one claim reliably sourced? (ok, short explanation)."""
    if claim.verification == "official":
        names = sorted({by_id[s].outlet for s in claim.verified_sources if s in by_id and by_id[s].is_official})
        return True, f"official ({', '.join(names)})"
    if claim.verification == "corroborated":
        return True, f"corroborated by {claim.independent_groups} independent origins"
    if claim.verification == "single_source":
        docs = [by_id[s] for s in claim.verified_sources if s in by_id]
        best = max((AUTHORITY_RANK.get(d.authority, 1) for d in docs), default=0)
        outlets = ", ".join(d.outlet for d in docs) or "?"
        if allow_single_source and best >= needed:
            return True, f"single source with sufficient authority ({outlets})"
        return False, f"only {outlets} (authority {best} < required {needed})"
    return False, "no verified support"


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
    # Every core claim must stand on its own sources: an official page confirming one detail (a Steam
    # release window) says nothing about another core claim (a trailer only one outlet reported).
    by_id = {d.doc_id: d for d in research.docs}
    needed = AUTHORITY_RANK.get("medium" if ov["single_source_ok"] else settings.single_source_min_authority, 3)
    assessed = [(c, *claim_reliability(c, by_id, settings.allow_single_source, needed)) for c in core]
    weak = [(c, why) for c, ok, why in assessed if not ok]
    if not core:
        add(CheckResult("sources.reliability", False, True, "no verified support for the core news"))
    elif weak:
        add(CheckResult("sources.reliability", False, True,
                        "core claim(s) not reliably sourced: " + "; ".join(f"{c.id} {why}" for c, why in weak)[:400]))
    else:
        add(CheckResult("sources.reliability", True, True,
                        "every core claim is reliably sourced: " + "; ".join(f"{c.id} {why}" for c, _, why in assessed)[:400]))

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
            # Per claim, like reliability: each rumored core claim needs its own two independent origins.
            counts = {c.id: len({by_id[s].origin_group for s in c.verified_sources if s in by_id})
                      for c in core if c.status == "speculative"}
            short = {cid: n for cid, n in counts.items() if n < 2}
            ok = not short or ov["rumor_ok"]
            add(CheckResult("policy.rumors", ok, True,
                            "rumor claim(s) without 2 independent origins: "
                            + ", ".join(f"{cid} ({n})" for cid, n in short.items()) if not ok else
                            "each rumored core claim has at least 2 independent origins" if not short else
                            "rumor allowed by editorial override (allow_rumor_entities)"))

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
