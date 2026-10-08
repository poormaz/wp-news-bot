"""Areas 3-4: story clustering and story identity across runs."""

from datetime import timedelta

import pytest
import yaml
from conftest import NOW, ROOT

from newsbot.cluster import Clusterer, assign_story_ids, story_title_tokens
from newsbot.entities import EntityExtractor, Gazetteer
from newsbot.ingest import annotate
from newsbot.models import FeedItem
from newsbot.state import StateStore


@pytest.fixture(scope="module")
def extractor():
    return EntityExtractor(Gazetteer(yaml.safe_load((ROOT / "config" / "entities.yaml").read_text(encoding="utf-8"))))


def make(extractor, idx, source, title, summary="", hours=1):
    item = FeedItem(item_id=f"i{idx}", source=source, source_type="publication", authority="medium",
                    role="primary", url=f"https://{source.lower().replace(' ', '')}.example/{idx}",
                    url_identity=f"{source}/{idx}", title=title, summary=summary,
                    published_at=NOW - timedelta(hours=hours))
    annotate(item, extractor)
    return item


def groups(clusters):
    return sorted(sorted(i.item_id for i in c) for c in clusters)


def test_five_outlets_one_story(extractor):
    items = [
        make(extractor, 1, "Gamingbolt", "Crimson Desert Launches March 19, 2026, Pre-Orders Now Live",
             "Pearl Abyss announced Crimson Desert will launch on March 19."),
        make(extractor, 2, "Wccftech", "Pearl Abyss Reveals Crimson Desert Release Date Alongside New Trailer",
             "Crimson Desert is coming March 19, 2026.", 2),
        make(extractor, 3, "PC Gamer", "New Crimson Desert trailer confirms March release", "", 3),
        make(extractor, 4, "IGN", "Crimson Desert Release Date Set for March 2026", "", 4),
        make(extractor, 5, "GameSpot", "Crimson Desert Finally Has A Launch Date", "", 5),
    ]
    clusters = Clusterer().cluster(items)
    assert groups(clusters) == [["i1", "i2", "i3", "i4", "i5"]]


def test_different_events_for_same_game_stay_separate(extractor):
    items = [
        make(extractor, 1, "Gamingbolt", "Crimson Desert Launches March 19, 2026"),
        make(extractor, 2, "DSOGaming", "Crimson Desert PC system requirements revealed", "", 4),
        make(extractor, 3, "Wccftech", "Crimson Desert Patch 1.03 Fixes Performance Issues", "", 30),
    ]
    assert len(Clusterer().cluster(items)) == 3


def test_different_games_same_event_stay_separate(extractor):
    items = [
        make(extractor, 1, "Gamingbolt", "Starfield Delayed to 2027"),
        make(extractor, 2, "Wccftech", "Hollow Knight: Silksong Delayed to 2027", "", 2),
    ]
    assert len(Clusterer().cluster(items)) == 2


def test_time_window_separates_old_coverage(extractor):
    items = [
        make(extractor, 1, "Gamingbolt", "Crimson Desert Launches March 19, 2026", hours=1),
        make(extractor, 2, "Wccftech", "Crimson Desert Launches March 19, 2026", hours=120),
    ]
    assert len(Clusterer(window_hours=72).cluster(items)) == 2


def test_headline_fuzzy_match_alone_is_not_enough(extractor):
    # Near-identical headline structure, different subjects.
    items = [
        make(extractor, 1, "Gamingbolt", "Ghost of Yotei Gets New Gameplay Trailer"),
        make(extractor, 2, "Wccftech", "Ghost Recon Wildlands Gets New Gameplay Trailer", "", 2),
    ]
    assert len(Clusterer().cluster(items)) == 2


def test_story_ids_are_stable_across_runs(tmp_path, extractor):
    store = StateStore(tmp_path / "state.db")
    first = [make(extractor, 1, "Gamingbolt", "Crimson Desert Launches March 19, 2026")]
    stories = assign_story_ids(Clusterer().cluster(first), {}, [])
    store.upsert_items(first)
    store.save_story(stories[0], story_title_tokens(stories[0].items))
    sid = stories[0].story_id

    # Next run: the same article plus new coverage from another outlet.
    second = first + [make(extractor, 2, "PC Gamer", "Crimson Desert Gets March 19 Release Date", "", 2)]
    item_story = {r["item_id"]: r["story_id"] for r in store.conn.execute("SELECT item_id, story_id FROM articles")}
    again = assign_story_ids(Clusterer().cluster(second), item_story, store.recent_stories(10))
    assert [s.story_id for s in again] == [sid]

    # Coverage arriving after the original items left the window still maps to the stored story.
    later = [make(extractor, 3, "IGN", "Crimson Desert Release Date Confirmed for March 19", "", 1)]
    matched = assign_story_ids(Clusterer().cluster(later), {}, store.recent_stories(10))
    assert matched[0].story_id == sid
    store.close()


def test_published_story_wins_when_cluster_merges_two_ids(extractor):
    items = [make(extractor, 1, "A", "Crimson Desert Launches March 19, 2026"),
             make(extractor, 2, "B", "Crimson Desert Release Date Is March 19, 2026", "", 2)]
    stored = [{"story_id": "aaaa", "status": "rejected"}, {"story_id": "bbbb", "status": "published"}]
    stories = assign_story_ids(Clusterer().cluster(items), {"i1": "aaaa", "i2": "bbbb"}, stored)
    assert stories[0].story_id == "bbbb"


def test_new_event_days_later_is_a_new_story(extractor):
    from newsbot.cluster import stored_story_similarity
    stored = {"story_id": "old", "primary_entity": "crimson desert", "entity_keys_json": '["crimson desert"]',
              "title_tokens_json": '["crimson", "desert", "launch", "march"]', "event_type": "release_date",
              "last_seen_at": (NOW - timedelta(days=6)).isoformat()}
    patch = [make(extractor, 9, "Wccftech", "Crimson Desert Patch 1.03 Fixes Performance Issues")]
    assert stored_story_similarity(patch, stored) == 0.0
    echo = [make(extractor, 10, "IGN", "Crimson Desert Launches March 19")]
    assert stored_story_similarity(echo, stored) >= 0.55
