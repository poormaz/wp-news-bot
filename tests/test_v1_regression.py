"""Regression on real items bot v1 published (GitHub Actions runs #2140-#2145)."""

import json

import yaml
from conftest import ROOT

from newsbot.entities import EntityExtractor, Gazetteer, classify_kind, primary_entity
from newsbot.ingest import NON_NEWS_KINDS

DATA = json.loads((ROOT / "tests" / "fixtures" / "v1_production_sample.json").read_text(encoding="utf-8"))["items"]
BY_RUN = {item["run"]: item for item in DATA}
EXTRACTOR = EntityExtractor(Gazetteer(yaml.safe_load((ROOT / "config" / "entities.yaml").read_text(encoding="utf-8"))))


def test_guide_and_opinion_column_never_reach_the_model():
    assert classify_kind(BY_RUN[2141]["title"]) == "guide"      # v1 published it as news (post 7053)
    assert classify_kind(BY_RUN[2140]["title"]) == "opinion"    # v1 published it as news (post 7044)
    assert {classify_kind(BY_RUN[r]["title"]) for r in (2141, 2140)} <= NON_NEWS_KINDS


def test_alleged_single_source_story_is_treated_as_rumor():
    assert classify_kind(BY_RUN[2143]["title"]) == "rumor"      # v1 published it as fact (post 7060)


def test_subjects_are_extracted_for_dedup_and_linking():
    def primary(run):
        title = BY_RUN[run]["title"]
        return primary_entity(EXTRACTOR.extract(title), title).display
    assert primary(2144) == "Dragon's Dogma 2"
    assert primary(2145) == "Planet Zoo 2"
    assert primary(2142) == "Bloodborne"


def test_v1_failure_modes_recorded_in_fixture():
    posted = [i for i in DATA if i["v1"]["posted"]]
    assert all(i["v1"]["publisher_image_copied"] for i in posted)
    assert sum(i["v1"]["related_links"] == 0 for i in posted) == 3
