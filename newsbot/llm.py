"""OpenAI access with budget limits, usage accounting and model verification.

* Responses API with strict JSON-schema output (Chat Completions as a configurable
  fallback API style).
* Token usage and the model actually served are taken from the API response; cost is
  computed from config/pricing.yaml. Nothing is estimated after the fact.
* Per-run and rolling-24h spend limits plus a request cap are checked before every
  call; a call that could exceed them is not made.
* Bounded retries for rate limits, timeouts and 5xx; immediate stop for auth, quota
  and unknown-model errors (unless an explicit fallback model is configured).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta

import openai

from .config import Settings
from .models import utcnow

log = logging.getLogger("newsbot.llm")


class LLMError(Exception):
    """Non-retryable model failure for this request."""


class LLMUnavailable(LLMError):
    """The model cannot be used in this run (auth, quota, unknown model)."""


class LLMTransient(LLMError):
    """Rate limit / timeout / server error that persisted through bounded retries."""


class LLMBadOutput(LLMError):
    """Invalid, truncated or schema-violating output."""


class BudgetExceeded(LLMError):
    """A spending or request limit would be exceeded."""


@dataclass
class CallRecord:
    stage: str
    model_requested: str
    model_served: str = ""
    api_style: str = "responses"
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: int = 0
    status: str = "ok"


@dataclass
class Ledger:
    settings: Settings
    store: object | None = None
    run_id: str = "local"
    calls: list[CallRecord] = field(default_factory=list)
    daily_spent_before: float = 0.0

    def __post_init__(self):
        if self.store is not None:
            try:
                self.daily_spent_before = self.store.cost_since(utcnow() - timedelta(hours=24))
            except Exception:  # noqa: BLE001 - ledger must not crash the run
                self.daily_spent_before = 0.0

    @property
    def requests(self) -> int:
        return len(self.calls)

    @property
    def cost(self) -> float:
        return sum(c.cost_usd or 0.0 for c in self.calls)

    @property
    def unpriced_calls(self) -> int:
        return sum(1 for c in self.calls if c.cost_usd is None and c.status == "ok")

    def totals(self) -> dict:
        served = sorted({c.model_served for c in self.calls if c.model_served})
        return {
            "requests": self.requests,
            "input_tokens": sum(c.input_tokens for c in self.calls),
            "cached_tokens": sum(c.cached_tokens for c in self.calls),
            "output_tokens": sum(c.output_tokens for c in self.calls),
            "reasoning_tokens": sum(c.reasoning_tokens for c in self.calls),
            "estimated_cost_usd": round(self.cost, 6),
            "unpriced_calls": self.unpriced_calls,
            "models_requested": sorted({c.model_requested for c in self.calls}),
            "models_served": served,
            "calls": [asdict(c) for c in self.calls],
        }

    def check(self, estimated_cost: float | None) -> None:
        s = self.settings
        if self.requests >= s.max_llm_requests_per_run:
            raise BudgetExceeded(f"request limit reached ({s.max_llm_requests_per_run} per run)")
        est = estimated_cost or 0.0
        if self.cost + est > s.max_run_cost_usd:
            raise BudgetExceeded(f"run budget ${s.max_run_cost_usd:.4f} would be exceeded "
                                 f"(spent ${self.cost:.4f}, next call up to ${est:.4f})")
        if self.daily_spent_before + self.cost + est > s.max_daily_cost_usd:
            raise BudgetExceeded(f"24h budget ${s.max_daily_cost_usd:.2f} would be exceeded")

    def record(self, rec: CallRecord) -> None:
        self.calls.append(rec)
        if self.store is not None:
            try:
                self.store.record_llm_call(self.run_id, rec.stage, rec.model_requested, rec.model_served,
                                           rec.input_tokens, rec.cached_tokens, rec.output_tokens,
                                           rec.reasoning_tokens, rec.cost_usd, rec.latency_ms, rec.status)
            except Exception:  # noqa: BLE001
                log.warning("Could not persist LLM call record")


def _usage_numbers(usage, api_style: str) -> tuple[int, int, int, int]:
    if usage is None:
        return 0, 0, 0, 0
    if api_style == "responses":
        inp = int(getattr(usage, "input_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        cached = int(getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0)
        reasoning = int(getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0)
    else:
        inp = int(getattr(usage, "prompt_tokens", 0) or 0)
        out = int(getattr(usage, "completion_tokens", 0) or 0)
        cached = int(getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0)
        reasoning = int(getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", 0) or 0)
    return inp, cached, out, reasoning


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    for name in ("retry-after-ms", "retry-after"):
        value = headers.get(name) if hasattr(headers, "get") else None
        if value:
            try:
                seconds = float(value) / (1000.0 if name.endswith("ms") else 1.0)
                return max(0.5, min(seconds, 60.0))
            except ValueError:
                continue
    return None


class LLMClient:
    BACKOFF = (2.0, 6.0, 15.0, 30.0, 45.0, 60.0)

    def __init__(self, settings: Settings, ledger: Ledger, client=None, sleep=time.sleep):
        self.settings = settings
        self.ledger = ledger
        self.model = settings.openai_model
        self.api_style = settings.openai_api_style
        self.reasoning_supported = True
        self.disabled_reason = ""
        self.fallback_used = False
        self._client = client
        self._sleep = sleep

    @property
    def available(self) -> bool:
        return not self.disabled_reason and bool(self.settings.openai_api_key or self._client is not None)

    def _openai(self):
        if self._client is None:
            if not self.settings.openai_api_key:
                raise LLMUnavailable("OPENAI_API_KEY is not set")
            self._client = openai.OpenAI(api_key=self.settings.openai_api_key, timeout=self.settings.openai_timeout,
                                         max_retries=0)
        return self._client

    def estimate_cost(self, chars: int, max_output_tokens: int) -> float | None:
        price = self.settings.price_for(self.model)
        if price is None:
            return None
        # Conservative: ~3 characters per token (Persian is denser than English) and the
        # full output allowance, which is the worst case the API can bill.
        est_in = chars / 3.0
        return (est_in * price.input + max_output_tokens * price.output) / 1_000_000

    def cost_of(self, model: str, inp: int, cached: int, out: int) -> float | None:
        price = self.settings.price_for(model) or self.settings.price_for(self.model)
        if price is None:
            return None
        cached = min(cached, inp)
        return ((inp - cached) * price.input + cached * price.cached_input + out * price.output) / 1_000_000

    def _invoke(self, system: str, user: str, schema_name: str, schema: dict, max_output_tokens: int,
                effort: str | None):
        client = self._openai()
        use_reasoning = bool(effort) and effort != "off" and self.reasoning_supported
        if self.api_style == "responses":
            kwargs = dict(
                model=self.model,
                instructions=system,
                input=user,
                max_output_tokens=max_output_tokens,
                text={"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}},
                store=False,
                prompt_cache_key=f"poormaz-newsbot-{schema_name}",
            )
            if use_reasoning:
                kwargs["reasoning"] = {"effort": effort}
            return client.responses.create(**kwargs)
        kwargs = dict(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_completion_tokens=max_output_tokens,
            response_format={"type": "json_schema",
                             "json_schema": {"name": schema_name, "schema": schema, "strict": True}},
            store=False,
        )
        if use_reasoning:
            kwargs["reasoning_effort"] = effort
        return client.chat.completions.create(**kwargs)

    def _parse(self, resp) -> tuple[str, bool]:
        if self.api_style == "responses":
            status = getattr(resp, "status", "completed")
            incomplete = status == "incomplete"
            return (getattr(resp, "output_text", "") or ""), incomplete
        choice = resp.choices[0]
        return (choice.message.content or ""), choice.finish_reason == "length"

    def call_json(self, stage: str, system: str, user: str, schema_name: str, schema: dict,
                  max_output_tokens: int, effort: str | None = None) -> dict:
        if self.disabled_reason:
            raise LLMUnavailable(self.disabled_reason)
        attempt = 0
        grew = False
        while True:
            self.ledger.check(self.estimate_cost(len(system) + len(user), max_output_tokens))
            rec = CallRecord(stage=stage, model_requested=self.model, api_style=self.api_style)
            started = time.monotonic()
            try:
                resp = self._invoke(system, user, schema_name, schema, max_output_tokens, effort)
            except openai.RateLimitError as exc:
                rec.status = "rate_limited"
                self._finish(rec, started)
                body = str(getattr(exc, "body", "") or exc).lower()
                if "insufficient_quota" in body or "billing" in body:
                    self.disabled_reason = "OpenAI quota exhausted (insufficient_quota)"
                    raise LLMUnavailable(self.disabled_reason) from exc
                attempt = self._backoff(attempt, exc, stage)
                continue
            except (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError) as exc:
                rec.status = type(exc).__name__
                self._finish(rec, started)
                attempt = self._backoff(attempt, exc, stage)
                continue
            except openai.BadRequestError as exc:
                rec.status = "bad_request"
                self._finish(rec, started)
                message = str(exc).lower()
                if self.reasoning_supported and effort and "reason" in message:
                    log.warning("Model %s rejected reasoning settings; retrying without them", self.model)
                    self.reasoning_supported = False
                    continue
                raise LLMError(f"{stage}: request rejected by the API (400)") from exc
            except openai.NotFoundError as exc:
                rec.status = "not_found"
                self._finish(rec, started)
                fallback = self.settings.openai_fallback_model
                if fallback and self.model != fallback:
                    log.warning("MODEL FALLBACK: %s is unavailable; switching to OPENAI_FALLBACK_MODEL=%s",
                                self.model, fallback)
                    self.model = fallback
                    self.fallback_used = True
                    self.reasoning_supported = True
                    continue
                self.disabled_reason = f"model {self.model!r} not found for this API key"
                raise LLMUnavailable(self.disabled_reason) from exc
            except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
                rec.status = "auth_error"
                self._finish(rec, started)
                self.disabled_reason = f"OpenAI rejected the credentials or model access ({type(exc).__name__})"
                raise LLMUnavailable(self.disabled_reason) from exc
            except openai.APIStatusError as exc:
                rec.status = f"http_{getattr(exc, 'status_code', 'error')}"
                self._finish(rec, started)
                raise LLMError(f"{stage}: API error {getattr(exc, 'status_code', '')}") from exc

            rec.model_served = str(getattr(resp, "model", "") or "")
            inp, cached, out, reasoning = _usage_numbers(getattr(resp, "usage", None), self.api_style)
            rec.input_tokens, rec.cached_tokens, rec.output_tokens, rec.reasoning_tokens = inp, cached, out, reasoning
            rec.cost_usd = self.cost_of(rec.model_served or self.model, inp, cached, out)
            text, incomplete = self._parse(resp)
            if incomplete:
                rec.status = "incomplete"
                self._finish(rec, started)
                if not grew and max_output_tokens < 32000:
                    max_output_tokens = min(32000, max_output_tokens * 2)
                    grew = True
                    log.warning("%s: output hit the token limit; retrying once with %d tokens", stage,
                                max_output_tokens)
                    continue
                raise LLMBadOutput(f"{stage}: model output truncated at max_output_tokens")
            self._finish(rec, started)
            if rec.model_served and not rec.model_served.startswith(self.model):
                log.warning("MODEL MISMATCH: requested %s but the API served %s", self.model, rec.model_served)
            log.info("LLM %s: model=%s in=%d (cached %d) out=%d (reasoning %d) cost=%s %dms", stage,
                     rec.model_served or self.model, inp, cached, out, reasoning,
                     f"${rec.cost_usd:.5f}" if rec.cost_usd is not None else "unpriced", rec.latency_ms)
            try:
                data = json.loads(text)
            except (TypeError, ValueError) as exc:
                raise LLMBadOutput(f"{stage}: model returned invalid JSON") from exc
            if not isinstance(data, dict):
                raise LLMBadOutput(f"{stage}: model returned a non-object JSON value")
            return data

    def _finish(self, rec: CallRecord, started: float) -> None:
        rec.latency_ms = int((time.monotonic() - started) * 1000)
        self.ledger.record(rec)

    def _backoff(self, attempt: int, exc: Exception, stage: str) -> int:
        if attempt >= self.settings.openai_max_retries:
            raise LLMTransient(f"{stage}: {type(exc).__name__} persisted after {attempt} retries") from exc
        wait = _retry_after(exc) or self.BACKOFF[min(attempt, len(self.BACKOFF) - 1)]
        log.warning("%s: %s; retry %d/%d in %.1fs", stage, type(exc).__name__, attempt + 1,
                    self.settings.openai_max_retries, wait)
        self._sleep(wait)
        return attempt + 1
