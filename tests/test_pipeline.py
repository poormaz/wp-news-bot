"""Integration tests with mocked feeds, pages, WordPress and OpenAI.

Areas 4, 13, 14, 17, 18: cross-run duplicates, publishing failures, retry/idempotency,
budget exhaustion, dry-run safety.
"""

import copy
import json

from conftest import IRONVALE_ARTICLE, FakeOpenAI, build

from newsbot.pipeline import write_reports


def decisions(report):
    return {s["headline"][:30]: s["decision"] for s in report.stories}


def test_dry_run_full_pipeline_makes_no_wordpress_writes(repo):
    pipeline, web, fake_wp, ai = build(repo, "dry-run")
    report = pipeline.run()
    assert report.exit_code == 0
    assert web.writes() == []                                  # nothing POSTed anywhere
    assert fake_wp.posts == {}
    passed = [s for s in report.stories if s["decision"] == "dry_run_pass"]
    assert len(passed) == 1 and "Ironvale" in passed[0]["headline"]
    assert report.counts["prefiltered"] == 2                   # list + deal never reach the model
    assert report.counts["multi_source_clusters"] == 1 and report.counts["clustered_duplicates"] == 2
    assert ai.schema_calls() == ["triage", "fact_sheet", "article", "fact_check"]
    assert report.llm["requests"] == 4 and report.llm["models_served"] == ["gpt-6-luna-2026-09-22"]
    assert report.llm["estimated_cost_usd"] > 0
    assert (repo / "artifacts" / "articles").exists()


def test_one_post_per_run_is_a_maximum(repo):
    pipeline, web, fake_wp, ai = build(repo, "publish")
    report = pipeline.run()
    assert len(report.published) == 1 and len(fake_wp.posts) == 1
    assert ai.schema_calls().count("article") == 1             # stops after the first published story


def test_publish_flow_and_second_run_is_idempotent(repo):
    pipeline, web, fake_wp, ai = build(repo, "publish")
    report = pipeline.run()
    post = next(iter(fake_wp.posts.values()))
    assert post["status"] == "publish" and post["categories"] == [2, 18]
    assert "nbstory-" in post["content"] and "<h1" not in post["content"]
    assert report.published[0]["link"].endswith(f"/{post['slug']}/")
    story = next(s for s in report.stories if s["decision"] == "published")
    assert story["public_check"]["h1_count"] == 1 and story["verification"] == ["ok"]
    assert story["image"]["media_id"] == 0                      # no publisher image copied
    writes = web.writes()
    assert writes[0].endswith("/wp/v2/posts") and writes[-1].endswith(f"/wp/v2/posts/{post['id']}")

    again, web2, _, ai2 = build(repo, "publish", web=web)
    again.wp = pipeline.wp
    report2 = again.run()
    assert report2.published == [] and len(fake_wp.posts) == 1
    assert [s["decision"] for s in report2.stories if "Ironvale" in s["headline"]] == ["skipped"]


def test_canary_draft_mode_never_publishes(repo):
    pipeline, web, fake_wp, _ = build(repo, "draft")
    report = pipeline.run()
    assert [p["status"] for p in report.published] == ["draft"]
    assert all(p["status"] == "draft" for p in fake_wp.posts.values())
    assert not any(w.endswith(f"/posts/{pid}") for w in web.writes() for pid in fake_wp.posts)


def test_low_quality_article_is_rejected_and_never_posted(repo):
    bad = copy.deepcopy(IRONVALE_ARTICLE)
    bad["sections"][0]["paragraphs"].append({"text": "ما در پورماز بازی را تست کردیم و ۱۲۰ ساعت محتوا دارد.",
                                             "claim_ids": ["C1"]})
    ai = FakeOpenAI({"article": lambda kw: bad})
    pipeline, web, fake_wp, _ = build(repo, "publish", openai_client=ai, max_revisions=1)
    report = pipeline.run()
    assert fake_wp.posts == {} and report.published == []
    rejected = next(s for s in report.stories if "Ironvale" in s["headline"])
    assert rejected["decision"] == "rejected"
    assert any("fabrication.first_hand" in r for r in rejected["reasons"])
    assert ai.schema_calls().count("article") == 2              # one bounded revision attempt
    assert "fact_check" not in ai.schema_calls()                # rules failed: no extra model call


def test_revision_fixes_issue_then_publishes(repo):
    bad = copy.deepcopy(IRONVALE_ARTICLE)
    bad["sections"][0]["paragraphs"].append({"text": "بازی بیش از ۱۲۰ ساعت محتوا دارد.", "claim_ids": ["C1"]})
    outputs = iter([bad, IRONVALE_ARTICLE])
    ai = FakeOpenAI({"article": lambda kw: next(outputs)})
    pipeline, _, fake_wp, _ = build(repo, "publish", openai_client=ai)
    report = pipeline.run()
    assert len(report.published) == 1
    second_compose = [c for c in ai.calls if c["text"]["format"]["name"] == "article"][1]
    assert "revision_instructions" in second_compose["input"]


