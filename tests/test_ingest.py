"""Area 1: RSS parsing, source configuration, rule-based pre-filtering, manual links."""

from datetime import timedelta

import pytest
import yaml
from conftest import NOW, ROOT, rss

from newsbot.config import ConfigError
from newsbot.entities import EntityExtractor, Gazetteer
from newsbot.ingest import load_sources, parse_feed, prefilter, read_manual_links
from newsbot.models import SourceConfig


@pytest.fixture
def extractor():
    return EntityExtractor(Gazetteer(yaml.safe_load((ROOT / "config" / "entities.yaml").read_text(encoding="utf-8"))))


SOURCE = SourceConfig(name="Gamingbolt", feed="https://gamingbolt.example/feed")


def test_parse_rss_basic_fields(extractor):
    xml = rss([{"title": "Ghost of Yotei Is Coming to PC in March 2027", "link": "https://x.example/a?utm_source=rss",
                "summary": "<p>Sony confirmed <b>the port</b>.</p>", "content": "<p>" + "Full text. " * 200 + "</p>"}])
    items = parse_feed(xml, SOURCE, extractor, now=NOW)
    assert len(items) == 1
    item = items[0]
    assert item.title == "Ghost of Yotei Is Coming to PC in March 2027"
    assert item.summary == "Sony confirmed the port ."
    assert item.url_identity == "x.example/a"          # tracking parameter removed
    assert "Full text." in item.content_html
    assert item.event_type == "release_date"
    assert any(e.display == "Ghost of Yotei" and e.kind == "game" for e in item.entities)
    assert item.published_at <= NOW


def test_parse_rss_dedupes_and_limits(extractor):
    xml = rss([{"title": f"Story {i}", "link": "https://x.example/same"} for i in range(3)] +
              [{"title": f"Other {i}", "link": f"https://x.example/{i}"} for i in range(20)])
    assert len(parse_feed(xml, SOURCE, extractor, now=NOW, limit=50)) == 21
    assert len(parse_feed(xml, SOURCE, extractor, now=NOW, limit=5)) == 3  # limit applies before dedup


def test_parse_rss_handles_garbage(extractor):
    assert parse_feed("<html>not a feed</html>", SOURCE, extractor, now=NOW) == []
    assert parse_feed("", SOURCE, extractor, now=NOW) == []
    xml = rss([{"title": "", "link": "https://x.example/1"}, {"title": "Ok title here", "link": ""}])
    assert parse_feed(xml, SOURCE, extractor, now=NOW) == []


def test_future_dates_are_clamped(extractor):
    xml = rss([{"title": "Future dated story about Crimson Desert", "link": "https://x.example/f",
                "published": NOW + timedelta(days=3)}])
    assert parse_feed(xml, SOURCE, extractor, now=NOW)[0].published_at == NOW


@pytest.mark.parametrize("title,reason", [
    ("The 25 best PC games of 2026", "non-news:list"),
    ("Save 40% on Elden Ring in this Steam deal", "non-news:deal"),
    ("How to find every collectible in Crimson Desert", "non-news:guide"),
    ("Battlefield 6 review: a return to form", "non-news:review"),
    ("I played 40 hours of Ghost of Yotei and I love it", "non-news:review"),
    ("Wordle answer today", "non-news:guide"),
])
def test_prefilter_rejects_non_news(extractor, title, reason):
    item = parse_feed(rss([{"title": title, "link": "https://x.example/n"}]), SOURCE, extractor, now=NOW)[0]
    assert prefilter(item, 36, {}, NOW) == reason


def test_prefilter_stale_blocked_and_ok(extractor):
    old = parse_feed(rss([{"title": "Crimson Desert Gets Release Date", "link": "https://x.example/o",
                           "published": NOW - timedelta(hours=50)}]), SOURCE, extractor, now=NOW)[0]
    assert prefilter(old, 36, {}, NOW) == "stale"
    fresh = parse_feed(rss([{"title": "Crimson Desert Gets Release Date", "link": "https://x.example/n"}]),
                       SOURCE, extractor, now=NOW)[0]
    assert prefilter(fresh, 36, {}, NOW) == ""
    assert prefilter(fresh, 36, {"block_entities": ["crimson desert"]}, NOW) == "blocked-entity"
    assert prefilter(fresh, 36, {"block_title_patterns": [r"release date"]}, NOW) == "blocked-title"


def test_sources_v1_format_still_loads(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text("sources:\n  - name: A\n    feed: 'https://a.example/feed'\n", encoding="utf-8")
    sources = load_sources(path)
    assert sources[0].type == "publication" and sources[0].role == "primary" and sources[0].enabled


def test_repository_sources_file_is_valid():
    sources = load_sources(ROOT / "sources.yaml")
    names = [s.name for s in sources]
    for legacy_name in ("Gamingbolt", "DSOGaming", "Wccftech", "PC Gamer"):
        assert legacy_name in names            # existing approved feeds are retained
    assert all(s.images == "none" for s in sources if s.type == "publication")


@pytest.mark.parametrize("body", [
    "sources: []\n",
    "sources:\n  - name: A\n",
    "sources:\n  - {name: A, feed: x, type: blog}\n",
    "sources:\n  - {name: A, feed: x}\n  - {name: A, feed: y}\n",
    "sources:\n  - {name: A, feed: x, images: anything}\n",
])
def test_sources_validation_errors(tmp_path, body):
    path = tmp_path / "sources.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_sources(path)


def test_manual_links(tmp_path):
    path = tmp_path / "manual_links.txt"
    path.write_text("# comment\nhttps://a.example/1\n\nnot-a-url\nhttps://a.example/1\nhttps://b.example/2\n",
                    encoding="utf-8")
    assert read_manual_links(path) == ["https://a.example/1", "https://b.example/2"]
    assert read_manual_links(tmp_path / "missing.txt") == []
