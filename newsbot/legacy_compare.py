"""Side-by-side measurement against bot v1 (samples mode only, never publishes).

Runs bot v1's own generator (legacy/bot_v1.py, unmodified) on the same story with the
model the production workflow uses for v1, records real token usage from the API
response, and scores the output with the same evidence checks used for v2.
"""

from __future__ import annotations

import importlib
import os
import time

import openai

from .config import Settings
from .factcheck import FABRICATION_PATTERNS, text_findings
from .models import FactSheet, SourceDoc, Story
from .textutil import clean_text, count_words, persian_ratio


class _RecordingOpenAI:
    """Drop-in for openai.OpenAI that records usage of chat.completions calls."""

    calls: list[dict] = []

    def __init__(self, api_key: str = "", **kwargs):
        self._client = openai.OpenAI(api_key=api_key, timeout=90, max_retries=1)
        outer = self

        class _Completions:
            def create(self, **kw):
                started = time.monotonic()
                resp = outer._client.chat.completions.create(**kw)
                usage = getattr(resp, "usage", None)
                details = getattr(usage, "prompt_tokens_details", None)
                _RecordingOpenAI.calls.append({
                    "model_served": getattr(resp, "model", ""),
                    "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                    "cached_tokens": int(getattr(details, "cached_tokens", 0) or 0),
                    "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                    "latency_ms": int((time.monotonic() - started) * 1000),
                })
                return resp

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def compare_with_legacy(story: Story, sheet: FactSheet, docs: list[SourceDoc], settings: Settings,
                        min_words: int, max_words: int, platform_names: set[str]) -> dict:
    legacy = importlib.import_module("legacy.bot_v1")
    model = os.getenv("LEGACY_OPENAI_MODEL", "gpt-4o-mini")
    legacy.OPENAI_MODEL = model
    legacy.OPENAI_API_KEY = settings.openai_api_key
    legacy.OpenAI = _RecordingOpenAI
    _RecordingOpenAI.calls = []

    item = sorted(story.items, key=lambda i: (-i.authority_rank, i.published_at))[0]
    doc = next((d for d in docs if d.url == item.url), docs[0] if docs else None)
    page_text = doc.text if doc else ""
    started = time.monotonic()
    result: dict = {"model_requested": model, "source_outlets_used": 1, "source": item.source}
    try:
        gen = legacy.openai_generate_fa_article(item.title, item.summary[:500], item.source, item.url,
                                                page_text=page_text)
        text = clean_text(gen.get("content_html_fa", ""))
        title = gen.get("title_fa", "")
        findings = text_findings(title, text, sheet, docs, platform_names)
        result.update({
            "title": title,
            "words": count_words(text),
            "persian_ratio": round(persian_ratio(text), 3),
            "unsupported_numbers": findings["unsupported_numbers"],
            "unsupported_names": findings["unsupported_names"],
            "fabrication_phrases": FABRICATION_PATTERNS.findall(text),
            "plain_text": text,
            "error": "",
        })
    except BaseException as exc:  # noqa: BLE001 - v1 raises ValueError/SystemExit on validation failure
        result.update({"error": f"{type(exc).__name__}: {str(exc)[:300]}"})
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    calls = list(_RecordingOpenAI.calls)
    price = settings.price_for(model)
    cost = None
    if price is not None:
        cost = sum(((c["input_tokens"] - c["cached_tokens"]) * price.input + c["cached_tokens"] * price.cached_input
                    + c["output_tokens"] * price.output) / 1_000_000 for c in calls)
    result.update({
        "api_requests": len(calls),
        "models_served": sorted({c["model_served"] for c in calls if c["model_served"]}),
        "input_tokens": sum(c["input_tokens"] for c in calls),
        "output_tokens": sum(c["output_tokens"] for c in calls),
        "cost_usd": round(cost, 6) if cost is not None else None,
    })
    return result