def test_model_fact_check_can_veto(repo):
    veto = {"issues": [{"paragraph": 1, "excerpt_fa": "x", "problem": "status_upgrade", "severity": "high",
                        "explanation": "presents rumor as fact"}],
            "language": {"fluent": True, "translationese": False, "problems": []}, "verdict": "reject"}
    ai = FakeOpenAI({"fact_check": lambda kw: veto})
    pipeline, _, fake_wp, _ = build(repo, "publish", openai_client=ai, max_revisions=0)
    report = pipeline.run()
    assert fake_wp.posts == {}
    assert any("factcheck.model_high_severity" in r for s in report.stories for r in s["reasons"])


def test_wordpress_create_failure_is_recorded_and_retryable(repo):
    pipeline, web, fake_wp, _ = build(repo, "publish")
    fake_wp.fail_create = (503, {"code": "down"}, {})
    report = pipeline.run()
    assert report.published == [] and fake_wp.posts == {}
    story = next(s for s in report.stories if "Ironvale" in s["headline"])
    assert story["decision"] == "transient"
    import sqlite3
    conn = sqlite3.connect(repo / "newsbot_state.db")
    status, retry_at = conn.execute("SELECT status, next_retry_at FROM stories WHERE story_id=?",
                                    (story["story_id"],)).fetchone()
    conn.close()
    assert status == "transient" and retry_at                  # queued for a later run, not dropped


def test_publish_step_failure_leaves_a_draft_not_a_public_post(repo):
    pipeline, web, fake_wp, _ = build(repo, "publish")
    fake_wp.fail_publish = (500, {"code": "error"}, {})
    report = pipeline.run()
    assert all(p["status"] == "draft" for p in fake_wp.posts.values())
    assert report.published == []


def test_crash_after_create_is_adopted_not_duplicated(repo):
    """Simulate a run that created the post but died before recording it."""
    pipeline, web, fake_wp, _ = build(repo, "publish")
    pipeline.run()
    story_id = next(iter(fake_wp.posts.values()))["content"].split("nbstory-")[1][:16]
    import sqlite3
    conn = sqlite3.connect(repo / "newsbot_state.db")
    conn.execute("UPDATE stories SET status='new', wp_post_id=NULL WHERE story_id=?", (story_id,))
    conn.execute("UPDATE publications SET status='creating', wp_post_id=NULL WHERE story_id=?", (story_id,))
    conn.commit()
    conn.close()
    again, _, _, ai = build(repo, "publish", web=web)
    again.wp = pipeline.wp
    report = again.run()
    assert len(fake_wp.posts) == 1 and report.published == []
    assert "article" not in ai.schema_calls()


def test_transient_failures_retry_later_with_backoff(repo):
    attempts = {"n": 0}

    def flaky(kw):
        attempts["n"] += 1
        import httpx2
        import openai
        req = httpx2.Request("POST", "https://api.openai.com/v1/responses")
        return openai.InternalServerError("boom", response=httpx2.Response(500, request=req), body=None)
    ai = FakeOpenAI({"fact_sheet": flaky})
    pipeline, web, _, _ = build(repo, "dry-run", openai_client=ai, openai_max_retries=1)
    report = pipeline.run()
    story = next(s for s in report.stories if "Ironvale" in s["headline"])
    assert story["decision"] == "transient" and "retry after" in story["reasons"][1]
    calls_first = attempts["n"]
    again, _, _, _ = build(repo, "dry-run", web=web, openai_client=ai, openai_max_retries=1)
    report2 = again.run()
    assert attempts["n"] == calls_first                         # still in backoff: not retried yet
    assert any("waiting to retry" in r for s in report2.stories for r in s["reasons"])


def test_budget_exhaustion_stops_cleanly(repo):
    pipeline, web, fake_wp, ai = build(repo, "publish", max_llm_requests_per_run=2)
    report = pipeline.run()
    assert fake_wp.posts == {}
    assert any(s["decision"] == "budget" for s in report.stories)
    assert report.exit_code == 0 and any("budget" in w for w in report.warnings)
    assert len(ai.calls) == 2


def test_missing_model_marks_run_failed_but_publishes_nothing(repo):
    import httpx2
    import openai
    req = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    nf = openai.NotFoundError("model not found", response=httpx2.Response(404, request=req), body=None)
    ai = FakeOpenAI({k: (lambda kw, e=nf: e) for k in ("triage", "fact_sheet", "article", "fact_check")})
    pipeline, web, fake_wp, _ = build(repo, "publish", openai_client=ai)
    report = pipeline.run()
    assert fake_wp.posts == {} and report.exit_code == 3


