# Operations

## Modes

| Mode | WordPress | Model calls | Use |
|---|---|---|---|
| `check` | reads only | 1 tiny probe | after configuration changes; verifies model, feeds, WordPress auth, category IDs, localization pages, head markup of a live post |
| `samples` | reads only | per sample (+ bot v1 per sample) | editorial review: writes `artifacts/samples/*.html|json` and prints `SAMPLE_JSON=` lines |
| `dry-run` | reads only — writes are refused in code | full pipeline | rehearsal; `artifacts/articles/<story>.html` shows what would be posted |
| `draft` | at most one **draft** | full pipeline | canary; drafts are never published by the bot |
| `publish` | one post per run: draft → verify → publish | full pipeline | production (main branch only) |

Manual run: Actions → **wp-news-bot** → *Run workflow* → choose `mode` (and `engine`).
Runs started from a branch other than `main` can never publish: `publish` becomes `dry-run`
and the legacy engine is refused.

## First deployment checklist

1. **CI**: the `tests` workflow is green on the pull request.
2. **Check** (from the PR branch): `mode = check`. In the job log confirm:
   * `CHECK_REPORT_JSON` → `model_probe.ok = true`, `models_served` starts with `gpt-6-luna`
     (if not, set `OPENAI_MODEL` to an available model or set `OPENAI_FALLBACK_MODEL`);
   * `wordpress.auth.ok = true`, `categories` show CAT_ALL/CAT_GAMING/CAT_HARDWARE with names;
   * every enabled feed `ok = true` (disable failing ones in `sources.yaml`);
   * `post_audit`: `h1_count`, `canonical_count`, `og_title_count` — record the theme's baseline
     (the bot adds no H1, canonical, OpenGraph or schema of its own);
   * `LOCALIZATION_DISCOVERY_JSON` → copy entries with `"valid": true` into
     `localization_pages.yaml` (see below).
3. **Samples**: `mode = samples` (default 4). Read the samples in the job log or in the
   `newsbot-report-*` artifact: facts with sources, gate decisions, Persian article, and the
   bot v1 article for the same story with its cost and evidence findings.
4. **Dry run**: `mode = dry-run`. The step summary lists every story decision with reasons,
   model, tokens, cost and elapsed time.
5. **Canary**: `mode = draft`. Open the draft in WordPress and verify facts against the
   listed sources, formatting (Gutenberg blocks, H2 only, no H1), internal links, Rank Math
   title/description/focus keyword, category and tags. Publish or delete it manually.
6. **Production**: merge to `main`. Scheduled runs use `NEWSBOT_MODE` (default `publish`).
   To keep production in canary for a while, set the repository variable
   `NEWSBOT_MODE=draft`; remove it (or set `publish`) when satisfied.

## Monitoring

Each run writes a step summary and uploads `newsbot-report-<run>`:
`run-report.json` (all decisions, gate checks, research attempts, fact provenance, LLM calls
with tokens/cost), `run-summary.md`, `articles/*.html`.

Exit codes: `0` normal (including zero posts), `2` configuration error, `3` model unavailable
or WordPress failure (job turns red and notifies the owner), `1` crash.

Warnings worth acting on: `MODEL MISMATCH`, `MODEL FALLBACK`, `budget: …`, repeated
`FEED … unavailable`, `Internal link rejected`, `state DB was corrupt`.

## Cost

Token usage is taken from every API response and priced with `config/pricing.yaml`.
Budgets: `NEWSBOT_MAX_RUN_COST_USD` (0.10), `NEWSBOT_MAX_DAILY_COST_USD` (1.00),
`NEWSBOT_MAX_LLM_REQUESTS` (16). When a limit would be exceeded the call is not made, the story
is retried next run, and the run ends normally. See `docs/COMPARISON.md` for estimates.

## Maintenance

**Sources** — edit `sources.yaml`. Use `role: corroboration` for outlets that should help
verify but not start stories. Mark `type: official` only for first-party sources.
`images: press_kit` only when the source's images are licensed for press use.

**Localization pages** — run `mode = check`, copy valid discovered entries into
`localization_pages.yaml` (name = official English game name, add aliases). The bot only links
pages that answer 200 without redirect and mention the game.

**Editorial overrides** — `config/editorial.yaml`: force-include URLs, block entities or
patterns, allow single-source or rumor coverage for named entities, priority entities.
`manual_links.txt` remains supported (each URL processed once).

**Entities** — add aliases/abbreviations to `config/entities.yaml`; canonical game names from
verified fact sheets are learned automatically.

**Model** — set the repository variable `OPENAI_MODEL`; add its price to `config/pricing.yaml`
(validation refuses unpriced models so budgets stay meaningful).

**Prompts** — `newsbot/prompts.py`; bump `PROMPT_VERSION` (invalidates the fact cache).

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| no posts for many runs | stories fail the gate | read `reasons` in the summary; common: single medium source (consider `allow_single_source_entities` or adding corroborating sources), stale stories |
| `model unavailable: model 'gpt-6-luna' not found` | account lacks access | set `OPENAI_FALLBACK_MODEL` or `OPENAI_MODEL` |
| `WordPress authentication check failed` | app password revoked | rotate `WP_APP_PASSWORD` |
| `no state DB restored` every run | cache evicted/unsaved | harmless (WordPress recovery); check the save step |
| duplicate published | should not happen; inspect `wp_recent` fingerprints in the run report and the marker in both posts | report as a bug, set `NEWSBOT_MODE=draft` meanwhile |
