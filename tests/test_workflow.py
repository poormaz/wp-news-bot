"""GitHub Actions workflow guarantees (schedule, limits, safety guards, rollback engine)."""

import re

import yaml
from conftest import ROOT

WF = ROOT / ".github" / "workflows"


def load(name):
    data = yaml.safe_load((WF / name).read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))       # PyYAML parses the bare key `on` as True
    return data


def steps(data):
    return {s.get("name"): s for s in data["jobs"]["run-bot"]["steps"]}


def test_schedule_dispatch_and_concurrency_preserved():
    data = load("newsbot.yml")
    assert data["on"]["schedule"] == [{"cron": "17 */2 * * *"}]
    assert "workflow_dispatch" in data["on"]
    assert data["concurrency"] == {"group": "wp-news-bot", "cancel-in-progress": False}
    assert data["permissions"] == {"contents": "read"}


def test_one_post_per_run_for_both_engines():
    s = steps(load("newsbot.yml"))
    assert s["Run News Bot 2.0"]["env"]["MAX_POSTS_PER_RUN"] == "1"
    assert s["Run legacy bot (rollback engine)"]["env"]["MAX_POSTS_PER_RUN"] == "1"


def test_model_configuration_is_explicit():
    s = steps(load("newsbot.yml"))
    assert s["Run News Bot 2.0"]["env"]["OPENAI_MODEL"] == "${{ vars.OPENAI_MODEL || 'gpt-6-luna' }}"
    assert s["Run legacy bot (rollback engine)"]["env"]["OPENAI_MODEL"] == "gpt-4o-mini"   # v1 exactly as before


def test_branches_cannot_publish():
    plan = steps(load("newsbot.yml"))["Resolve engine and mode"]["run"]
    assert 'if [ "$REF" != "refs/heads/main" ]' in plan
    assert 'mode="dry-run"' in plan and "legacy engine publishes directly" in plan


def test_state_is_saved_even_when_the_run_fails():
    s = steps(load("newsbot.yml"))
    assert s["Save News Bot 2.0 state"]["if"].startswith("always()")
    assert s["Save bot v1 database"]["if"].startswith("always()")
    assert s["Restore bot v1 database (news_cache.db)"]["with"]["restore-keys"].strip() == "wp-news-bot-db-"


def test_no_secret_is_echoed_or_dumped():
    text = (WF / "newsbot.yml").read_text(encoding="utf-8")
    assert not re.search(r"echo[^\n]*\$\{?\{?\s*(secrets\.|OPENAI_API_KEY|WP_APP_PASSWORD)", text)
    assert "-u \"$WP_USERNAME:$WP_APP_PASSWORD\"" not in text           # old verbose auth probe removed
    probe = steps(load("newsbot.yml"))["WordPress connectivity (status codes only)"]["run"]
    assert "-o /dev/null" in probe


def test_test_workflow_runs_lint_actionlint_and_pytest():
    data = load("tests.yml")
    runs = " ".join(s.get("run", "") for s in data["jobs"]["test"]["steps"])
    for needle in ("ruff check", "actionlint", "pytest", "--validate-config", "compileall"):
        assert needle in runs
    assert "pull_request" in data["on"]


def test_review_workflow_untouched():
    text = (WF / "reviews.yml").read_text(encoding="utf-8")
    assert "python reviews/review_bot.py" in text and "REVIEW_POST_STATUS: draft" in text
