"""Persistent pipeline state (SQLite, restored between GitHub Actions runs via cache).

The state DB is an optimisation, never the only line of defence: if the cache is
evicted the pipeline rebuilds its published-story fingerprints from WordPress itself
(see pipeline.recover_from_wordpress), so a lost cache cannot cause duplicate posts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Entity, FeedItem, Story, utcnow
from .textutil import legacy_title_norm, legacy_url_hash

log = logging.getLogger("newsbot.state")

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS articles (
    item_id TEXT PRIMARY KEY, source TEXT, source_type TEXT, authority TEXT, role TEXT,
    url TEXT, url_identity TEXT, title TEXT, summary TEXT, published_at TEXT,
    entities_json TEXT, event_type TEXT, kind TEXT, story_id TEXT, status TEXT,
    prefilter_reason TEXT, first_seen_at TEXT, content_html TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at);
CREATE INDEX IF NOT EXISTS idx_articles_identity ON articles(url_identity);
CREATE INDEX IF NOT EXISTS idx_articles_story ON articles(story_id);
CREATE TABLE IF NOT EXISTS stories (
    story_id TEXT PRIMARY KEY, primary_entity TEXT, primary_display TEXT, entity_keys_json TEXT,
    event_type TEXT, kind TEXT, title_tokens_json TEXT, headline TEXT, first_seen_at TEXT,
    last_seen_at TEXT, status TEXT, wp_post_id INTEGER, wp_link TEXT, published_at TEXT,
    fact_values_json TEXT, attempts INTEGER DEFAULT 0, transient_failures INTEGER DEFAULT 0,
    next_retry_at TEXT, last_reasons_json TEXT, source_count_at_decision INTEGER DEFAULT 0,
    parent_story_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_stories_last_seen ON stories(last_seen_at);
CREATE TABLE IF NOT EXISTS facts_cache (
    cache_key TEXT PRIMARY KEY, story_id TEXT, model TEXT, prompt_version TEXT, json TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, story_id TEXT, decision TEXT,
    reasons_json TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS publications (
    story_id TEXT PRIMARY KEY, run_id TEXT, status TEXT, slug TEXT, wp_post_id INTEGER, wp_link TEXT,
    mode TEXT, created_at TEXT, updated_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, mode TEXT, metrics_json TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, stage TEXT, model_requested TEXT,
    model_served TEXT, input_tokens INTEGER, cached_tokens INTEGER, output_tokens INTEGER,
    reasoning_tokens INTEGER, cost_usd REAL, latency_ms INTEGER, status TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls(created_at);
CREATE TABLE IF NOT EXISTS url_checks (url TEXT PRIMARY KEY, status INTEGER, final_url TEXT, ok INTEGER,
    checked_at TEXT);
CREATE TABLE IF NOT EXISTS entities_learned (key TEXT PRIMARY KEY, display TEXT, kind TEXT,
    seen_count INTEGER DEFAULT 1, last_seen_at TEXT);
CREATE TABLE IF NOT EXISTS openings (id INTEGER PRIMARY KEY AUTOINCREMENT, story_id TEXT, text TEXT,
    created_at TEXT);
CREATE TABLE IF NOT EXISTS anchors (id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT, anchor TEXT,
    created_at TEXT);
CREATE TABLE IF NOT EXISTS manual_urls (url_identity TEXT PRIMARY KEY, url TEXT, status TEXT, story_id TEXT,
    detail TEXT, processed_at TEXT);
CREATE TABLE IF NOT EXISTS wp_recent (post_id INTEGER PRIMARY KEY, link TEXT, title TEXT, date TEXT,
    status TEXT, story_id TEXT, source_urls_json TEXT, entity_keys_json TEXT, event_type TEXT,
    numbers_json TEXT, fetched_at TEXT);
"""

LEGACY_DDL = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY, source_name TEXT, title_en TEXT, title_norm TEXT, snippet_en TEXT, url TEXT,
    published_at TEXT, published_ts INTEGER, created_at TEXT, status TEXT, wp_post_id INTEGER,
    retry_count INTEGER DEFAULT 0, next_retry_at TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""


