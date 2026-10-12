# Configuration reference

All settings have safe defaults (`newsbot/config.py`). Names shared with bot v1 keep their
meaning; new settings are prefixed `NEWSBOT_`. In GitHub Actions, secrets come from
repository **secrets** and tunables from repository **variables** (Settings → Secrets and
variables → Actions). Validate with `python bot.py --validate-config --mode <mode>`.

The workflow passes **every** tunable below to the bot as `${{ vars.NAME }}` (no workflow-side
defaults), so setting or changing a repository variable takes effect on the next run; an unset
variable falls back to the code default listed here. `tests/test_workflow.py` fails if a setting
read by `newsbot/config.py` is not passed through. Only the file paths (`NEWSBOT_STATE_DB`,
`NEWSBOT_REPORT_DIR`) are fixed by the workflow.

## Secrets (GitHub Secrets)

| Name | Required in | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | all modes except `--offline` | model calls |
| `WP_BASE_URL` | draft, publish (reads in dry-run) | `https://poormaz.com` |
| `WP_USERNAME`, `WP_APP_PASSWORD` | draft, publish | WordPress application password |
| `RANKMATH_UPDATER_URL`, `RANKMATH_UPDATER_TOKEN` | legacy engine only | unchanged; v2 uses the Rank Math REST route like v1 did |

## Run control

| Variable | Default | Meaning |
|---|---|---|
| `NEWSBOT_MODE` | `dry-run` locally; workflow default `publish` on main | `dry-run` (no WordPress writes), `draft` (canary: at most one draft), `publish` |
| `NEWSBOT_ENGINE` (workflow) | `v2` | `legacy` runs `legacy/bot_v1.py` with its original settings |
| `MAX_POSTS_PER_RUN` | `1` | ceiling, hard-capped at 1 |
| `NEWSBOT_MAX_CANDIDATES` | `8` (1–40) | candidate backfilling: ranked stories are evaluated one after another until one passes or this many have been tried. Every candidate faces the same gate; the request, cost and time limits below can stop earlier |
| `NEWSBOT_MAX_RUN_SECONDS` | `840` (60–1500) | no new candidate is started after this many seconds (the job timeout is 20 minutes) |
| `NEWSBOT_CANDIDATE_RESERVE_USD` | `0.005` | a new candidate is started only if the run and 24 h budgets still hold at least this much **and** the worst-case cost of its extraction call; otherwise backfilling stops with a `backfill stopped: …` warning |
| `NEWSBOT_TRIAGE_BATCH` | `9` (3–20) | headlines triaged per model call; the next batch is triaged only when backfilling reaches it |

## Model and budget

| Variable | Default | Meaning |
|---|---|---|
| `OPENAI_MODEL` | `gpt-6-luna` | must have an entry in `config/pricing.yaml` |
| `OPENAI_FALLBACK_MODEL` | empty | used only if the main model returns "not found"; logged as MODEL FALLBACK |
| `NEWSBOT_OPENAI_API_STYLE` | `responses` | `chat` uses Chat Completions with JSON schema |
| `NEWSBOT_REASONING_EFFORT` | `low` | triage/extraction/fact check (`off` disables the parameter) |
| `NEWSBOT_COMPOSE_REASONING_EFFORT` | `medium` | article writing |
| `NEWSBOT_MAX_OUTPUT_TOKENS_{TRIAGE,EXTRACT,COMPOSE,CHECK}` | 2500 / 9000 / 9000 / 4000 | per-call caps (reasoning tokens count) |
| `NEWSBOT_MAX_RUN_COST_USD` | `0.10` | checked before each call against the worst case of that call |
| `NEWSBOT_MAX_DAILY_COST_USD` | `1.00` | rolling 24 h, from the state DB |
| `NEWSBOT_MAX_LLM_REQUESTS` | `30` (0–80) | API requests per run (retries count); a candidate is started only when at least 3 remain. A `--samples N` run raises it to `1 + N × (3 + 2 × NEWSBOT_MAX_REVISIONS)` (max 60) so every sample gets its full revision budget; the cost ceilings are unchanged and the raise is reported as a warning |
| `NEWSBOT_OPENAI_MAX_RETRIES` | `3` | retries for 429/5xx/timeouts |
| `NEWSBOT_OPENAI_TIMEOUT` | `90` | seconds per request |
| `NEWSBOT_PRICING_JSON` | empty | override pricing without a commit |

