"""Stage 8: newsworthiness assessment.

Cheap rule-based ranking first (coverage breadth, official sources, event type,
freshness, relevance to Poormaz's localization catalogue, source diversity), then an
optional single batched model call that triages the top candidates by headline. After
fact extraction the model's own newsworthiness judgement is checked again.
"""

from __future__ import annotations

import logging

from .config import Settings
from .llm import LLMClient
from .models import CheckResult, FactSheet, Story, utcnow
from .prompts import TRIAGE_SCHEMA, TRIAGE_SYSTEM, build_triage_input

log = logging.getLogger("newsbot.news")

EVENT_WEIGHTS = {
    "release_date": 1.0, "delay": 1.0, "launch": 0.9, "announcement": 0.9, "cancellation": 0.9,
    "port_platform": 0.9, "dlc_expansion": 0.8, "price": 0.8, "patch_update": 0.7, "trailer": 0.6,
    "beta_playtest": 0.6, "business": 0.6, "hardware_spec": 0.6, "event_show": 0.6, "sales_numbers": 0.5,
    "content_update": 0.5, "esports": 0.3, "other": 0.35,
}


def rule_score(story: Story, localized_keys: set[str], recent_outlets: list[str]) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = EVENT_WEIGHTS.get(story.event_type, 0.35)
    reasons.append(f"event {story.event_type} ({score:.2f})")
    outlets = len(story.outlets)
    if outlets > 1:
        bonus = 0.25 * min(outlets - 1, 3)
        score += bonus
        reasons.append(f"{outlets} outlets (+{bonus:.2f})")
    if any(i.source_type == "official" for i in story.items):
        score += 0.3
        reasons.append("official source (+0.30)")
    best_authority = max(i.authority_rank for i in story.items)
    score += 0.15 * (best_authority - 2)
    hours = max(0.0, (utcnow() - story.first_published).total_seconds() / 3600)
    fresh = max(0.0, 1.0 - hours / 48.0) * 0.4
    score += fresh
    reasons.append(f"age {hours:.0f}h (+{fresh:.2f})")
    if story.primary_entity and story.primary_entity in localized_keys:
        score += 0.35
        reasons.append("Poormaz has a localization page (+0.35)")
    kinds = {e.kind for i in story.items for e in i.entities if e.key == story.primary_entity}
    if "game" in kinds:
        score += 0.15
    if story.kind == "rumor":
        score -= 0.6
        reasons.append("rumor (-0.60)")
    if not story.primary_entity:
        score -= 0.3
        reasons.append("no clear subject (-0.30)")
    if story.outlets and recent_outlets and set(story.outlets) <= set(recent_outlets[:2]):
        score -= 0.2
        reasons.append("same outlet as recent posts (-0.20)")
    if story.is_development:
        score += 0.1
        reasons.append("development of earlier story (+0.10)")
    return round(score, 3), reasons


def seedable(story: Story) -> bool:
    """A story needs at least one primary (non corroboration-only) source or a manual request."""
    return story.manual or any(i.role == "primary" for i in story.items)


def triage(stories: list[Story], llm: LLMClient, settings: Settings) -> dict[str, dict]:
    if not stories:
        return {}
    data = llm.call_json("triage", TRIAGE_SYSTEM, build_triage_input(stories), "triage", TRIAGE_SCHEMA,
                         settings.max_output_tokens_triage, "low" if settings.reasoning_effort != "off" else None)
    out: dict[str, dict] = {}
    for row in data.get("stories") or []:
        if isinstance(row, dict) and row.get("id"):
            out[str(row["id"])] = row
    return out


def apply_triage(story: Story, verdict: dict | None) -> bool:
    """Adjust the story score with the triage verdict. Returns False when the story should be dropped."""
    if not verdict:
        return True
    story.triage = verdict
    if not verdict.get("is_news", True) or verdict.get("kind") in ("guide", "deal", "review", "list", "opinion",
                                                                  "sponsored", "other"):
        story.score_reasons.append(f"triage: not news ({verdict.get('reason', '')})")
        return False
    adjust = {"high": 0.5, "medium": 0.0, "low": -0.6}
    story.score += adjust.get(verdict.get("significance", "medium"), 0.0)
    story.score += {"high": 0.2, "medium": 0.0, "low": -0.3}.get(verdict.get("relevance", "medium"), 0.0)
    story.score_reasons.append(f"triage significance={verdict.get('significance')} "
                               f"relevance={verdict.get('relevance')}")
    if verdict.get("kind") == "rumor" and story.kind != "rumor":
        story.kind = "rumor"
    return True


def assess(sheet: FactSheet, story: Story, settings: Settings) -> list[CheckResult]:
    news = sheet.newsworthiness or {}
    checks = []
    is_news = bool(news.get("is_news", True)) and sheet.kind in ("news", "rumor")
    checks.append(CheckResult("newsworthiness.is_news", is_news, True,
                              "" if is_news else f"not news (kind={sheet.kind}): "
                              + "; ".join(news.get("reasons") or [])[:200]))
    significance = news.get("significance", "medium")
    meaningful = significance != "low" or len(story.outlets) >= 3 or story.manual
    checks.append(CheckResult("newsworthiness.significance", meaningful, True,
                              f"significance={significance}, outlets={len(story.outlets)}"))
    return checks
