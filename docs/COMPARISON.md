# Bot v1 vs News Bot 2.0

Status of the evidence (2026-10-08):

* **Measured** — bot v1 behaviour in production, from the public GitHub Actions logs of runs
  #2140–#2145 (2026-10-06 → 2026-10-08), recorded in `tests/fixtures/v1_production_sample.json`.
* **Measured offline** — v2 behaviour in the automated test suite (mocked feeds, pages,
  WordPress and OpenAI; no live data).
* **Pending** — live v2 measurements (real GPT-6 Luna output, tokens, cost, runtime, Persian
  quality). These require a `check`/`samples`/`dry-run` workflow run, which could not be
  started from the development session (see "Validation status" below). The `samples` mode
  with `--compare-legacy` produces the side-by-side live numbers automatically.

## 1. What bot v1 actually published (measured)

| Run | Picked item (source) | v1 result | v2 (deterministic stages) |
|---|---|---|---|
| #2141 | "Star Wars: Galactic Racer Guide – 15 Best Tips and Tricks" (Gamingbolt) | **published as news** (post 7053), publisher image copied, new tag | **filtered** before any API call (`guide`) |
| #2140 | "My most-played album of 2026? Sorry, Mitski—it's Final Fantasy 10: House Grooves…" (PC Gamer) | **published as news** (post 7044), image copied, new tag | **filtered** (`opinion`) |
| #2143 | "Switching From An RTX 4090 To iGPU … Allegedly Helped A User … Qwen3.8-27B" (Wccftech) | **published** (post 7060), image copied, 2 new tags | classified **rumor**; single origin → gate rejects (rumors need 2 independent origins) |
| #2142 | "Bloodborne Unofficial Native PC Port Available Now for Download" (DSOGaming) | **published** (post 7057), 2020 image copied | single medium-authority source → gate rejects unless corroborated or officially confirmed |
| #2145 | "Planet Zoo 2 Adds Another 6 New Species…" (Gamingbolt) | 2 API calls, **rejected** by v1's own 300-word rule (276 words) | single medium source → publishable only with official confirmation (frontier.co.uk is followed if linked) or corroboration; no word quota |
| #2144 | "Here's the Dragon's Dogma 2: Dark Arisen release time for your region" (PC Gamer) | **published** (post 7062), 2 API calls, image copied, new tag, 0 related links | eligible (high-authority single source); published only if its facts verify |

Across the five published posts: **5/5 single-source**, **5/5 publisher images copied** into the
Poormaz media library, **5 new tag archives** created, **0/5 localization links**, related-post
links 0/0/3/4/0. Two of five were not news at all (a guide and a personal music column).
Run-bot step time: 16–38 s (mean 23 s). v1 used 1.6 API calls per published post; it logs no
token usage, so v1 token/cost figures below are estimates.

## 2. Behaviour comparison

| Criterion | bot v1 | News Bot 2.0 | Evidence |
|---|---|---|---|
| Factual accuracy | model rewrites one page; only a word count and final punctuation were validated | claims carry verbatim quotes checked in code; altered numbers are rejected; every number and Latin name in the Persian text must exist in verified sources; model fact check; rumors must be framed | `test_facts.py`, `test_factcheck.py`, `test_pipeline.py::test_model_fact_check_can_veto` |
| Duplicate prevention | fuzzy English-title similarity vs. DB; WordPress check compares the **English** headline with **Persian** titles (rarely matches) | multi-signal clustering, persistent story IDs, WordPress fingerprints (marker, cited URLs, entities, event), crash adoption | `test_cluster.py` (5 outlets → 1 story; same game/different event stays separate), `test_state.py::test_cache_loss_is_recovered_from_wordpress`, `test_pipeline.py::test_crash_after_create_is_adopted_not_duplicated` |
| Usefulness | 2/5 recent posts were a guide and an opinion column | non-news filtered for free; triage + newsworthiness gate; developments allowed, repeats rejected | §1, `test_ingest.py`, `test_v1_regression.py` |
| Readability | fixed 450–650-word target encouraged padding (and failed short stories) | length proportional to verified facts; padding, repetition, clichés, untranslated English, keyword stuffing and repeated openings are rejected | `test_factcheck.py` |
| Source diversity | 1 source per article; round-robin over 4 publications | all outlets covering a story, official pages linked from coverage, Steam data; official first-party feeds added; IGN/GameSpot for corroboration | `test_research.py`, `sources.yaml` |
| Internal links | generic "مطالب مرتبط" list (often empty), localization link for 3 games | links inside the text, only validated targets (200, no redirect, page mentions the game), varied anchors, no self/duplicate links, earlier coverage for developments | `test_enrich.py` |
| Images | copies the publisher's og:image (rights not established) | no publisher images; press-kit sources (opt-in), Poormaz's own localization artwork, or a site fallback | `test_pipeline.py::test_publish_flow…` (`media_id == 0`) |
| Tags | a new tag for every new entity | existing tags; new tags only for entities already covered by ≥ 2 posts | `test_enrich.py::test_tag_policy_avoids_thin_archives` |
| Safety | publishes directly | dry-run / draft canary / publish (draft → verify → publish); write guard in code; branches cannot publish | `test_wordpress.py`, `test_workflow.py` |
| API cost control | none | per-run and 24 h budgets, request cap, usage and cost from every response | `test_llm.py` |
| Execution time | 16–38 s (measured) | more HTTP (several outlets, official pages) and 3–5 model calls with reasoning; estimated 1–3 min per evaluated story, bounded by an 18-minute step timeout | pending live measurement |

