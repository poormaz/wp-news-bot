"""Areas 16-17: API rate limits, quota/auth errors, budget exhaustion, usage accounting."""

import httpx2
import openai
import pytest
from conftest import FakeOpenAI, make_settings, response, usage

from newsbot.llm import BudgetExceeded, Ledger, LLMBadOutput, LLMClient, LLMTransient, LLMUnavailable
from newsbot.state import StateStore

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}


def api_error(cls, status, body=None, headers=None, message="error"):
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    resp = httpx2.Response(status, request=request, headers=headers or {})
    return cls(message, response=resp, body=body)


def make(repo, handler, **settings):
    s = make_settings(repo, **settings)
    fake = FakeOpenAI({"probe": handler})
    sleeps = []
    store = StateStore(":memory:")
    ledger = Ledger(s, store, "run-1")
    return LLMClient(s, ledger, client=fake, sleep=sleeps.append), fake, sleeps, ledger, store


def call(llm):
    return llm.call_json("probe", "sys", "user", "probe", SCHEMA, 500, "low")


def test_usage_and_cost_come_from_the_response(repo):
    llm, fake, _, ledger, store = make(repo, lambda kw: response({"ok": True}, usage=usage(10000, 4000, 2000, 500)))
    assert call(llm) == {"ok": True}
    rec = ledger.calls[0]
    assert (rec.input_tokens, rec.cached_tokens, rec.output_tokens, rec.reasoning_tokens) == (10000, 4000, 2000, 500)
    # gpt-6-luna: 6000*0.10 + 4000*0.01 + 2000*0.50 per 1M
    assert rec.cost_usd == pytest.approx((6000 * 0.10 + 4000 * 0.01 + 2000 * 0.50) / 1e6)
    assert rec.model_served == "gpt-6-luna-2026-09-22"
    assert fake.calls[0]["reasoning"] == {"effort": "low"}
    assert fake.calls[0]["text"]["format"]["strict"] is True and fake.calls[0]["store"] is False
    assert store.cost_since(__import__("newsbot.models", fromlist=["utcnow"]).utcnow().replace(year=2000)) > 0


def test_rate_limit_is_retried_with_backoff_then_succeeds(repo):
    attempts = {"n": 0}

    def handler(kw):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return api_error(openai.RateLimitError, 429, headers={"retry-after": "3"})
        return {"ok": True}
    llm, _, sleeps, ledger, _ = make(repo, handler)
    assert call(llm) == {"ok": True}
    assert sleeps == [3.0, 3.0] and ledger.requests == 3


def test_rate_limit_retries_are_bounded(repo):
    llm, _, sleeps, ledger, _ = make(repo, lambda kw: api_error(openai.RateLimitError, 429))
    with pytest.raises(LLMTransient):
        call(llm)
    assert len(sleeps) == llm.settings.openai_max_retries
    assert ledger.requests == llm.settings.openai_max_retries + 1


def test_quota_exhaustion_disables_model_immediately(repo):
    llm, _, sleeps, _, _ = make(repo, lambda kw: api_error(openai.RateLimitError, 429,
                                                          body={"code": "insufficient_quota"}))
    with pytest.raises(LLMUnavailable):
        call(llm)
    assert sleeps == [] and not llm.available
    with pytest.raises(LLMUnavailable):
        call(llm)


def test_auth_error_and_unknown_model(repo):
    llm, _, _, _, _ = make(repo, lambda kw: api_error(openai.AuthenticationError, 401))
    with pytest.raises(LLMUnavailable):
        call(llm)
    llm, _, _, _, _ = make(repo, lambda kw: api_error(openai.NotFoundError, 404))
    with pytest.raises(LLMUnavailable):
        call(llm)


def test_explicit_fallback_model_is_used_and_reported(repo):
    def handler(kw):
        return api_error(openai.NotFoundError, 404) if kw["model"] == "gpt-6-luna" else \
            response({"ok": True}, model="gpt-4o-mini-2024-07-18")
    llm, fake, _, ledger, _ = make(repo, handler, openai_fallback_model="gpt-4o-mini")
    assert call(llm) == {"ok": True}
    assert llm.fallback_used and llm.model == "gpt-4o-mini"
    assert ledger.totals()["models_served"] == ["gpt-4o-mini-2024-07-18"]


def test_reasoning_param_rejection_retries_without_it(repo):
    def handler(kw):
        return api_error(openai.BadRequestError, 400, message="Unsupported parameter: 'reasoning.effort' is not "
                         "supported with this model.") if "reasoning" in kw else {"ok": True}
    llm, fake, _, _, _ = make(repo, handler)
    assert call(llm) == {"ok": True}
    assert "reasoning" in fake.calls[0] and "reasoning" not in fake.calls[1]


def test_incomplete_output_retries_once_with_more_tokens(repo):
    outputs = iter([response({"ok": True}, status="incomplete"), response({"ok": True})])
    llm, fake, _, _, _ = make(repo, lambda kw: next(outputs))
    assert call(llm) == {"ok": True}
    assert fake.calls[1]["max_output_tokens"] == 1000


def test_other_bad_requests_are_not_retried(repo):
    llm, fake, _, _, _ = make(repo, lambda kw: api_error(openai.BadRequestError, 400, message="Invalid schema"))
    with pytest.raises(Exception) as exc:
        call(llm)
    assert "rejected by the API" in str(exc.value) and len(fake.calls) == 1


def test_invalid_json_is_bad_output(repo):
    from types import SimpleNamespace
    llm, _, _, _, _ = make(repo, lambda kw: SimpleNamespace(model="gpt-6-luna", output_text="not json",
                                                            status="completed", usage=usage()))
    with pytest.raises(LLMBadOutput):
        call(llm)


def test_run_budget_blocks_call_before_it_is_made(repo):
    llm, fake, _, _, _ = make(repo, lambda kw: {"ok": True}, max_run_cost_usd=0.0001)
    with pytest.raises(BudgetExceeded):
        llm.call_json("probe", "s", "u", "probe", SCHEMA, 9000, "low")
    assert fake.calls == []


def test_request_limit(repo):
    llm, fake, _, _, _ = make(repo, lambda kw: {"ok": True}, max_llm_requests_per_run=2)
    call(llm)
    call(llm)
    with pytest.raises(BudgetExceeded):
        call(llm)
    assert len(fake.calls) == 2


def test_daily_budget_uses_persisted_spend(repo):
    s = make_settings(repo, max_daily_cost_usd=0.01)
    store = StateStore(":memory:")
    store.record_llm_call("earlier", "compose", "gpt-6-luna", "gpt-6-luna", 0, 0, 0, 0, 0.0099, 0, "ok")
    llm = LLMClient(s, Ledger(s, store, "now"), client=FakeOpenAI({"probe": lambda kw: {"ok": True}}))
    with pytest.raises(BudgetExceeded):
        call(llm)


def test_missing_api_key_is_unavailable(repo, clean_env):
    clean_env.delenv("OPENAI_API_KEY")
    s = make_settings(repo)
    llm = LLMClient(s, Ledger(s))
    assert not llm.available
    with pytest.raises(LLMUnavailable):
        call(llm)
