"""Stage 1-2: source discovery (RSS + manual links) and story normalization.

Outputs FeedItem records with entities, event type and editorial kind already
attached, plus a cheap rule-based pre-filter so that guides, deals, reviews, lists,
opinion pieces and stale items never reach an API call.
"""

from __future__ import annotations

import calendar
import html as html_lib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import yaml

from .config import ConfigError
from .entities import EntityExtractor, classify_event, classify_kind
from .htmlextract import extract_page
from .httpclient import HttpClient
from .models import FeedItem, SourceConfig, utcnow
from .textutil import clean_text, entity_key, short_hash, site_host, truncate_words, url_identity

log = logging.getLogger("newsbot.ingest")

NON_NEWS_KINDS = {"guide", "deal", "review", "list", "opinion", "sponsored", "other"}
NON_NEWS_FEED_CATEGORIES = re.compile(r"^(deals?|guides?|reviews?|sponsored|buying guides?|opinion|features?|"
                                      r"hardware deals|best of|walkthroughs?|quiz)$", re.I)


@dataclass
class FeedHealth:
    source: str
    ok: bool
    status: int = 0
    entries: int = 0
    error: str = ""
    elapsed_ms: int = 0


def load_sources(path: Path) -> list[SourceConfig]:
    """Read sources.yaml. Entries with only name/feed (bot v1 format) still work."""
    if not path.exists():
        raise ConfigError(f"Missing sources file {path.name}")
    with path.open("r", encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle) or {}
    raw = cfg.get("sources") if isinstance(cfg, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ConfigError("sources.yaml must contain a non-empty 'sources:' list")
    out: list[SourceConfig] = []
    names: set[str] = set()
    for row in raw:
        if not isinstance(row, dict) or not row.get("name") or not row.get("feed"):
            raise ConfigError("Each source in sources.yaml needs 'name' and 'feed'")
        name = str(row["name"]).strip()
        if name in names:
            raise ConfigError(f"Duplicate source name in sources.yaml: {name}")
        names.add(name)
        source = SourceConfig(
            name=name,
            feed=str(row["feed"]).strip(),
            type=str(row.get("type", "publication")).lower(),
            authority=str(row.get("authority", "medium")).lower(),
            role=str(row.get("role", "primary")).lower(),
            enabled=bool(row.get("enabled", True)),
            images=str(row.get("images", "none")).lower(),
            domains=[str(d).lower() for d in (row.get("domains") or [site_host(row["feed"])])],
        )
        if source.type not in ("official", "publication"):
            raise ConfigError(f"{name}: type must be 'official' or 'publication'")
        if source.authority not in ("high", "medium", "low"):
            raise ConfigError(f"{name}: authority must be high, medium or low")
        if source.role not in ("primary", "corroboration"):
            raise ConfigError(f"{name}: role must be 'primary' or 'corroboration'")
        if source.images not in ("none", "press_kit"):
            raise ConfigError(f"{name}: images must be 'none' or 'press_kit'")
        out.append(source)
    return out


def _entry_datetime(entry, now: datetime) -> datetime:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return now


def parse_feed(text: str, source: SourceConfig, extractor: EntityExtractor, now: datetime | None = None,
               limit: int = 15) -> list[FeedItem]:
    now = now or utcnow()
    parsed = feedparser.parse(text or "")
    items: list[FeedItem] = []
    seen: set[str] = set()
    for entry in (parsed.entries or [])[: max(1, limit)]:
        url = (entry.get("link") or "").strip()
        title = clean_text(entry.get("title", ""))
        if not url or not title:
            continue
        ident = url_identity(url)
        if ident in seen:
            continue
        seen.add(ident)
        summary = truncate_words(clean_text(entry.get("summary", "") or entry.get("description", "")), 700)
        content_html = ""
        for content in entry.get("content") or []:
            value = content.get("value") if isinstance(content, dict) else ""
            if value and len(value) > len(content_html):
                content_html = value
        published = _entry_datetime(entry, now)
        if published > now + timedelta(hours=2):
            published = now  # clocks/feeds in the future are not trusted
        categories = [clean_text(t.get("term", "")) for t in (entry.get("tags") or []) if isinstance(t, dict)]
        item = FeedItem(
            item_id=short_hash(ident, 24),
            source=source.name,
            source_type=source.type,
            authority=source.authority,
            role=source.role,
            url=url,
            url_identity=ident,
            title=title,
            summary=summary,
            published_at=published,
            content_html=content_html,
            categories=[c for c in categories if c],
        )
        annotate(item, extractor)
        items.append(item)
    return items


def annotate(item: FeedItem, extractor: EntityExtractor) -> None:
    item.entities = extractor.extract(item.title, item.summary)
    item.event_type = classify_event(item.title, item.summary)
    item.kind = classify_kind(item.title, item.summary)


def fetch_feed(source: SourceConfig, http: HttpClient, extractor: EntityExtractor, limit: int,
               now: datetime | None = None) -> tuple[list[FeedItem], FeedHealth]:
    result = http.get(source.feed, accept="application/rss+xml,application/atom+xml,application/xml;q=0.9,*/*;q=0.5")
    health = FeedHealth(source=source.name, ok=False, status=result.status, elapsed_ms=result.elapsed_ms)
    if not result.ok:
        health.error = result.error or result.classification
        log.warning("FEED %s unavailable: status=%s class=%s", source.name, result.status, result.classification)
        return [], health
    items = parse_feed(result.text, source, extractor, now=now, limit=limit)
    health.ok = True
    health.entries = len(items)
    log.info("FEED %s: status=%s entries=%d (%dms)", source.name, result.status, len(items), result.elapsed_ms)
    return items, health


def prefilter(item: FeedItem, max_age_hours: int, overrides: dict, now: datetime | None = None) -> str:
    """Return "" when the item may proceed, else a short rejection reason."""
    now = now or utcnow()
    if not item.manual and now - item.published_at > timedelta(hours=max_age_hours):
        return "stale"
    if item.kind in NON_NEWS_KINDS:
        return f"non-news:{item.kind}"
    if any(NON_NEWS_FEED_CATEGORIES.match(c) for c in item.categories):
        return "non-news:feed-category"
    blocked_entities = {entity_key(e) for e in (overrides.get("block_entities") or [])}
    if blocked_entities & item.entity_keys:
        return "blocked-entity"
    for pattern in overrides.get("block_url_patterns") or []:
        if re.search(str(pattern), item.url, flags=re.I):
            return "blocked-url"
    for pattern in overrides.get("block_title_patterns") or []:
        if re.search(str(pattern), item.title, flags=re.I):
            return "blocked-title"
    return ""


def read_manual_links(path: Path) -> list[str]:
    if not path.exists():
        return []
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line.lower().startswith(("http://", "https://")) and line not in urls:
            urls.append(line)
    return urls


def manual_item(url: str, http: HttpClient, extractor: EntityExtractor, sources: list[SourceConfig],
                now: datetime | None = None) -> tuple[FeedItem | None, str]:
    """Build a FeedItem for an editor-supplied URL. Verification rules still apply later."""
    now = now or utcnow()
    result = http.get(url, check_robots=True)
    if not result.ok:
        return None, f"unavailable ({result.classification})"
    page = extract_page(result.text, result.final_url or url)
    if not page.title:
        return None, "no title"
    host = site_host(url)
    match = next((s for s in sources if any(host == d or host.endswith("." + d) for d in s.domains)), None)
    published = now
    if page.published_at:
        try:
            published = datetime.fromisoformat(page.published_at.replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except ValueError:
            published = now
    ident = url_identity(url)
    item = FeedItem(
        item_id=short_hash(ident, 24),
        source=match.name if match else (page.site_name or host),
        source_type=match.type if match else "publication",
        authority=match.authority if match else "medium",
        role="primary",
        url=url,
        url_identity=ident,
        title=page.title,
        summary=truncate_words(page.text, 700),
        published_at=published,
        content_html="".join(f"<p>{html_lib.escape(p)}</p>" for p in page.paragraphs),
        manual=True,
    )
    annotate(item, extractor)
    return item, ""