## 3. Cost per run (estimates — confirm with the first run report)

Assumptions: ~4 characters per token for English prompts, ~2.5 for Persian output;
`config/pricing.yaml` list prices (gpt-6-luna $0.10 in / $0.01 cached / $0.50 out per 1M;
gpt-4o-mini $0.15 / $0.60). Prompt sizes measured from the code: extraction prompt ≤ 39.6k
characters (6 documents at the 7k cap), compose system prompt 4.0k, check 1.4k, triage 1.1k.

| Call | Input tokens | Output tokens incl. reasoning | Est. cost (Luna) |
|---|---|---|---|
| triage (per run) | ~1.5k | ~1k | ~$0.0007 |
| fact extraction (per evaluated story; cached on retry) | 4k–10k | 2k–4k | $0.0014–0.003 |
| composition | ~3k | 4k–6k | $0.0023–0.0033 |
| fact check | ~3k | 1k–2k | $0.0008–0.0013 |
| **published article** (no revision) | 11k–17k | 8k–13k | **≈ $0.005–0.008** |
| bot v1 article (gpt-4o-mini, 1.6 calls) | ~4.5k per call | ~2k per call | ≈ $0.003–0.005 |

A typical v2 run (triage + 1–3 extractions + one article) is estimated at ≈ $0.005–0.015; the
hard ceiling is `NEWSBOT_MAX_RUN_COST_USD` ($0.10). Observed scheduling is ~5–6 runs per day
(GitHub delays the two-hourly cron), i.e. roughly $0.03–0.09 per day for v2 vs. ≈ $0.02–0.03
for v1. v2 spends more per run but less per *useful* article, and publishes fewer posts.

## 4. Open weaknesses

1. **Live validation outstanding** — GPT-6 Luna availability for this API key, real Persian
   quality, real token usage/cost and runtime have not been observed yet (blocked; see below).
2. **Lower volume by design** — Gamingbolt, DSOGaming and Wccftech items alone (medium
   authority) are only published with official confirmation or corroboration. Some runs, and
   possibly some days, will publish nothing. Tune with `NEWSBOT_SINGLE_SOURCE_MIN_AUTHORITY`,
   `allow_single_source_entities` or more corroborating sources.
3. **Lexical verification** — quote and value checks prove that text exists in a source, not
   that a paraphrase is semantically faithful; the model fact check covers semantics but is
   not infallible. Human review of the canary and early posts is recommended.
4. **Entity extraction is heuristic** — unknown new titles in Title Case headlines can be
   mis-segmented; mitigated by text similarity, aliases and learned names.
5. **Featured images** — with the safe policy most posts will have no featured image unless
   `NEWSBOT_FALLBACK_MEDIA_ID` is set to a site-owned image or `featured_media` is added to
   localization entries. How the Gamxo theme renders posts without one is unverified.
6. **Localization map** — still the 3 owner-provided entries; expansion requires one `check`
   run and review of `LOCALIZATION_DISCOVERY_JSON` (pages could not be reached from the
   development environment, and no URL was guessed).
7. **Legacy posts** — matching against older Persian posts relies on English names in their
   titles; posts without them are matched only by cited source URL.
8. **Existing ~1,961 posts are unchanged** (as required): their copied images and thin content
   remain.
9. **No guarantee of indexing or rankings** — the changes reduce quality risk; they do not
   guarantee Google indexing.

## Validation status

| Step | Status |
|---|---|
| Unit + mocked integration tests (209) | passing locally (Python 3.11, same dependency versions as CI) |
| ruff, compileall, actionlint, config validation | passing locally |
| `tests` workflow on the pull request | runs automatically on GitHub |
| `check` / `samples` / `dry-run` / `draft` workflow runs | **blocked**: dispatching workflows returned `403 Resource not accessible by integration` (the GitHub App lacks Actions write permission). Run them from the Actions tab as described in `docs/OPERATIONS.md`. |
| Live feeds, poormaz.com, OpenAI from the development container | **blocked** by the container's network policy |
