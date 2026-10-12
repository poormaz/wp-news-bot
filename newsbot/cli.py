"""Command line entry point.

    python bot.py                      # mode from NEWSBOT_MODE (dry-run | draft | publish)
    python bot.py --mode dry-run       # full pipeline, no WordPress writes
    python bot.py --mode draft         # canary: at most one WordPress DRAFT, never public
    python bot.py --samples 4 --compare-legacy   # editorial samples (no WordPress writes)
    python bot.py --check              # read-only diagnostics
    python bot.py --validate-config --mode publish
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import MODES, ConfigError, load_settings
from .logsetup import setup_logging
from .pipeline import Pipeline, write_reports

log = logging.getLogger("newsbot")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="bot.py", description="Poormaz News Bot 2.0")
    parser.add_argument("--mode", choices=MODES, help="override NEWSBOT_MODE")
    parser.add_argument("--samples", type=int, default=0, help="write N editorial samples; implies dry-run")
    parser.add_argument("--compare-legacy", action="store_true", help="in samples mode, also run bot v1 per story")
    parser.add_argument("--check", action="store_true", help="read-only diagnostics")
    parser.add_argument("--validate-config", action="store_true", help="validate configuration and exit")
    parser.add_argument("--offline", action="store_true", help="no model calls (discovery/clustering only)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)
    mode = "dry-run" if (args.samples or args.check) else args.mode
    try:
        settings = load_settings(mode)
    except ConfigError as exc:
        setup_logging([])
        log.error("CONFIGURATION ERROR: %s", exc)
        return 2
    setup_logging(settings.secrets)
    log.info("Poormaz News Bot 2.0 | mode=%s | model=%s | run=%s", settings.mode, settings.openai_model,
             settings.run_id)

    if args.validate_config:
        problems = settings.validate(needs_llm=not args.offline)
        from .ingest import load_sources
        try:
            sources = load_sources(settings.sources_file)
            log.info("sources.yaml: %d source(s), %d enabled", len(sources), sum(1 for s in sources if s.enabled))
        except ConfigError as exc:
            problems.append(str(exc))
        for problem in problems:
            log.error("CONFIG: %s", problem)
        if not problems:
            log.info("Configuration valid for mode %s", settings.mode)
        return 2 if problems else 0

    if args.check:
        from .checks import run_checks
        result = run_checks(settings)
        settings.report_dir.mkdir(parents=True, exist_ok=True)
        (settings.report_dir / "check-report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                               encoding="utf-8")
        log.info("CHECK_REPORT_JSON=%s", json.dumps({k: v for k, v in result.items() if k != "feed_snapshot"},
                                                     ensure_ascii=False))
        log.info("FEED_SNAPSHOT_JSON=%s", json.dumps(result.get("feed_snapshot", []), ensure_ascii=False))
        probe = result.get("model_probe") or {}
        return 0 if probe.get("ok") else 3

    pipeline = Pipeline(settings, samples=args.samples, compare_legacy=args.compare_legacy, offline=args.offline)
    report = pipeline.run()
    if args.samples:
        write_samples(report, settings.report_dir / "samples")
    write_reports(report, settings.report_dir)
    log.info("RUN SUMMARY: %s", json.dumps({"counts": report.counts, "llm": {
        k: report.llm.get(k) for k in ("requests", "input_tokens", "cached_tokens", "output_tokens",
                                       "reasoning_tokens", "estimated_cost_usd", "models_served")},
        "elapsed_seconds": report.elapsed_seconds, "exit_code": report.exit_code}, ensure_ascii=False))
    return report.exit_code


def write_samples(report, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for index, story in enumerate(s for s in report.stories if s.get("article")):
        article = story["article"]
        html = story.get("article_html", "")
        name = f"{index + 1:02d}-{story['story_id']}"
        page = (f"<!doctype html><html lang=\"fa\" dir=\"rtl\"><meta charset=\"utf-8\">"
                f"<title>{html_lib.escape(article['title'])}</title>"
                f"<body style=\"max-width:760px;margin:auto;font-family:Tahoma,sans-serif;line-height:1.9\">"
                f"<p style=\"direction:ltr\">Decision: <b>{story['decision']}</b> — "
                f"{html_lib.escape('; '.join(story.get('reasons') or []))}</p>"
                f"<h1>{html_lib.escape(article['title'])}</h1>{html}</body></html>")
        (out_dir / f"{name}.html").write_text(page, encoding="utf-8")
        (out_dir / f"{name}.json").write_text(json.dumps(story, ensure_ascii=False, indent=2, default=str),
                                              encoding="utf-8")
        # Printed between markers so samples can be reviewed straight from the job log.
        log.info("SAMPLE_JSON_BEGIN %s", name)
        log.info("SAMPLE_JSON=%s", json.dumps({k: v for k, v in story.items()}, ensure_ascii=False, default=str))
        log.info("SAMPLE_JSON_END %s", name)


if __name__ == "__main__":
    sys.exit(main())
