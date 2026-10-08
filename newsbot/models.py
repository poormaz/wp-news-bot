"""Typed records passed between pipeline stages."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

AUTHORITY_RANK = {"low": 1, "medium": 2, "high": 3}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SourceConfig:
    name: str
    feed: str
    type: str = "publication"          # official | publication
    authority: str = "medium"          # high | medium | low
    role: str = "primary"              # primary (may seed a story) | corroboration
    enabled: bool = True
    images: str = "none"               # none | press_kit
    domains: list[str] = field(default_factory=list)

    @property
    def authority_rank(self) -> int:
        return AUTHORITY_RANK.get(self.authority, 2)


@dataclass
class Entity:
    key: str
    display: str
    kind: str = "unknown"              # game | company | platform | hardware | franchise | unknown
    confidence: float = 0.6


@dataclass
class FeedItem:
    item_id: str
    source: str
    source_type: str
    authority: str
    role: str
    url: str
    url_identity: str
    title: str
    summary: str
    published_at: datetime
    content_html: str = ""
    categories: list[str] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    event_type: str = "other"
    kind: str = "news"
    manual: bool = False

    @property
    def entity_keys(self) -> set[str]:
        return {e.key for e in self.entities if e.kind not in ("platform", "event")}

    @property
    def authority_rank(self) -> int:
        return AUTHORITY_RANK.get(self.authority, 2)


@dataclass
class Story:
    story_id: str
    items: list[FeedItem]
    primary_entity: str = ""
    primary_display: str = ""
    event_type: str = "other"
    kind: str = "news"
    score: float = 0.0
    score_reasons: list[str] = field(default_factory=list)
    is_development: bool = False
    parent_story_id: str = ""
    parent_post: dict | None = None
    status: str = "new"
    triage: dict | None = None
    manual: bool = False

    @property
    def headline(self) -> str:
        best = sorted(self.items, key=lambda i: (-i.authority_rank, i.published_at))
        return best[0].title if best else ""

    @property
    def outlets(self) -> list[str]:
        seen: list[str] = []
        for item in self.items:
            if item.source not in seen:
                seen.append(item.source)
        return seen

    @property
    def first_published(self) -> datetime:
        return min(i.published_at for i in self.items)

    @property
    def entity_keys(self) -> set[str]:
        keys: set[str] = set()
        for item in self.items:
            keys |= item.entity_keys
        return keys


@dataclass
class SourceDoc:
    doc_id: str
    url: str
    outlet: str
    source_type: str                   # official | official_store | publication
    authority: str
    title: str
    text: str
    published_at: str = ""
    fetch_status: str = "ok"           # ok | feed_only | blocked | not_found | robots | error
    origin_group: int = 0
    links: list[tuple[str, str]] = field(default_factory=list)
    image_url: str = ""
    image_rights: str = "none"
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.fetch_status in ("ok", "feed_only") and len(self.text) >= 200

    @property
    def is_official(self) -> bool:
        return self.source_type in ("official", "official_store")


@dataclass
class Claim:
    id: str
    text_en: str
    category: str
    subject: str
    value: str
    status: str                        # confirmed | reported | speculative
    confidence: str
    importance: str                    # core | supporting | background
    support: list[dict] = field(default_factory=list)
    verified_sources: list[str] = field(default_factory=list)
    verification: str = "unverified"   # official | corroborated | single_source | unverified
    independent_groups: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.verification != "unverified"


@dataclass
class FactSheet:
    headline_en: str
    event_type: str
    kind: str
    entities: dict
    claims: list[Claim]
    contradictions: list[dict]
    sources_meta: list[dict]
    newsworthiness: dict
    open_questions: list[str]
    injection_detected: bool = False
    from_cache: bool = False
    verification_notes: list[str] = field(default_factory=list)

    @property
    def usable_claims(self) -> list[Claim]:
        return [c for c in self.claims if c.usable]

    @property
    def core_claims(self) -> list[Claim]:
        return [c for c in self.usable_claims if c.importance == "core"]

    def claim(self, claim_id: str) -> Claim | None:
        for c in self.claims:
            if c.id == claim_id:
                return c
        return None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LinkCandidate:
    id: str
    url: str
    title: str
    kind: str                          # localization | coverage | parent
    entity: str = ""
    media_id: int = 0


@dataclass
class Article:
    title_fa: str
    meta_title_fa: str
    meta_description_fa: str
    focus_keyword_fa: str
    slug_en: str
    lead: dict
    sections: list[dict]
    uncertainties: list[str]
    entity_tags: list[str]
    content_type: str
    coverage: dict
    html: str = ""
    plain_text: str = ""
    word_count: int = 0
    links_used: list[dict] = field(default_factory=list)
    revision: int = 0

    def paragraphs(self) -> list[dict]:
        out = [self.lead] if self.lead.get("text") else []
        for section in self.sections:
            out.extend(section.get("paragraphs") or [])
        return out


@dataclass
class CheckResult:
    name: str
    passed: bool
    blocking: bool = True
    detail: str = ""


@dataclass
class GateDecision:
    passed: bool
    checks: list[CheckResult]
    retryable: bool = False

    @property
    def reasons(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.passed and c.blocking]

    @property
    def warnings(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.passed and not c.blocking]

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "retryable": self.retryable,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "checks": [asdict(c) for c in self.checks],
        }