## Editorial policy

| Variable | Default | Meaning |
|---|---|---|
| `NEWSBOT_MIN_VERIFIED_CLAIMS` | `3` | minimum verified claims to write an article |
| `NEWSBOT_MIN_WORDS` / `NEWSBOT_MAX_WORDS` | `130` / `900` | there is **no word quota**: `NEWSBOT_MIN_WORDS` is only a soft target (warning `length.target`); only articles under 60 words (truncated output) are rejected. The ceiling is `min(NEWSBOT_MAX_WORDS, 120 + 75 × verified claims)` |
| `NEWSBOT_WORDS_PER_CLAIM` | `75` | words allowed per verified claim (anti-padding) |
| `NEWSBOT_ALLOW_SINGLE_SOURCE` | `1` | allow a core claim backed by one non-official source if its authority suffices (checked per core claim) |
| `NEWSBOT_SINGLE_SOURCE_LOW_RISK_MIN_AUTHORITY` | `medium` | authority a single source needs for **ordinary** news (release dates, PC ports, patches, trailers, interviews). Such an article must also name the outlet in the text and attribute the headline (`status.reported_attributed`, `status.title_attributed`) and pass the model fact check like any other. `low` authority is never enough alone |
| `NEWSBOT_SINGLE_SOURCE_MIN_AUTHORITY` | `high` | authority a single source needs for **high-risk** stories: rumors, speculative core claims, conflicting reports, and headlines about allegations, lawsuits, layoffs, closures, cancellations, acquisitions, leaks (`quality.HIGH_RISK_RE` plus `high_risk_patterns` in `config/editorial.yaml`). Below it, independent corroboration or an official source is required |
| `NEWSBOT_RUMOR_POLICY` | `corroborated` | `reject` or require ≥ 2 independent origins |
| `NEWSBOT_TRIAGE_LLM` | `1` | batched headline triage before research |
| `NEWSBOT_LLM_FACTCHECK` | `1` | model fact check after rule checks. **Required in `draft`/`publish`** (validation fails otherwise): an article reaches WordPress only after the checker returns verdict `pass` with no high/medium issue |
| `NEWSBOT_MAX_REVISIONS` | `2` (0–3) | corrective rewrites after failed rule checks or a `revise` verdict; after each rewrite the rule checks **and** the model check run again. Still failing after the last one → the story is rejected (re-evaluated only when new sources join). A `reject` verdict is final (no rewrite) |
| `NEWSBOT_MAX_STORY_AGE_HOURS` | `36` | older stories are not written |
| `NEWSBOT_CLUSTER_WINDOW_HOURS` | `72` | clustering window |
| `NEWSBOT_CLUSTER_THRESHOLD` | `0.55` | similarity needed to merge coverage |
| `NEWSBOT_STORY_DEDUP_DAYS` | `10` | look-back for published duplicates |
| `NEWSBOT_MAX_STORY_ATTEMPTS` | `3` | evaluations per story (new sources required to re-evaluate a rejection) |
| `NEWSBOT_RECONSIDER_AFTER_POLICY_CHANGE` | `1` | a fresh, unpublished story rejected under an older editorial policy / prompt version (`quality.policy_version()`) is evaluated once more under the current one. Never applies to duplicate, stale, blocked or security rejections, nor to published or drafted stories |
| `NEWSBOT_RETRY_BACKOFF_MINUTES` | `110` | backoff × failures for transient errors |

## Research and HTTP

| Variable | Default | Meaning |
|---|---|---|
| `FEED_ENTRIES_LIMIT` | `15` | newest entries read per feed |
| `NEWSBOT_MAX_SOURCE_DOCS` | `5` | outlets per story |
| `NEWSBOT_MAX_DOC_CHARS` | `7000` | characters per document sent to the model |
| `NEWSBOT_OFFICIAL_EXPANSION` | `1` | follow links to official domains |
| `NEWSBOT_MAX_OFFICIAL_FETCHES` | `2` | official pages per story |
| `NEWSBOT_STEAM_CONTEXT` | `1` | Steam store data and the game's official Steam announcements (`ISteamNews`, developer/publisher posts only; matched by title, at most 10 days old) |
| `NEWSBOT_STEAM_SEARCH` | `1` | find the Steam app by name when no source links to it (one public store-search request per candidate) |
| `NEWSBOT_RESPECT_ROBOTS` | `1` | robots.txt for article pages |
| `HTTP_TIMEOUT` | `20` | seconds |
| `NEWSBOT_USER_AGENT` | `Mozilla/5.0 (compatible; PoormazNewsBot/2.0; +https://poormaz.com/)` | honest bot UA |

