"""Areas 3-4: story clustering and story identity across runs."""

import json
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


# ---------------------------------------------------------------------------
# Live regression (run 37851559690): event-level clustering for the same game
# ---------------------------------------------------------------------------
GTA_RADIO_GS = "GTA 6 Reveals 6 Radio Stations, Plus Podcasts You Can Listen To On Foot"
GTA_RADIO_GB = "GTA 6 Features Bad Bunny and Lana Del Rey as Radio Station Hosts"
GTA_MERCH = "GTA 6 Merch Revealed, Including T-Shirt With Alligator Wearing Sunglasses And Drinking A Cocktail"
GTA_JASON = "Jason may or may not appear fully nude in GTA 6, but either way we've now seen his dong"


@pytest.fixture(scope="module")
def gazetteer():
    return Gazetteer(yaml.safe_load((ROOT / "config" / "entities.yaml").read_text(encoding="utf-8")))


def gta_items(extractor):
    return [
        make(extractor, 1, "GameSpot", GTA_RADIO_GS, hours=8),
        make(extractor, 2, "Gamingbolt", GTA_RADIO_GB, hours=7),
        make(extractor, 3, "GameSpot", GTA_MERCH, hours=6),
        make(extractor, 4, "PC Gamer", GTA_JASON, hours=5),
    ]


@pytest.mark.parametrize("use_gazetteer", [True, False])
def test_gta6_radio_merch_and_column_are_three_stories(extractor, gazetteer, use_gazetteer):
    clusters = Clusterer(gazetteer=gazetteer if use_gazetteer else None).cluster(gta_items(extractor))
    # Same announcement from two outlets combines; the merch drop and the column stay separate.
    assert groups(clusters) == [["i1", "i2"], ["i3"], ["i4"]]


@pytest.mark.parametrize("order", [(0, 1, 2, 3), (3, 2, 1, 0), (2, 0, 3, 1), (1, 3, 0, 2)])
def test_gta6_clustering_does_not_depend_on_arrival_order(extractor, gazetteer, order):
    items = gta_items(extractor)
    clusters = Clusterer(gazetteer=gazetteer).cluster([items[i] for i in order])
    assert groups(clusters) == [["i1", "i2"], ["i3"], ["i4"]]


def test_gta6_radio_story_counts_only_its_own_outlets(extractor, gazetteer):
    stories = assign_story_ids(Clusterer(gazetteer=gazetteer).cluster(gta_items(extractor)), {}, [],
                               gazetteer=gazetteer)
    radio = next(s for s in stories if any(i.title == GTA_RADIO_GS for i in s.items))
    assert radio.outlets == ["GameSpot", "Gamingbolt"]           # no PC Gamer column inflating the count
    assert {i.title for i in radio.items} == {GTA_RADIO_GS, GTA_RADIO_GB}
    assert len({s.story_id for s in stories}) == 3


def test_same_announcement_from_different_outlets_still_combines(extractor, gazetteer):
    items = [
        make(extractor, 1, "Gamingbolt", "Forza Horizon 6 Launches on January 26th, 2027 for PS5, Pre-Orders Now "
             "Available", hours=6),
        make(extractor, 2, "IGN", "Forza Horizon 6 Is Finally Coming to PS5 in January Alongside First Expansion",
             hours=5),
    ]
    assert groups(Clusterer(gazetteer=gazetteer).cluster(items)) == [["i1", "i2"]]


def test_chaining_cannot_pull_an_unrelated_item_into_a_story(extractor, gazetteer):
    # B shares words with A and with C, but C is a different development than A and B.
    items = [
        make(extractor, 1, "GameSpot", "Crimson Desert Gets March 19 Release Date", hours=6),
        make(extractor, 2, "IGN", "Crimson Desert Release Date And Collector's Edition Revealed", hours=5),
        make(extractor, 3, "PC Gamer", "Crimson Desert Collector's Edition Statue Is Huge", hours=4),
    ]
    clusters = groups(Clusterer(gazetteer=gazetteer).cluster(items))
    assert ["i1", "i2"] in clusters and ["i3"] in clusters


def test_stale_mixed_story_id_is_kept_by_one_cluster_only(extractor, gazetteer):
    """State from the live run had all four GTA items under one story id; only the radio cluster keeps it."""
    clusters = Clusterer(gazetteer=gazetteer).cluster(gta_items(extractor))
    stale = {"i1": "radio123", "i2": "radio123", "i3": "radio123", "i4": "radio123"}
    stories = assign_story_ids(clusters, stale, [{"story_id": "radio123", "status": "dry_run_pass"}],
                               gazetteer=gazetteer)
    by_title = {i.title: s.story_id for s in stories for i in s.items}
    assert by_title[GTA_RADIO_GS] == by_title[GTA_RADIO_GB] == "radio123"
    assert len({by_title[GTA_MERCH], by_title[GTA_JASON], "radio123"}) == 3


def test_stored_story_matches_only_the_same_development(extractor, gazetteer):
    from newsbot.cluster import stored_story_similarity
    stored = {"story_id": "radio", "primary_entity": "grand theft auto 6",
              "entity_keys_json": '["grand theft auto 6"]',
              "title_tokens_json": json.dumps(story_title_tokens(gta_items(extractor)[:2])),
              "event_type": "announcement", "last_seen_at": NOW.isoformat()}
    merch = [make(extractor, 9, "IGN", GTA_MERCH)]
    assert stored_story_similarity(merch, stored, gazetteer) == 0.0
    more_radio = [make(extractor, 10, "IGN", "GTA 6 Radio Stations Include Hosts Bad Bunny And Lana Del Rey")]
    assert stored_story_similarity(more_radio, stored, gazetteer) >= 0.55
