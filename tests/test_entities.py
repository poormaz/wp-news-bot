"""Area 2: entity normalization and topic extraction."""

import pytest
import yaml
from conftest import ROOT

from newsbot.entities import (
    EntityExtractor,
    Gazetteer,
    classify_event,
    classify_kind,
    classify_persian_event,
    events_compatible,
    latin_entity_keys,
    primary_entity,
)
from newsbot.textutil import entity_key, make_latin_slug


@pytest.fixture(scope="module")
def gazetteer():
    return Gazetteer(yaml.safe_load((ROOT / "config" / "entities.yaml").read_text(encoding="utf-8")))


@pytest.mark.parametrize("a,b", [
    ("The Witcher IV", "Witcher 4"),
    ("Tom Clancy's Ghost Recon®: Wildlands", "Tom Clancys Ghost Recon Wildlands"),
    ("Ghost of Yōtei", "ghost of yotei"),
    ("Final Fantasy VII Remake", "final fantasy 7 remake"),
    ("Pokémon Legends: Z-A", "pokemon legends z a"),
    ("Assassin’s Creed Shadows", "Assassin's Creed Shadows"),
])
def test_entity_key_equivalences(a, b):
    assert entity_key(a) == entity_key(b)


def test_entity_key_keeps_distinct_titles_distinct():
    assert entity_key("Dragon's Dogma 2") != entity_key("Dragon's Dogma")
    assert entity_key("Xbox Series X") != entity_key("Xbox Series S")
    assert entity_key("GTA V") != entity_key("GTA 6")      # single-letter numerals are not converted


def test_aliases_resolve_to_canonical_names(gazetteer):
    found = {e.display for e in gazetteer.match("GTA 6 trailer 3 rumored for next week")}
    assert "Grand Theft Auto VI" in found
    assert {e.display for e in gazetteer.match("New FF7 Rebirth patch")} >= {"Final Fantasy VII Rebirth"}


def test_ambiguous_aliases_need_capitalization(gazetteer):
    assert not any(e.display == "Nintendo Switch" for e in gazetteer.match("players can switch to the new mode"))
    assert any(e.display == "Nintendo Switch" for e in gazetteer.match("Coming to Switch next year"))


@pytest.mark.parametrize("title,primary,kind", [
    ("Ubisoft Confirms Assassin's Creed Hexe Delay to 2027", "Assassin's Creed Hexe", "game"),
    ("Battlefield 6 Season 2 Roadmap Revealed", "Battlefield 6", "game"),
    ("Ghost of Yotei's co-op Legends mode arrives on October 15", "Ghost of Yotei", "game"),
    ("Nvidia GeForce RTX 5090 Super Specs Leak Ahead of CES", "GeForce RTX 5090 Super", "hardware"),
    ("Planet Zoo 2 Adds Another 6 New Species to Its Roster", "Planet Zoo 2", "unknown"),
    ("Hollow Knight: Silksong Patch 1.0.3 Fixes Save Bug on Switch 2", "Hollow Knight: Silksong", "game"),
])
def test_primary_entity(gazetteer, title, primary, kind):
    entities = EntityExtractor(gazetteer).extract(title)
    found = primary_entity(entities, title)
    assert found is not None and found.display == primary and found.kind == kind


def test_platforms_and_events_are_not_topics(gazetteer):
    entities = EntityExtractor(gazetteer).extract("Crimson Desert Gets New Gameplay Trailer at Gamescom for PS5")
    kinds = {e.display: e.kind for e in entities}
    assert kinds["Gamescom"] == "event" and kinds["PlayStation 5"] == "platform"
    assert primary_entity(entities, "").display == "Crimson Desert"


def test_learned_entities_extend_gazetteer():
    g = Gazetteer({}, learned={"Ironvale Chronicles": "game"})
    assert [e.display for e in g.match("ironvale chronicles patch notes")] == ["Ironvale Chronicles"]


@pytest.mark.parametrize("title,event", [
    ("Crimson Desert Launches March 19, 2026", "release_date"),
    ("New Crimson Desert trailer confirms March release", "release_date"),
    ("Assassin's Creed Shadows Patch 1.1.5 Out Now", "patch_update"),
    ("Starfield delayed to 2027", "delay"),
    ("Crimson Desert PC system requirements revealed", "hardware_spec"),
    ("Elden Ring Nightreign DLC The Forsaken Hollows Launches December 4", "dlc_expansion"),
    ("Studio lays off 200 staff after acquisition", "business"),
])
def test_event_classification(title, event):
    assert classify_event(title) == event


def test_kind_classification():
    assert classify_kind("GTA 6 release date leaked by insider") == "rumor"
    assert classify_kind("Crimson Desert Launches March 19") == "news"


def test_event_compatibility():
    assert events_compatible("trailer", "release_date")
    assert not events_compatible("patch_update", "trailer")
    assert events_compatible("other", "delay")


def test_persian_titles():
    assert classify_persian_event("تاریخ انتشار Ghost of Yotei برای PC مشخص شد") == "release_date"
    assert classify_persian_event("آپدیت جدید Crimson Desert منتشر شد") in ("patch_update", "launch")
    assert classify_persian_event("بازی Starfield با تأخیر عرضه می‌شود") == "delay"


def test_latin_keys_from_persian_titles(gazetteer):
    keys = latin_entity_keys("تاریخ انتشار Ghost of Yotei برای PC مشخص شد", gazetteer)
    assert "ghost of yotei" in keys and "pc" not in keys


def test_latin_slug():
    assert make_latin_slug("Ghost of Yōtei Is Finally Coming to PC This Year") == "ghost-yotei-coming-pc"
    assert make_latin_slug("کاملاً فارسی") == ""