## WordPress output

| Variable | Default | Meaning |
|---|---|---|
| `CAT_ALL`, `CAT_GAMING`, `CAT_HARDWARE`, `WP_CATEGORY_ID` | workflow: 2 / 18 / 19 | same category scheme as v1; reviews are never assigned by the news bot |
| `WP_TAGS_ENABLED`, `WP_TAGS_MAX` | `1`, `2` (max 3) | entity tags |
| `NEWSBOT_TAG_CREATE_POLICY` | `covered` | `never`, `covered` (create only if ≥ N posts mention the entity), `always` |
| `NEWSBOT_TAG_CREATE_MIN_POSTS` | `2` | N for `covered` |
| `SEO_INTERNAL_LINKS_ENABLED`, `SEO_INTERNAL_LINKS_MAX` | `1`, `3` (max 4) | contextual internal links |
| `NEWSBOT_VALIDATE_LINKS` | `1` | require HTTP 200 without redirect |
| `NEWSBOT_RANKMATH` | `1` | set Rank Math title/description/focus keyword |
| `NEWSBOT_SOURCE_LINKS_NOFOLLOW` | `1` | `rel=nofollow` on source links (v1 behaviour) |
| `NEWSBOT_IMAGE_POLICY` | `safe` | `safe` (press kits / Poormaz artwork / fallback), `none`, `legacy` (v1 copying; not recommended) |
| `NEWSBOT_FALLBACK_MEDIA_IDS` | empty | Poormaz-owned artwork already in the media library, used when nothing licensed is available: `123,456` or `gaming=123,456; hardware=789; default=321`. Rotated per story; each ID is checked (`GET /wp/v2/media/{id}` must return an image) before use. See `docs/IMAGES.md` |
| `NEWSBOT_FALLBACK_MEDIA_ID` | `0` | single fallback ID (older setting; added to `default`) |
| `NEWSBOT_REUSE_LOCALIZATION_IMAGE` | `1` | reuse `featured_media` of the matching localization page |
| `NEWSBOT_WP_RECENT_POSTS` | `40` | posts fingerprinted for dedup/recovery |

## State and files

| Variable | Default | Meaning |
|---|---|---|
| `NEWSBOT_STATE_DB` | `newsbot_state.db` | v2 state (Actions cache) |
| `DB_FILE` | `news_cache.db` | bot v1 DB: imported once, kept in sync for rollback |
| `NEWSBOT_LEGACY_SYNC` | `1` | write v2 decisions into `news_cache.db` |
| `NEWSBOT_WP_RECOVERY` | `1` | fingerprint recent WordPress posts each run |
| `NEWSBOT_REPORT_DIR` | `artifacts` | run report, summary, article previews |
| `SOURCES_FILE`, `LOCALIZATION_PAGES_FILE`, `MANUAL_LINKS_FILE` | defaults | file locations |

## YAML files

* `sources.yaml` — `name`, `feed`, optional `type` (`official`/`publication`), `authority`
  (`high`/`medium`/`low`), `role` (`primary`/`corroboration`), `enabled`, `images`
  (`none`/`press_kit`). A v1-style entry (name + feed) still works.
* `localization_pages.yaml` — see the comments in the file.
* `config/editorial.yaml` — overrides: `force_include_urls`, `block_entities`,
  `block_url_patterns`, `block_title_patterns`, `allow_single_source_entities`,
  `allow_rumor_entities`, `priority_entities`. Overrides never relax fact verification and
  can never bypass the model fact-check verdict. `high_risk_patterns` (regexes matched against
  headlines) adds topics that need `NEWSBOT_SINGLE_SOURCE_MIN_AUTHORITY` instead of the
  low-risk level. `display_names` maps spellings to the one
  used in articles (`XBOX: Xbox`); it is applied to the model's text and the fact sheet it
  reads, never to the source quotes that verification uses.
* `config/entities.yaml` — platforms, companies, franchises, games (with aliases), events,
  hardware patterns, generic words.
* `config/official_domains.yaml` — official and press-wire domains followed during research.
* `config/pricing.yaml` — USD per 1M tokens per model, with the source of each price.
* `manual_links.txt` — one URL per line; each is evaluated once (still fully verified).
