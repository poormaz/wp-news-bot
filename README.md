# Poormaz News Bot 2.0

Automated gaming-news editorial pipeline for [poormaz.com](https://poormaz.com/). It turns
English gaming coverage into **independently written, source-verified Persian news** and
publishes at most one article every two hours — and nothing at all when no story is good
enough.

```
RSS / official sources → normalization → entities → clustering & dedup → source research
→ structured facts → cross-source verification → newsworthiness → Persian composition
→ editorial fact check → quality gate → WordPress enrichment → (draft → verify → publish)
```

What changed from bot v1 (preserved unchanged in `legacy/bot_v1.py`):

| | bot v1 | News Bot 2.0 |
|---|---|---|
| Story selection | next RSS item of the next source in rotation | clustered stories ranked by coverage, official sources, freshness, relevance |
| Sources per article | 1 | all outlets covering the story + linked official pages + Steam data |
| Facts | model rewrites one page | claims with verbatim quotes, verified against the sources in code |
| Non-news (guides, deals, opinion) | published as news | filtered before any API call |
| Article length | forced 300–650 words | proportional to verified facts; padding rejected |
| Publishing | always one post if anything is pending | explainable quality gate; zero posts is normal |
| Images | publisher's og:image copied to the media library | no publisher images; licensed/press-kit or site-owned images only |
| Tags | a new tag per new entity | existing tags; new tag only for entities with ≥ 2 posts |
| Internal links | generic "related posts" block, often empty | validated localization pages + related coverage inside the text |
| Model | `gpt-4o-mini` (workflow override) | `gpt-6-luna` (Responses API, strict JSON schemas), configurable |
| Cost control | none | per-run / 24h budgets, request cap, usage + cost logged |
| Safety | publish only | dry-run · draft canary · publish (draft → verify → publish) |

## Quick start

```bash
pip install -r requirements.txt -c constraints.txt
cp .env.example .env            # optional, for local runs
python bot.py --validate-config --mode dry-run
python bot.py --mode dry-run    # full pipeline, no WordPress writes
python bot.py --samples 4 --compare-legacy   # editorial samples + v1 comparison (no writes)
python bot.py --check           # read-only diagnostics (model, feeds, WordPress, localization pages)
```

Tests: `pip install -r requirements-dev.txt && pytest` (no network or secrets needed).

## GitHub Actions

`.github/workflows/newsbot.yml` runs at minute 17 every two hours (UTC) and on manual dispatch.
Engine and mode come from workflow inputs or repository variables:

| Variable | Values | Default |
|---|---|---|
| `NEWSBOT_ENGINE` | `v2`, `legacy` | `v2` |
| `NEWSBOT_MODE` | `publish`, `draft`, `dry-run` | `publish` (main only; other branches are forced to dry-run) |
| `OPENAI_MODEL` | any priced model | `gpt-6-luna` |

`.github/workflows/tests.yml` runs ruff, actionlint, configuration validation and the test
suite on every pull request.

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — stages, data model, state, idempotency, security
- [Configuration reference](docs/CONFIGURATION.md) — every variable and YAML file
- [Operations](docs/OPERATIONS.md) — modes, canary, enabling production, monitoring, maintenance
- [Rollback](docs/ROLLBACK.md) — returning to bot v1 in one step
- [v1 vs v2 comparison](docs/COMPARISON.md) — measured differences and open weaknesses

## Repository layout

```
bot.py                  entry point (workflow runs `python bot.py`)
newsbot/                News Bot 2.0 package (one module per pipeline stage)
legacy/bot_v1.py        previous bot, byte-identical, for rollback
config/                 entities, official domains, editorial overrides, model pricing
sources.yaml            feeds (add/remove sources here)
localization_pages.yaml Poormaz subtitle/localization pages used for internal links
manual_links.txt        one-off article URLs to evaluate
reviews/                review bot (separate workflow, unchanged)
tests/                  unit + mocked integration tests
```
