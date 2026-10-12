"""Stage 11: editorial quality gate.

An explainable pass/reject decision. Every criterion is a named check with a reason;
the story is published only when all blocking checks pass. Editorial overrides from
config/editorial.yaml may relax reliability/significance/rumor policy for named
entities, but can never relax fact verification (NON_OVERRIDABLE).
"""

from __future__ import annotations

import re
from datetime import timedelta

from .config import Settings
from .facts import story_status
from .models import AUTHORITY_RANK, CheckResult, Claim, FactSheet, GateDecision, Story, utcnow
from .research import ResearchResult
from .textutil import entity_key

NON_OVERRIDABLE_PREFIXES = ("citations.", "facts.", "fabrication.", "status.", "factcheck.", "claims.",
                            "language.", "persian.", "duplicate.")
RETRYABLE_CHECKS = {"sources.available"}
# Bump when the editorial rules change what passes: stories rejected under an older policy may then be
# reconsidered once (see pipeline._eligibility). Combined with prompts.PROMPT_VERSION in policy_version().
EDITORIAL_POLICY_VERSION = "2.1"

# Topics where a single medium-authority report is not enough: allegations, legal and labour matters,
# closures, cancellations, acquisitions, leaks. Extend with `high_risk_patterns` in config/editorial.yaml.
HIGH_RISK_RE = re.compile(
    r"\b(lawsuits?|sued|suing|court (?:case|ruling|filing|order)s?|allegations?|alleged(?:ly)?|accus\w*|"
    r"harass\w*|abus(?:e|ive)|fraud\w*|scandal\w*|arrest\w*|lay ?offs?|laid off|fired|firings?|unioni[sz]\w*|"
    r"on strike|strike action|bankrupt\w*|insolven\w*|acquisitions?|acquires|acquired by|mergers?|buyouts?|"
    r"shut(?:s|ting)? down|shutdown|closures?|clos(?:e|es|ing) (?:its |the )?studio|"
    r"cancel(?:s|led|ed|ling|ing|lation)?|leak\w*|insiders?|datamin\w*|rumou?r\w*|reportedly|anonymous|"
    r"unnamed sources?|according to sources|died|dies|passed away|hack(?:ed|ers)|data breach)\b",
    re.I,
)


def policy_version() -> str:
    from .prompts import PROMPT_VERSION
    return f"{EDITORIAL_POLICY_VERSION}+{PROMPT_VERSION}"


def story_risk(story: Story, sheet: FactSheet, settings: Settings) -> list[str]:
    """Why this story is high-risk (empty list = ordinary, low-risk news)."""
    reasons: list[str] = []
    core = sheet.core_claims
    speculative = [c.id for c in core if c.status == "speculative"]
    if speculative:
        reasons.append("speculative core claim(s) " + ", ".join(speculative))
    if story_status(sheet) == "rumor" or story.kind == "rumor" or sheet.kind == "rumor":
        reasons.append("rumor")
    if sheet.contradictions:
        reasons.append("conflicting reports")
    extra = [str(p) for p in ((settings.editorial or {}).get("high_risk_patterns") or []) if str(p).strip()]
    # Headlines name the topic; claim texts are not scanned (a claim like "players can acquire the DLC" or a
    # game called Death Stranding must not turn ordinary news into a high-risk story).
    for text in (i.title for i in story.items):
        match = HIGH_RISK_RE.search(text or "") or next(
            (m for m in (re.search(p, text or "", re.I) for p in extra) if m), None)
        if match:
            reasons.append(f"sensitive topic ({match.group(0).lower()})")
            break
    return reasons


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
    # A single trusted publication is enough for ordinary news (NEWSBOT_SINGLE_SOURCE_LOW_RISK_MIN_AUTHORITY,
    # default medium); rumors, allegations, conflicting reports and other high-risk stories need
    # NEWSBOT_SINGLE_SOURCE_MIN_AUTHORITY (default high) or independent corroboration.
    by_id = {d.doc_id: d for d in research.docs}
    risk = story_risk(story, sheet, settings)
    level = settings.single_source_min_authority if risk else settings.single_source_low_risk_min_authority
    if ov["single_source_ok"]:
        level = min((level, "medium"), key=lambda v: AUTHORITY_RANK.get(v, 3))
    needed = AUTHORITY_RANK.get(level, 3)
    add(CheckResult("sources.risk", True, False,
                    ("high-risk story (" + "; ".join(risk) + f"): single sources need authority {level}")
                    if risk else f"ordinary news: a single source of authority {level} or higher may suffice"))
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