def iso(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).isoformat() if dt else ""


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class StateStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.fresh = not Path(self.path).exists() if self.path != ":memory:" else True
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        self.conn.commit()

    def _migrate(self) -> None:
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(articles)").fetchall()}
        if "content_html" not in columns:
            self.conn.execute("ALTER TABLE articles ADD COLUMN content_html TEXT DEFAULT ''")

    def prune(self, content_hours: int = 96, keep_days: int = 60) -> None:
        """Drop feed bodies outside the clustering window and forget very old rows."""
        now = utcnow()
        self.conn.execute("UPDATE articles SET content_html='' WHERE published_at < ? AND content_html != ''",
                          (iso(now - timedelta(hours=content_hours)),))
        cutoff = iso(now - timedelta(days=keep_days))
        self.conn.execute("DELETE FROM articles WHERE first_seen_at < ? AND published_at < ?", (cutoff, cutoff))
        self.conn.execute("DELETE FROM url_checks WHERE checked_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM llm_calls WHERE created_at < ?", (cutoff,))
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def integrity_ok(self) -> bool:
        try:
            row = self.conn.execute("PRAGMA integrity_check").fetchone()
            return bool(row) and row[0] == "ok"
        except sqlite3.DatabaseError:
            return False

    # -- meta -------------------------------------------------------------
    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, value))

    # -- articles -----------------------------------------------------------
    def upsert_items(self, items: list[FeedItem]) -> int:
        added = 0
        now = iso(utcnow())
        for item in items:
            entities = json.dumps([[e.key, e.display, e.kind, e.confidence] for e in item.entities], ensure_ascii=False)
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO articles(item_id, source, source_type, authority, role, url, url_identity, "
                "title, summary, published_at, entities_json, event_type, kind, story_id, status, prefilter_reason, "
                "first_seen_at, content_html) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item.item_id, item.source, item.source_type, item.authority, item.role, item.url, item.url_identity,
                 item.title, item.summary, iso(item.published_at), entities, item.event_type, item.kind, "",
                 "new", "", now, (item.content_html or "")[:40000]),
            )
            if cur.rowcount:
                added += 1
        self.conn.commit()
        return added

    def item_known(self, url_identity: str) -> bool:
        return self.conn.execute("SELECT 1 FROM articles WHERE url_identity=? LIMIT 1", (url_identity,)).fetchone() \
            is not None

    def set_item_status(self, item_id: str, status: str, reason: str = "", story_id: str | None = None) -> None:
        if story_id is None:
            self.conn.execute("UPDATE articles SET status=?, prefilter_reason=? WHERE item_id=?", (status, reason, item_id))
        else:
            self.conn.execute("UPDATE articles SET status=?, prefilter_reason=?, story_id=? WHERE item_id=?",
                              (status, reason, story_id, item_id))

    def recent_items(self, hours: int, now: datetime | None = None) -> list[FeedItem]:
        cutoff = iso((now or utcnow()) - timedelta(hours=hours))
        rows = self.conn.execute(
            "SELECT * FROM articles WHERE published_at >= ? AND status NOT LIKE 'legacy%' AND status != 'filtered'",
            (cutoff,)).fetchall()
        return [self._row_to_item(r) for r in rows]

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> FeedItem:
        entities = [Entity(key=e[0], display=e[1], kind=e[2], confidence=float(e[3]))
                    for e in json.loads(row["entities_json"] or "[]")]
        return FeedItem(
            item_id=row["item_id"], source=row["source"], source_type=row["source_type"] or "publication",
            authority=row["authority"] or "medium", role=row["role"] or "primary", url=row["url"],
            url_identity=row["url_identity"], title=row["title"], summary=row["summary"] or "",
            published_at=parse_iso(row["published_at"]) or utcnow(), entities=entities,
            event_type=row["event_type"] or "other", kind=row["kind"] or "news",
            content_html=row["content_html"] or "",
        )

    # -- stories --------------------------------------------------------------
    def get_story(self, story_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM stories WHERE story_id=?", (story_id,)).fetchone()
        return dict(row) if row else None

    def recent_stories(self, days: int, now: datetime | None = None) -> list[dict]:
        cutoff = iso((now or utcnow()) - timedelta(days=days))
        rows = self.conn.execute("SELECT * FROM stories WHERE last_seen_at >= ? OR published_at >= ?",
                                 (cutoff, cutoff)).fetchall()
        return [dict(r) for r in rows]

    def save_story(self, story: Story, title_tokens: list[str]) -> None:
        now = iso(utcnow())
        existing = self.get_story(story.story_id)
        if existing:
            self.conn.execute(
                "UPDATE stories SET entity_keys_json=?, title_tokens_json=?, headline=?, last_seen_at=?, "
                "primary_entity=COALESCE(NULLIF(primary_entity,''), ?), primary_display=COALESCE(NULLIF(primary_display,''), ?) "
                "WHERE story_id=?",
                (json.dumps(sorted(story.entity_keys)), json.dumps(sorted(set(title_tokens))), story.headline, now,
                 story.primary_entity, story.primary_display, story.story_id))
        else:
            self.conn.execute(
                "INSERT INTO stories(story_id, primary_entity, primary_display, entity_keys_json, event_type, kind, "
                "title_tokens_json, headline, first_seen_at, last_seen_at, status, parent_story_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (story.story_id, story.primary_entity, story.primary_display, json.dumps(sorted(story.entity_keys)),
                 story.event_type, story.kind, json.dumps(sorted(set(title_tokens))), story.headline, now, now, "new",
                 story.parent_story_id))
        for item in story.items:
            self.conn.execute("UPDATE articles SET story_id=? WHERE item_id=?", (story.story_id, item.item_id))
        self.conn.commit()

    def update_story(self, story_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE stories SET {cols} WHERE story_id=?", (*fields.values(), story_id))
        self.conn.commit()

    def record_decision(self, run_id: str, story_id: str, decision: str, reasons: list[str]) -> None:
        self.conn.execute("INSERT INTO decisions(run_id, story_id, decision, reasons_json, created_at) VALUES (?,?,?,?,?)",
                          (run_id, story_id, decision, json.dumps(reasons, ensure_ascii=False), iso(utcnow())))
        self.conn.commit()

    def story_item_count(self, story_id: str) -> int:
        row = self.conn.execute("SELECT COUNT(DISTINCT source) AS n FROM articles WHERE story_id=?", (story_id,)).fetchone()
        return int(row["n"] or 0)

    # -- facts cache ------------------------------------------------------------
    def get_facts(self, cache_key: str) -> dict | None:
        row = self.conn.execute("SELECT json FROM facts_cache WHERE cache_key=?", (cache_key,)).fetchone()
        return json.loads(row["json"]) if row else None

    def put_facts(self, cache_key: str, story_id: str, model: str, prompt_version: str, data: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO facts_cache VALUES (?,?,?,?,?,?)",
                          (cache_key, story_id, model, prompt_version, json.dumps(data, ensure_ascii=False),
                           iso(utcnow())))
        self.conn.commit()

    # -- publications -------------------------------------------------------------
    def get_publication(self, story_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM publications WHERE story_id=?", (story_id,)).fetchone()
        return dict(row) if row else None

    def begin_publication(self, story_id: str, run_id: str, slug: str, mode: str) -> None:
        now = iso(utcnow())
        self.conn.execute(
            "INSERT INTO publications(story_id, run_id, status, slug, mode, created_at, updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(story_id) DO UPDATE SET run_id=excluded.run_id, status='creating', slug=excluded.slug, "
            "mode=excluded.mode, updated_at=excluded.updated_at",
            (story_id, run_id, "creating", slug, mode, now, now))
        self.conn.commit()

    def update_publication(self, story_id: str, **fields) -> None:
        fields["updated_at"] = iso(utcnow())
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE publications SET {cols} WHERE story_id=?", (*fields.values(), story_id))
        self.conn.commit()

    def pending_publications(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM publications WHERE status IN ('creating','draft_created')").fetchall()
        return [dict(r) for r in rows]

    # -- runs / llm ledger ----------------------------------------------------------
    def start_run(self, run_id: str, mode: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO runs(run_id, started_at, mode) VALUES (?,?,?)",
                          (run_id, iso(utcnow()), mode))
        self.conn.commit()

    def finish_run(self, run_id: str, metrics: dict) -> None:
        self.conn.execute("UPDATE runs SET finished_at=?, metrics_json=? WHERE run_id=?",
                          (iso(utcnow()), json.dumps(metrics, ensure_ascii=False, default=str), run_id))
        self.conn.commit()

    def record_llm_call(self, run_id: str, stage: str, model_requested: str, model_served: str, input_tokens: int,
                        cached_tokens: int, output_tokens: int, reasoning_tokens: int, cost_usd: float | None,
                        latency_ms: int, status: str) -> None:
        self.conn.execute(
            "INSERT INTO llm_calls(run_id, stage, model_requested, model_served, input_tokens, cached_tokens, "
            "output_tokens, reasoning_tokens, cost_usd, latency_ms, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, stage, model_requested, model_served, input_tokens, cached_tokens, output_tokens,
             reasoning_tokens, cost_usd, latency_ms, status, iso(utcnow())))
        self.conn.commit()

    def cost_since(self, since: datetime) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(cost_usd),0) AS c FROM llm_calls WHERE created_at >= ?",
                                (iso(since),)).fetchone()
        return float(row["c"] or 0.0)

    # -- url checks -------------------------------------------------------------------
    def get_url_check(self, url: str, max_age_hours: int = 72) -> dict | None:
        row = self.conn.execute("SELECT * FROM url_checks WHERE url=?", (url,)).fetchone()
        if not row:
            return None
        checked = parse_iso(row["checked_at"])
        if not checked or utcnow() - checked > timedelta(hours=max_age_hours):
            return None
        return dict(row)

    def put_url_check(self, url: str, status: int, final_url: str, ok: bool) -> None:
        self.conn.execute("INSERT OR REPLACE INTO url_checks VALUES (?,?,?,?,?)",
                          (url, status, final_url, 1 if ok else 0, iso(utcnow())))
        self.conn.commit()

    # -- learned entities ---------------------------------------------------------------
    def learn_entities(self, entities: list[tuple[str, str, str]]) -> None:
        now = iso(utcnow())
        for key, display, kind in entities:
            if not key:
                continue
            self.conn.execute(
                "INSERT INTO entities_learned(key, display, kind, seen_count, last_seen_at) VALUES (?,?,?,1,?) "
                "ON CONFLICT(key) DO UPDATE SET seen_count=seen_count+1, last_seen_at=excluded.last_seen_at",
                (key, display, kind, now))
        self.conn.commit()

    def learned_entities(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT display, kind FROM entities_learned").fetchall()
        return {r["display"]: r["kind"] for r in rows}

    # -- openings / anchors ----------------------------------------------------------------
    def add_opening(self, story_id: str, text: str) -> None:
        self.conn.execute("INSERT INTO openings(story_id, text, created_at) VALUES (?,?,?)", (story_id, text, iso(utcnow())))
        self.conn.commit()

    def recent_openings(self, limit: int = 30) -> list[str]:
        rows = self.conn.execute("SELECT text FROM openings ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [r["text"] for r in rows]

    def add_anchor(self, target: str, anchor: str) -> None:
        self.conn.execute("INSERT INTO anchors(target, anchor, created_at) VALUES (?,?,?)", (target, anchor, iso(utcnow())))
        self.conn.commit()

    def recent_anchors(self, target: str, limit: int = 20) -> list[str]:
        rows = self.conn.execute("SELECT anchor FROM anchors WHERE target=? ORDER BY id DESC LIMIT ?",
                                 (target, limit)).fetchall()
        return [r["anchor"] for r in rows]

    # -- manual urls -------------------------------------------------------------------------
    def manual_status(self, url_identity: str) -> str:
        row = self.conn.execute("SELECT status FROM manual_urls WHERE url_identity=?", (url_identity,)).fetchone()
        return row["status"] if row else ""

    def set_manual_status(self, url_identity: str, url: str, status: str, story_id: str = "", detail: str = "") -> None:
        self.conn.execute("INSERT OR REPLACE INTO manual_urls VALUES (?,?,?,?,?,?)",
                          (url_identity, url, status, story_id, detail, iso(utcnow())))
        self.conn.commit()

    # -- WordPress recovery snapshot ------------------------------------------------------------
    def replace_wp_recent(self, posts: list[dict]) -> None:
        now = iso(utcnow())
        for post in posts:
            self.conn.execute(
                "INSERT OR REPLACE INTO wp_recent VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (post["id"], post.get("link", ""), post.get("title", ""), post.get("date", ""), post.get("status", ""),
                 post.get("story_id", ""), json.dumps(post.get("source_urls", [])),
                 json.dumps(sorted(post.get("entity_keys", []))), post.get("event_type", "other"),
                 json.dumps(post.get("numbers", [])), now))
        self.conn.commit()

    def wp_recent(self, days: int = 30) -> list[dict]:
        cutoff = iso(utcnow() - timedelta(days=days))
        rows = self.conn.execute("SELECT * FROM wp_recent WHERE date >= ? OR fetched_at >= ?", (cutoff, cutoff)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["source_urls"] = json.loads(d.pop("source_urls_json") or "[]")
            d["entity_keys"] = set(json.loads(d.pop("entity_keys_json") or "[]"))
            d["numbers"] = json.loads(d.pop("numbers_json") or "[]")
            out.append(d)
        return out

    # -- bot v1 compatibility --------------------------------------------------------------------
    def import_legacy(self, legacy_path: Path, days: int = 21) -> int:
        """Mark items bot v1 already handled as seen, once, so they are not treated as new."""
        if self.get_meta("legacy_imported") or not Path(legacy_path).exists():
            return 0
        cutoff = int((utcnow() - timedelta(days=days)).timestamp())
        imported = 0
        try:
            legacy = sqlite3.connect(f"file:{legacy_path}?mode=ro", uri=True, timeout=10)
            legacy.row_factory = sqlite3.Row
            rows = legacy.execute(
                "SELECT url, title_en, source_name, status, published_at, published_ts FROM items "
                "WHERE COALESCE(published_ts,0) >= ? AND status IN ('posted','skipped','failed')", (cutoff,)).fetchall()
            legacy.close()
        except sqlite3.DatabaseError as exc:
            log.warning("Legacy DB unreadable (%s); skipping import", type(exc).__name__)
            self.set_meta("legacy_imported", "error")
            return 0
        from .textutil import short_hash, url_identity
        for row in rows:
            ident = url_identity(row["url"] or "")
            if not ident:
                continue
            published = datetime.fromtimestamp(int(row["published_ts"] or 0), tz=timezone.utc)
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO articles(item_id, source, url, url_identity, title, summary, published_at, "
                "entities_json, event_type, kind, story_id, status, prefilter_reason, first_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (short_hash(ident, 24), row["source_name"], row["url"], ident, row["title_en"], "", iso(published), "[]",
                 "other", "news", "", f"legacy_{row['status']}", "", iso(utcnow())))
            imported += cur.rowcount
        self.set_meta("legacy_imported", iso(utcnow()))
        self.conn.commit()
        log.info("Imported %d already-handled item(s) from bot v1 state", imported)
        return imported


def legacy_sync(legacy_path: Path, items: list[FeedItem], status: str, wp_post_id: int | None = None) -> int:
    """Record v2 decisions in bot v1's news_cache.db so a rollback to v1 does not re-post them."""
    if not items:
        return 0
    conn = sqlite3.connect(str(legacy_path), timeout=30)
    try:
        conn.executescript(LEGACY_DDL)
        now = iso(utcnow())
        written = 0
        for item in items:
            hid = legacy_url_hash(item.url)
            ts = int(item.published_at.timestamp())
            cur = conn.execute(
                "INSERT OR IGNORE INTO items(id, source_name, title_en, title_norm, snippet_en, url, published_at, "
                "published_ts, created_at, status, wp_post_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (hid, item.source, item.title, legacy_title_norm(item.title), item.summary[:500], item.url,
                 iso(item.published_at), ts, now,
                 status, wp_post_id))
            if not cur.rowcount:
                conn.execute("UPDATE items SET status=?, wp_post_id=COALESCE(?, wp_post_id) WHERE id=? "
                             "AND status IN ('pending','failed')", (status, wp_post_id, hid))
            written += 1
        conn.commit()
        return written
    finally:
        conn.close()
