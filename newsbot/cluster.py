"""Stage 3-4: story clustering and deduplication.

A story is one EVENT (one announcement or development), not one game. Five outlets
covering one announcement become one Story; a merch drop, a radio reveal and a
column about the same game stay three stories. Similarity combines:
  * weighted entity overlap (games, companies, hardware; platforms/events ignored),
  * topic similarity: the headline words that remain after removing the subject's
    names, platforms and generic verbs ("reveals", "features", "includes"), with
    release/launch/date wording normalised - two items about the same subject join
    only when they share the development itself,
  * text similarity (character 3-gram TF-IDF cosine over title + summary, plus
    headline token overlap) - a cheap local stand-in for semantic similarity,
  * event-type agreement ("other" is unknown, never a wildcard),
  * publication time proximity.
Story identities persist in the state DB so later runs attach new coverage to the
same story instead of creating a new one.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .entities import COMPATIBLE_EVENTS
from .models import FeedItem, Story
from .textutil import (
    build_idf,
    char_ngrams,
    entity_key,
    headline_tokens,
    jaccard,
    light_stem,
    overlap_coefficient,
    short_hash,
    tfidf_cosine,
)

# Two items about the same subject are the same story only if their topic words overlap this much.
TOPIC_MIN = 0.25
SUBJECT_KINDS = {"game", "hardware", "company", "franchise", "platform", "event"}
# Headline words that say nothing about WHICH development it is.
TOPIC_STOP = {
    "reveal", "featur", "feature", "includ", "include", "announc", "announce", "show", "showcas", "showcase",
    "confirm", "unveil", "detail", "offici", "official", "plus", "alongsid", "alongside", "game", "gam", "video",
    "player", "fan", "gamer", "studio", "developer", "publisher", "not", "no", "either", "way", "weve", "ive",
    "seen", "see", "still", "even", "why", "how", "what", "who", "when", "where", "which", "ha", "hav", "may",
    "might", "make", "mak", "bring", "giv", "give", "get", "come", "go", "big", "huge", "major", "minor", "today",
    "week", "month", "year", "soon", "upcoming", "everything", "know", "need", "want", "out", "now", "here",
    "ahead", "biggest", "part", "first", "two", "three",
}
# Different words for "it now has a date / it comes out".
TOPIC_SYNONYMS = {
    "launch": "release", "launche": "release", "releas": "release", "date": "release", "dates": "release",
    "dated": "release", "arriv": "release", "arrive": "release", "coming": "release", "availabl": "release",
    "available": "release", "preorder": "preorder", "pre": "preorder", "order": "preorder",
    "delayed": "delay", "postpon": "delay", "postpone": "delay",
}


def _event_score(a: str, b: str) -> float:
    """1 = same known event type, 0.6 = compatible, 0.5 = unknown ("other"), 0 = incompatible."""
    if a == b and a != "other":
        return 1.0
    if "other" in (a, b):
        return 0.5
    return 0.6 if any(a in g and b in g for g in COMPATIBLE_EVENTS) else 0.0


def _initials(display: str) -> str:
    words = [w for w in entity_key(display).split() if w.isalpha()]
    return "".join(w[0] for w in words) if len(words) >= 2 else ""


def subject_tokens(item: FeedItem, gazetteer=None, primary: str = "") -> set[str]:
    """Stemmed headline tokens that name the subject (games, companies, platforms...), incl. aliases."""
    raw: set[str] = set()
    for entity in item.entities:
        if entity.kind in SUBJECT_KINDS or (primary and entity.key == primary):
            raw.update(entity.key.split())
            raw.update(entity_key(entity.display).split())
            if _initials(entity.display):
                raw.add(_initials(entity.display))
    if gazetteer is not None:
        raw |= gazetteer.subject_tokens(item.title, SUBJECT_KINDS)
    return {light_stem(t) for t in raw} | raw


def topic_tokens(text: str, subject: set[str]) -> set[str]:
    out: set[str] = set()
    for token in headline_tokens(text):
        token = TOPIC_SYNONYMS.get(token, token)
        if token in subject or token in TOPIC_STOP or len(token) < 2:
            continue
        out.add(token)
    return out


def topic_similarity(a: set[str], b: set[str]) -> float | None:
    """Overlap of the development words; None when one side has none (unknown, not different)."""
    if not a or not b:
        return None
    return overlap_coefficient(a, b)


@dataclass
class _Vec:
    item: FeedItem
    grams: Counter
    tokens: set[str]
    weights: dict[str, float]
    primary: str
    topic: set[str]


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
    def __init__(self, threshold: float = 0.55, window_hours: int = 72, gazetteer=None):
        self.threshold = threshold
        self.window = timedelta(hours=window_hours)
        self.gazetteer = gazetteer
        self.idf: dict[str, float] = {}

    def _vec(self, item: FeedItem) -> _Vec:
        text = f"{item.title} {item.title} {item.summary[:300]}"
        primary = _primary(item)
        topic = topic_tokens(item.title, subject_tokens(item, self.gazetteer, primary))
        return _Vec(item=item, grams=char_ngrams(text), tokens=set(headline_tokens(item.title)),
                    weights=_entity_weights(item), primary=primary, topic=topic)

    def same_event(self, a: _Vec, b: _Vec) -> bool:
        """Event-level test used to stop chaining: topic overlap, or near-identical text."""
        topic = topic_similarity(a.topic, b.topic)
        if topic is not None:
            return topic >= TOPIC_MIN or tfidf_cosine(a.grams, b.grams, self.idf) > 0.8
        return _event_score(a.item.event_type, b.item.event_type) >= 0.6

    def similarity(self, a: _Vec, b: _Vec) -> float:
        dt = abs(a.item.published_at - b.item.published_at)
        if dt > self.window:
            return 0.0
        ent = weighted_entity_overlap(a.weights, b.weights)
        same_primary = bool(a.primary) and _related_keys(a.primary, b.primary)
        chars = tfidf_cosine(a.grams, b.grams, self.idf)
        if same_primary:
            ent = max(ent, 0.85 if a.primary == b.primary else 0.65)
        elif a.primary and b.primary and ent < 0.2:
            # Two clearly different main subjects: only near-identical text can join them.
            return 0.5 * chars if chars > 0.8 else 0.0
        if not a.weights and not b.weights:
            return max(chars, jaccard(a.tokens, b.tokens))  # no entities at all: rely on text only
        event = _event_score(a.item.event_type, b.item.event_type)
        topic = topic_similarity(a.topic, b.topic)
        near_identical = chars > 0.8
        if topic is None:
            # One headline has no development words left ("Crimson Desert"): needs a known, agreeing event.
            if event < 0.6 and not near_identical:
                return 0.0
            topic = 0.5
        elif topic < TOPIC_MIN and not near_identical:
            # Same game, different development (merch vs. radio stations vs. a column): never one story,
            # whatever NEWSBOT_CLUSTER_THRESHOLD is set to.
            return 0.0
        text = max(chars, jaccard(a.tokens, b.tokens), topic)
        score = 0.45 * ent + 0.35 * text + 0.20 * event
        if event == 0.0:
            score *= 0.8 if dt <= timedelta(hours=24) else 0.6
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
                # Anti-chaining: the newcomer must be the same event as at least half of the members,
                # so A~B and B~C cannot pull an unrelated C into A's story.
                agree = sum(1 for m in members if self.same_event(vec, m))
                if (top >= self.threshold and mean >= self.threshold * 0.7 and agree * 2 >= len(members)
                        and top > best_score):
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


def stored_story_similarity(items: list[FeedItem], stored: dict, gazetteer=None) -> float:
    keys = set()
    for item in items:
        keys |= item.entity_keys
    stored_keys = set(json.loads(stored.get("entity_keys_json") or "[]"))
    stored_tokens = set(json.loads(stored.get("title_tokens_json") or "[]"))
    primary, _, event, _ = summarize_cluster(items)
    stored_event = stored.get("event_type") or "other"
    ent = jaccard(keys, stored_keys)
    if primary and _related_keys(primary, stored.get("primary_entity") or ""):
        ent = max(ent, 0.85)
    elif primary and stored.get("primary_entity") and ent < 0.2:
        return 0.0
    last_seen = _parse_time(stored.get("last_seen_at"))
    newest = max(i.published_at for i in items)
    if event != stored_event and last_seen and (newest - last_seen) > timedelta(hours=72):
        return 0.0  # a different kind of event days later is a new story, not more coverage
    # Same subject is not enough: the development itself must match the stored story's headlines.
    subject: set[str] = set()
    for item in items:
        subject |= subject_tokens(item, gazetteer, primary)
    stored_primary = stored.get("primary_entity") or ""
    subject |= set(stored_primary.split()) | {light_stem(t) for t in stored_primary.split()}
    new_topic: set[str] = set()
    for item in items:
        new_topic |= topic_tokens(item.title, subject)
    old_topic = {TOPIC_SYNONYMS.get(t, t) for t in stored_tokens} - subject - TOPIC_STOP
    topic = topic_similarity(new_topic, old_topic)
    if topic is not None and topic < TOPIC_MIN:
        return 0.0
    tok = jaccard(set(story_title_tokens(items)), stored_tokens)
    ev = _event_score(event, stored_event)
    score = 0.5 * ent + 0.3 * max(tok, topic or 0.0) + 0.2 * ev
    return score if ev > 0.0 else score * 0.6


def assign_story_ids(clusters: list[list[FeedItem]], item_story: dict[str, str], stored_stories: list[dict],
                     threshold: float = 0.55, gazetteer=None) -> list[Story]:
    """Give every cluster a persistent story id (existing when items or signature match).

    One story id is never given to two clusters of the same run: if an earlier run grouped
    unrelated items under one id, the cluster holding most of that story's items keeps it and
    the others become new stories.
    """
    by_id = {s["story_id"]: s for s in stored_stories}
    proposals: list[tuple[str, int]] = []
    for items in clusters:
        existing = [item_story.get(i.item_id) for i in items if item_story.get(i.item_id)]
        if not existing:
            proposals.append(("", 0))
            continue
        counts = Counter(existing)
        # Prefer a story that is already published: protects against duplicates.
        published = [sid for sid in counts if (by_id.get(sid) or {}).get("status") in ("published", "drafted")]
        sid = published[0] if published else counts.most_common(1)[0][0]
        proposals.append((sid, counts[sid]))
    owner: dict[str, int] = {}
    for index, (sid, count) in enumerate(proposals):
        if sid and (sid not in owner or count > proposals[owner[sid]][1]):
            owner[sid] = index

    stories: list[Story] = []
    taken = set(owner)
    for index, items in enumerate(clusters):
        primary, display, event, kind = summarize_cluster(items)
        sid, _ = proposals[index]
        story_id = sid if sid and owner.get(sid) == index else ""
        if not story_id:
            best, best_score = None, 0.0
            for stored in stored_stories:
                if stored["story_id"] in taken:
                    continue
                score = stored_story_similarity(items, stored, gazetteer)
                if score >= threshold and score > best_score:
                    best, best_score = stored, score
            if best:
                story_id = best["story_id"]
        if not story_id:
            first = min(items, key=lambda i: i.published_at)
            story_id = short_hash(f"{primary}|{event}|{first.url_identity}", 16)
        taken.add(story_id)
        stories.append(Story(story_id=story_id, items=items, primary_entity=primary, primary_display=display,
                             event_type=event, kind=kind, manual=any(i.manual for i in items)))
    return stories
