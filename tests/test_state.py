"""Area 20: GitHub Actions state restoration, legacy compatibility, cache loss recovery."""

import sqlite3
from datetime import timedelta

from conftest import NOW, build

from newsbot.models import FeedItem
from newsbot.pipeline import RunReport, open_state
from newsbot.state import StateStore, legacy_sync
from newsbot.textutil import legacy_url_hash


def item(url="https://gb.example/a", title="Ironvale Chronicles Launches March 19"):
    return FeedItem("id1", "Gamingbolt", "publication", "medium", "primary", url, url, title, "s",
                    NOW - timedelta(hours=1))


def test_state_persists_between_processes(tmp_path):
    path = tmp_path / "state.db"
    store = StateStore(path)
    store.upsert_items([item()])
    store.set_meta("k", "v")
    store.close()
    again = StateStore(path)
    assert again.item_known("https://gb.example/a") and again.get_meta("k") == "v"
    assert not again.fresh


def test_corrupt_state_is_set_aside_and_rebuilt(tmp_path):
    path = tmp_path / "state.db"
    path.write_bytes(b"this is not a sqlite database at all" * 50)
    report = RunReport("r", "dry-run")
    store = open_state(path, report)
    assert store.integrity_ok()
    assert (tmp_path / "state.corrupt").exists()
    assert any("corrupt" in w for w in report.warnings)


def test_missing_state_is_reported(tmp_path):
    report = RunReport("r", "dry-run")
    open_state(tmp_path / "none.db", report).close()
    assert any("cache miss" in w for w in report.warnings)


def test_schema_migration_adds_columns(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE articles (item_id TEXT PRIMARY KEY, source TEXT, source_type TEXT, authority TEXT, "
                 "role TEXT, url TEXT, url_identity TEXT, title TEXT, summary TEXT, published_at TEXT, "
                 "entities_json TEXT, event_type TEXT, kind TEXT, story_id TEXT, status TEXT, prefilter_reason TEXT, "
                 "first_seen_at TEXT)")
    conn.commit()
    conn.close()
    store = StateStore(path)
    store.upsert_items([item()])
    assert store.recent_items(48)[0].url == "https://gb.example/a"


def test_legacy_db_import_and_sync(tmp_path):
    legacy = tmp_path / "news_cache.db"
    legacy_sync(legacy, [item()], "posted", 7062)
    conn = sqlite3.connect(legacy)
    row = conn.execute("SELECT id, status, wp_post_id, title_norm FROM items").fetchone()
    assert row[0] == legacy_url_hash("https://gb.example/a") and row[1] == "posted" and row[2] == 7062
    assert row[3] == "ironvale chronicles launches march"          # bot v1 dedup key format
    conn.close()
    store = StateStore(tmp_path / "state.db")
    assert store.import_legacy(legacy) == 1
    assert store.item_known("gb.example/a")
    assert store.import_legacy(legacy) == 0                        # once only


def test_cache_loss_is_recovered_from_wordpress(repo, tmp_path):
    """Run 1 publishes; the state cache is then lost; run 2 must not publish the story again."""
    p1, web, fake_wp, _ = build(repo, "publish", state_db=tmp_path / "a.db")
    assert p1.run().published
    assert len(fake_wp.posts) == 1
    p2, _, _, ai = build(repo, "publish", web=web, state_db=tmp_path / "fresh.db", legacy_sync=False)
    p2.wp = p1.wp
    report = p2.run()
    assert report.published == [] and len(fake_wp.posts) == 1
    decisions = {s["headline"][:20]: s["reasons"][0] for s in report.stories if "Ironvale" in s["headline"]}
    assert any("duplicate" in r for r in decisions.values())
    assert "article" not in ai.schema_calls()                      # dedup happened before any writing call
