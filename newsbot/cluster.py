"""Stage 3-4: story clustering and deduplication.

Five outlets covering one announcement become one Story. Similarity combines:
  * weighted entity overlap (games, companies, hardware; platforms/events ignored),
  * text similarity (character 3-gram TF-IDF cosine over title + summary, plus
    headline token overlap) - a cheap local stand-in for semantic similarity,
  * event-type agreement (a patch and a trailer for one game are different stories),
  * publication time proximity.
Story identities persist in the state DB so later runs attach new coverage to the
same story instead of creating a new one.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .entities import events_compatible
from .models import FeedItem, Story
from .textutil import build_idf, char_ngrams, headline_tokens, jaccard, short_hash, tfidf_cosine


@dataclass
class _Vec:
    item: FeedItem
    grams: Counter
    tokens: set[str]
    weights: dict[str, float]
    primary: str


def _entity_weights(item: FeedItem) -> dict[str, float]:
    return {e.key: e.confidence for e in item.entities if e.kind not in ("platform", "event")}


def _primary(item: FeedItem) -> str:
    rank = {"game": 0, "hardware": 1, "franchise": 2, "unknown": 3, "company": 4}
    cands = [e for e in item.entities if e.kind in rank and e.confidence >= 0.55]
    if not cands:
        return ""
    return sorted(cands, key=lambda e: (rank[e.kind], -e.confidence))[0].key


def weighted_entity_overlap(a: dict[str, float], b: dict[str, float]) -> float:
    keys = set(a) | set(b)
    if not keys:
        return 0.0
    inter = sum(min(a.get(k, 0.0), b.get(k, 0.0)) for k in keys)
    union = sum(max(a.get(k, 0.0), b.get(k, 0.0)) for k in keys)
    return inter / union if union else 0.0


def _related_keys(a: str, b: str) -> bool:
    """'assassins creed hexe' ~ 'assassins creed' style containment (sequel/subtitle variants)."""
    if not a or not b:
        return False
    return a == b or a.startswith(b + " ") or b.startswith(a + " ")


class Clusterer:
    def __init__(self, threshold: float = 0.55, window_hours: int = 72):
        self.threshold = threshold
        self.window = timedelta(hours=window_hours)
        self.idf: dict[str, float] = {}

    def _vec(self, item: FeedItem) -> _Vec:
        text = f"{item.title} {item.title} {item.summary[:300]}"
        return _Vec(item=item, grams=char_ngrams(text), tokens=set(headline_tokens(item.title)),
                    weights=_entity_weights(item), primary=_primary(item))

    def similarity(self, a: _Vec, b: _Vec) -> float:
        dt = abs(a.item.published_at - b.item.published_at)
        if dt > self.window:
            return 0.0
        ent = weighted_entity_overlap(a.weights, b.weights)
        same_primary = bool(a.primary) and _related_keys(a.primary, b.primary)
        if same_primary:
            ent = max(ent, 0.85 if a.primary == b.primary else 0.65)
        elif a.primary and b.primary and ent < 0.2:
            # Two clearly different main subjects: only near-identical text can join them.
            text = tfidf_cosine(a.grams, b.grams, self.idf)
            return 0.5 * text if text > 0.8 else 0.0
        text = max(tfidf_cosine(a.grams, b.grams, self.idf), jaccard(a.tokens, b.tokens))
        compatible = events_compatible(a.item.event_type, b.item.event_type)
        event = 1.0 if a.item.event_type == b.item.event_type else (0.6 if compatible else 0.0)
        score = 0.45 * ent + 0.35 * text + 0.20 * event
        if not compatible:
            score *= 0.8 if dt <= timedelta(hours=24) else 0.6
        if not a.weights and not b.weights:
            score = text  # no entities at all: rely on text only
        return score

    def cluster(self, items: list[FeedItem]) -> list[list[FeedItem]]:
        ordered = sorted(items, key=lambda i: i.published_at)
        vecs = [self._vec(i) for i in ordered]
        self.idf = build_idf([v.grams for v in vecs])
        clusters: list[list[_Vec]] = []
        for vec in vecs:
            best_index, best_score = -1, 0.0
            for index, members in enumerate(clusters):
                scores = [self.similarity(vec, m) for m in members]
                top = max(scores)
                mean = sum(scores) / len(scores)
                if top >= self.threshold and mean >= self.threshold * 0.7 and top > best_score:
                    best_index, best_score = index, top
            if best_index >= 0:
                clusters[best_index].append(vec)
            else:
                clusters.append([vec])
        return [[v.item for v in members] for members in clusters]


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def summarize_cluster(items: list[FeedItem]) -> tuple[str, str, str, str]:
    """(primary_key, primary_display, event_type, kind) for a cluster."""
    votes: Counter = Counter()
    display: dict[str, str] = {}
    for item in items:
        key = _primary(item)
        if key:
            votes[key] += item.authority_rank
            ent = next(e for e in item.entities if e.key == key)
            display.setdefault(key, ent.display)
    primary = votes.most_common(1)[0][0] if votes else ""
    events = Counter(i.event_type for i in items if i.event_type != "other")
    event = events.most_common(1)[0][0] if events else "other"
    rumor = sum(1 for i in items if i.kind == "rumor")
    kind = "rumor" if rumor and rumor * 2 >= len(items) else "news"
    return primary, display.get(primary, ""), event, kind


def story_title_tokens(items: list[FeedItem]) -> list[str]:
    tokens: list[str] = []
    for item in items:
        tokens.extend(headline_tokens(item.title))
    return tokens


def stored_story_similarity(items: list[FeedItem], stored: dict) -> float:
    keys = set()
    for item in items:
        keys |= item.entity_keys
    stored_keys = set(json.loads(stored.get("entity_keys_json") or "[]"))
    stored_tokens = set(json.loads(stored.get("title_tokens_json") or "[]"))
    primary, _, event, _ = summarize_cluster(items)
    ent = jaccard(keys, stored_keys)
    if primary and _related_keys(primary, stored.get("primary_entity") or ""):
        ent = max(ent, 0.85)
    elif primary and stored.get("primary_entity") and ent < 0.2:
        return 0.0
    tok = jaccard(set(story_title_tokens(items)), stored_tokens)
    compatible = events_compatible(event, stored.get("event_type") or "other")
    last_seen = _parse_time(stored.get("last_seen_at"))
    newest = max(i.published_at for i in items)
    if event != stored.get("event_type") and last_seen and (newest - last_seen) > timedelta(hours=72):
        return 0.0  # a different kind of event days later is a new story, not more coverage
    ev = 1.0 if event == stored.get("event_type") else (0.6 if compatible else 0.0)
    score = 0.5 * ent + 0.3 * tok + 0.2 * ev
    return score if compatible else score * 0.6


def assign_story_ids(clusters: list[list[FeedItem]], item_story: dict[str, str], stored_stories: list[dict],
                     threshold: float = 0.55) -> list[Story]:
    """Give every cluster a persistent story id (existing when items or signature match)."""
    by_id = {s["story_id"]: s for s in stored_stories}
    stories: list[Story] = []
    for items in clusters:
        primary, display, event, kind = summarize_cluster(items)
        existing = [item_story.get(i.item_id) for i in items if item_story.get(i.item_id)]
        story_id = ""
        if existing:
            counts = Counter(existing)
            # Prefer a story that is already published: protects against duplicates.
            published = [sid for sid in counts if (by_id.get(sid) or {}).get("status") in ("published", "drafted")]
            story_id = published[0] if published else counts.most_common(1)[0][0]
        else:
            best, best_score = None, 0.0
            for stored in stored_stories:
                score = stored_story_similarity(items, stored)
                if score >= threshold and score > best_score:
                    best, best_score = stored, score
            if best:
                story_id = best["story_id"]
        if not story_id:
            first = min(items, key=lambda i: i.published_at)
            story_id = short_hash(f"{primary}|{event}|{first.url_identity}", 16)
        stories.append(Story(story_id=story_id, items=items, primary_entity=primary, primary_display=display,
                             event_type=event, kind=kind, manual=any(i.manual for i in items)))
    return stories
