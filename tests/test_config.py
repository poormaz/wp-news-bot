"""Area 15: configuration and missing secrets."""

import pytest
from conftest import ROOT, write_repo

from newsbot.cli import main
from newsbot.config import ConfigError, load_settings


def test_defaults_use_gpt6_luna_and_safe_mode(tmp_path, clean_env):
    write_repo(tmp_path)
    s = load_settings(repo_root=tmp_path)
    assert s.mode == "dry-run"                  # no NEWSBOT_MODE => never writes
    assert s.openai_model == "gpt-6-luna"
    assert s.max_posts_per_run == 1
    assert s.price_for("gpt-6-luna") is not None and s.price_for("gpt-6-luna-2026-09-22") is not None


def test_max_posts_is_capped_at_one(tmp_path, clean_env):
    write_repo(tmp_path)
    clean_env.setenv("MAX_POSTS_PER_RUN", "5")
    assert load_settings(repo_root=tmp_path).max_posts_per_run == 1
    clean_env.setenv("MAX_POSTS_PER_RUN", "0")
    assert load_settings(repo_root=tmp_path).max_posts_per_run == 0


@pytest.mark.parametrize("mode,missing", [
    ("publish", {"WP_BASE_URL", "WP_USERNAME", "WP_APP_PASSWORD", "OPENAI_API_KEY"}),
    ("draft", {"WP_BASE_URL", "WP_USERNAME", "WP_APP_PASSWORD", "OPENAI_API_KEY"}),
    ("dry-run", {"OPENAI_API_KEY"}),
])
def test_missing_secrets_are_named_without_values(tmp_path, clean_env, mode, missing):
    write_repo(tmp_path)
    clean_env.setenv("CAT_ALL", "2")
    problems = " ".join(load_settings(mode, repo_root=tmp_path).validate())
    for name in missing:
        assert name in problems


def test_secret_values_never_appear_in_validation(tmp_path, clean_env):
    write_repo(tmp_path)
    clean_env.setenv("WP_BASE_URL", "http://insecure.example")
    clean_env.setenv("WP_APP_PASSWORD", "supersecretpassword")
    problems = " ".join(load_settings("publish", repo_root=tmp_path).validate())
    assert "https" in problems and "supersecretpassword" not in problems


def test_invalid_values(tmp_path, clean_env):
    write_repo(tmp_path)
    clean_env.setenv("NEWSBOT_MODE", "yolo")
    with pytest.raises(ConfigError):
        load_settings(repo_root=tmp_path)
    clean_env.setenv("NEWSBOT_MODE", "dry-run")
    clean_env.setenv("NEWSBOT_MAX_RUN_COST_USD", "lots")
    with pytest.raises(ConfigError):
        load_settings(repo_root=tmp_path)


def test_unpriced_model_is_flagged(tmp_path, clean_env):
    write_repo(tmp_path)
    clean_env.setenv("OPENAI_MODEL", "some-unknown-model")
    clean_env.setenv("OPENAI_API_KEY", "sk-test-0123456789abcdefghij")
    assert any("No pricing" in p for p in load_settings(repo_root=tmp_path).validate())
    clean_env.setenv("NEWSBOT_PRICING_JSON", '{"some-unknown-model": {"input": 1, "output": 2}}')
    assert load_settings(repo_root=tmp_path).validate() == []


def test_cli_validate_config_exit_codes(clean_env, monkeypatch):
    monkeypatch.chdir(ROOT)
    assert main(["--validate-config", "--mode", "publish"]) == 2        # secrets missing
    clean_env.setenv("OPENAI_API_KEY", "sk-test-0123456789abcdefghij")
    assert main(["--validate-config", "--mode", "dry-run"]) == 0


def test_cli_publish_without_secrets_fails_cleanly(clean_env, tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    clean_env.setenv("NEWSBOT_REPORT_DIR", str(tmp_path / "out"))
    clean_env.setenv("NEWSBOT_STATE_DB", str(tmp_path / "state.db"))
    assert main(["--mode", "publish"]) == 2
    assert (tmp_path / "out" / "run-report.json").exists()
