"""Check mode (read-only diagnostics) and the samples writer used for editorial review."""

import json

from conftest import SITE, FakeOpenAI, FakeWeb, FakeWordPress, build, make_settings, standard_web

from newsbot.checks import run_checks
from newsbot.cli import write_samples
from newsbot.httpclient import HttpClient
from newsbot.wordpress import WordPressClient


def test_check_mode_is_read_only_and_discovers_localization_pages(repo):
    web = FakeWeb()
    web.handler(lambda r: (200, [
        {"id": 55, "title": "دانلود زیرنویس فارسی Ironvale Chronicles", "url": f"{SITE}/ironvale-fa/",
         "subtype": "page"},
        {"id": 56, "title": "فارسی ساز Old Game", "url": f"{SITE}/old-game-fa/", "subtype": "product"},
        {"id": 57, "title": "اخبار هفته", "url": f"{SITE}/weekly/", "subtype": "post"},
    ], {}) if "/wp-json/wp/v2/search" in r.url else None)
    FakeWordPress(web)
    standard_web(web)
    web.add(f"{SITE}/ironvale-fa/", "<h1>زیرنویس فارسی Ironvale Chronicles</h1>")
    web.add(f"{SITE}/old-game-fa/", "", status=301, headers={"Location": f"{SITE}/new/"})
    s = make_settings(repo)
    session = web.session()
    http = HttpClient(s.user_agent, 5, True, session=session)
    wp = WordPressClient(SITE, "bot", "abcd efgh ijkl mnop", "dry-run", s.user_agent, session=session,
                         sleep=lambda x: 0)
    result = run_checks(s, http, openai_client=FakeOpenAI(), wp=wp)
    assert web.writes() == []
    found = {e["url"]: e for e in result["localization"]["discovered"]}
    assert set(found) == {f"{SITE}/ironvale-fa/", f"{SITE}/old-game-fa/"}            # unrelated post ignored
    assert found[f"{SITE}/ironvale-fa/"]["valid"] and found[f"{SITE}/ironvale-fa/"]["game_name_guess"] == \
        "Ironvale Chronicles"
    assert not found[f"{SITE}/old-game-fa/"]["valid"] and found[f"{SITE}/old-game-fa/"]["redirects_to"]
    assert result["model_probe"]["ok"] and result["model_probe"]["models_served"] == ["gpt-6-luna-2026-09-22"]
    assert result["wordpress"]["auth"]["ok"]
    assert result["wordpress"]["categories"]["CAT_GAMING"]["name"] == "Gaming"
    assert result["post_audit"].get("error") or "h1_count" in result["post_audit"]
    assert {f["source"] for f in result["feeds"]} == {"Gamingbolt", "Wccftech", "PC Gamer"}
    assert result["feed_snapshot"] and "entities" in result["feed_snapshot"][0]


def test_samples_mode_writes_reviewable_files(repo, tmp_path):
    pipeline, web, _, _ = build(repo, "dry-run")
    pipeline.samples = 3
    report = pipeline.run()
    assert web.writes() == []
    write_samples(report, tmp_path)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert any(f.endswith(".html") for f in files) and any(f.endswith(".json") for f in files)
    sample = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert sample["article"]["title"] and sample["facts"]["claims"] and sample["decision"]
    html = next(tmp_path.glob("*.html")).read_text(encoding="utf-8")
    assert 'dir="rtl"' in html and "منابع خبر" in html