def test_rejected_story_not_reevaluated_without_new_sources(repo):
    ai = FakeOpenAI({"fact_sheet": lambda kw: {**json.loads(json.dumps(__import__("conftest").ironvale_fact_sheet(
        kw["input"]))), "newsworthiness": {"is_news": False, "significance": "low", "reader_value": "",
                                            "reasons": ["minor"]}}})
    pipeline, web, _, _ = build(repo, "dry-run", openai_client=ai)
    pipeline.run()
    first = ai.schema_calls().count("fact_sheet")
    again, _, _, _ = build(repo, "dry-run", web=web, openai_client=ai)
    report = again.run()
    assert ai.schema_calls().count("fact_sheet") == first
    assert any("no new sources" in r for s in report.stories for r in s["reasons"])


def test_reports_are_written_without_secrets(repo):
    pipeline, *_ = build(repo, "dry-run")
    report = pipeline.run()
    write_reports(report, repo / "out")
    text = (repo / "out" / "run-report.json").read_text(encoding="utf-8")
    summary = (repo / "out" / "run-summary.md").read_text(encoding="utf-8")
    assert "sk-test" not in text and "abcd efgh" not in text
    assert "gpt-6-luna" in summary and "Estimated cost" in summary


def _existing(post_id, title, days_ago, categories=(2, 18), status="publish", content="<p>متن قدیمی</p>"):
    from datetime import timedelta

    from conftest import NOW
    return {"id": post_id, "title": title, "content": content, "status": status, "slug": f"p{post_id}",
            "link": f"https://poormaz.test/p{post_id}/", "categories": list(categories),
            "date_gmt": (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")}


def test_review_drafts_do_not_block_news(repo):
    review = _existing(500, "نقد و بررسی Ironvale Chronicles", 1, categories=(3,), status="draft")
    pipeline, _, fake_wp, _ = build(repo, "publish", wp_existing=[review], cat_reviews=3)
    report = pipeline.run()
    assert len(report.published) == 1


def test_same_event_recent_post_is_a_duplicate(repo):
    earlier = _existing(501, "تاریخ انتشار Ironvale Chronicles اعلام شد", 3,
                        content="<p>Ironvale Chronicles روز ۱۹ مارس ۲۰۲۷ منتشر می‌شود.</p>")
    pipeline, _, fake_wp, ai = build(repo, "publish", wp_existing=[earlier])
    report = pipeline.run()
    assert report.published == [] and "article" not in ai.schema_calls()
    assert any("already covers" in r for s in report.stories for r in s["reasons"])


def test_genuine_development_is_not_suppressed(repo):
    # Earlier post announced the game without a date; the new story adds the date.
    earlier = _existing(503, "تاریخ انتشار Ironvale Chronicles به زودی اعلام می‌شود", 3,
                        content="<p>سازنده گفته تاریخ انتشار به زودی اعلام می‌شود.</p>")
    pipeline, _, fake_wp, ai = build(repo, "publish", wp_existing=[earlier])
    report = pipeline.run()
    assert len(report.published) == 1
    story = next(s for s in report.stories if s["decision"] == "published")
    assert story["development"] is True
    compose_input = next(c["input"] for c in ai.calls if c["text"]["format"]["name"] == "article")
    assert "earlier_coverage" in compose_input           # earlier Poormaz post offered as a link


def test_compatible_event_days_later_is_not_suppressed(repo):
    trailer = _existing(502, "تریلر جدید Ironvale Chronicles منتشر شد", 5)
    pipeline, _, fake_wp, _ = build(repo, "publish", wp_existing=[trailer])
    assert len(pipeline.run().published) == 1


def test_triage_rejection_is_not_repeated(repo):
    def triage_no(kw):
        import re
        return {"stories": [{"id": sid, "is_news": False, "kind": "opinion", "significance": "low",
                             "relevance": "low", "reason": "column"}
                            for sid in re.findall(r'"id": "([0-9a-f]{16})"', kw["input"])]}
    ai = FakeOpenAI({"triage": triage_no})
    pipeline, web, _, _ = build(repo, "dry-run", openai_client=ai)
    pipeline.run()
    assert ai.schema_calls() == ["triage"]
    again, _, _, _ = build(repo, "dry-run", web=web, openai_client=ai)
    again.run()
    assert ai.schema_calls() == ["triage"]          # no second triage for unchanged stories


def test_all_feeds_down_is_reported_as_failure(repo):
    from conftest import FakeWeb
    web = FakeWeb()
    web.handler(lambda r: (503, "down", {}) if r.url.endswith(("/feed", "/rss")) else None)
    pipeline, _, fake_wp, ai = build(repo, "publish", web=web)
    report = pipeline.run()
    assert report.exit_code == 3 and fake_wp.posts == {} and ai.calls == []
