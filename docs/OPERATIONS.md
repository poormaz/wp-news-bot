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
   * every enabled feed `ok = true` (disable failing ones in `sources.yaml`); this includes the
     corroboration feeds added in 2.1 (Eurogamer, VGC, Gematsu, Rock Paper Shotgun), whose URLs
     could not be fetched from the development environment;
   * `post_audit`: `h1_count`, `canonical_count`, `og_title_count` — record the theme's baseline
     (the bot adds no H1, canonical, OpenGraph or schema of its own);
   * `LOCALIZATION_DISCOVERY_JSON` → copy entries with `"valid": true` into
     `localization_pages.yaml` (see below);
   * `FEATURED_IMAGE_AUDIT_JSON` → the active theme, whether recent posts lack a featured
     image and how such a page renders, and whether every `NEWSBOT_FALLBACK_MEDIA_IDS` entry
     exists as an image (see `docs/IMAGES.md`).
3. **Samples**: `mode = samples` (default 4). Read the samples in the job log or in the
   `newsbot-report-*` artifact: facts with sources, gate decisions, Persian article, and the
   bot v1 article for the same story with its cost and evidence findings.
4. **Dry run**: `mode = dry-run`. The step summary lists every story decision with reasons,
   model, tokens, cost and elapsed time. `candidates` / `candidates_not_evaluated` and a
   `backfill stopped: …` warning show how far backfilling got and which limit ended it.
   Dry-run and samples rejections are stored as `dry_run_rejected`. They never block a later
   `draft`/`publish` run, and PR-branch runs use their own Actions cache anyway.
5. **Canary**: `mode = draft`. Open the draft in WordPress and verify facts against the
   listed sources, formatting (Gutenberg blocks, H2 only, no H1), internal links, Rank Math
   title/description/focus keyword, category and tags. Use *Preview* to see how the theme
   shows the post's featured image (or its absence) on the single-post page and in lists.
   Publish or delete it manually.
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
`NEWSBOT_MAX_LLM_REQUESTS` (30). Backfilling starts another candidate only when the remaining
run and 24 h budgets cover at least its worst-case extraction call and 3 requests remain, so a
run tries up to `NEWSBOT_MAX_CANDIDATES` (8) stories within the same $0.10 / $1.00 caps. When a
limit would be exceeded mid-story, the call is not made, the story is retried next run, and the
run ends normally. See `docs/COMPARISON.md` for estimates.

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
Bumping it, or `quality.EDITORIAL_POLICY_VERSION` when the gate rules change, lets fresh stories
rejected under the old version be evaluated once more
(`NEWSBOT_RECONSIDER_AFTER_POLICY_CHANGE`).

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| no posts for many runs | stories fail the gate | read `reasons` in the summary; common: a high-risk story with one medium source (`sources.risk` says why it is high-risk), single-source article not attributed (`status.*_attributed`, usually fixed by the revision), stale stories |
| `backfill stopped: candidate limit reached` with stories left | all tried candidates failed | raise `NEWSBOT_MAX_CANDIDATES`; the cost caps still apply |
| `backfill stopped: run budget too low…` | earlier candidates used the run budget | expected; the rest are considered next run |
| a repository variable seems ignored | integer outside its documented range (clamped to the range) or a typo in the name | every tunable is passed by the workflow (`tests/test_workflow.py`); non-numeric or unknown values fail `--validate-config` with exit code 2 |
| `model unavailable: model 'gpt-6-luna' not found` | account lacks access | set `OPENAI_FALLBACK_MODEL` or `OPENAI_MODEL` |
| `WordPress authentication check failed` | app password revoked | rotate `WP_APP_PASSWORD` |
| `no state DB restored` every run | cache evicted/unsaved | harmless (WordPress recovery); check the save step |
| duplicate published | should not happen; inspect `wp_recent` fingerprints in the run report and the marker in both posts | report as a bug, set `NEWSBOT_MODE=draft` meanwhile |
