# Bot v1 vs News Bot 2.0

Status of the evidence (2026-10-12):

* **Measured** — bot v1 behaviour in production, from the public GitHub Actions logs of runs
  #2140–#2145 (2026-10-06 → 2026-10-08), recorded in `tests/fixtures/v1_production_sample.json`.
* **Measured offline** — v2 behaviour in the automated test suite (mocked feeds, pages,
  WordPress and OpenAI; no live data).
* **Measured live** — the first v2 `samples` run (GitHub Actions run 37851559690, 2026-10-08):
  GPT-6 Luna, 16 requests, ≈ $0.01346 estimated, no WordPress writes. Its four `SAMPLE_JSON`
  records are kept as regression fixtures (`tests/fixtures/live_samples_37851559690.json`);
  the defects they revealed are listed in §4 and fixed.
* **Pending** — `dry-run` and the WordPress draft canary with the fixed pipeline.

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
| Duplicate prevention | fuzzy English-title similarity vs. DB; WordPress check compares the **English** headline with **Persian** titles (rarely matches) | event-level clustering (topic words, not just the game), persistent story IDs, WordPress fingerprints (marker, cited URLs, entities, event), crash adoption | `test_cluster.py` (5 outlets → 1 story; GTA 6 radio/merch/column → 3 stories), `test_state.py::test_cache_loss_is_recovered_from_wordpress`, `test_pipeline.py::test_crash_after_create_is_adopted_not_duplicated` |
| Usefulness | 2/5 recent posts were a guide and an opinion column | non-news filtered for free; triage + newsworthiness gate; developments allowed, repeats rejected | §1, `test_ingest.py`, `test_v1_regression.py` |
| Readability | fixed 450–650-word target encouraged padding (and failed short stories) | no word quota (ceiling proportional to verified facts); padding, repetition, restated points, filler about "the available information", "see also" sentences, clichés, untranslated English, keyword stuffing and repeated openings are rejected; one spelling per name | `test_factcheck.py`, `test_live_regressions.py` |
| Source diversity | 1 source per article; round-robin over 4 publications | all outlets covering a story, official pages linked from coverage, Steam data; official first-party feeds added; IGN/GameSpot for corroboration | `test_research.py`, `sources.yaml` |
| Internal links | generic "مطالب مرتبط" list (often empty), localization link for 3 games | links inside the text, only validated targets (200, no redirect, page mentions the game), varied anchors, no self/duplicate links, earlier coverage for developments | `test_enrich.py` |
| Images | copies the publisher's og:image (rights not established) | no publisher images; press-kit sources (opt-in), Poormaz's own localization artwork, or validated Poormaz fallback artwork (`NEWSBOT_FALLBACK_MEDIA_IDS`, see `docs/IMAGES.md`) | `test_images.py` |
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

## 4. Live samples run 37851559690: defects found and fixed

| Defect (live evidence) | Fix | Regression test |
|---|---|---|
| Forza Horizon 6, Game Pass and Monster Hunter Stories 3 got fact-check verdict `revise` but **passed** (the gate allowed one medium issue and treated anything but `reject` as a pass) | only verdict `pass` with no high/medium issue passes; `revise` → corrective rewrite → rule checks and model check run again; still failing after `NEWSBOT_MAX_REVISIONS` → rejected; `reject` final; fact check mandatory in draft/publish | `test_live_regressions.py` (the three live verdicts can never pass; revise→revise rejected; revise→pass publishes only the re-checked text; overrides cannot bypass) |
| GTA 6 radio story clustered with the GTA 6 merch article and the PC Gamer Jason column (3 "independent" groups) | event-level clustering by topic words; "other" no longer a wildcard; anti-chaining; stale mixed story IDs split | `test_cluster.py` (exact four headlines, any arrival order), `test_live_regressions.py::test_gta6_live_cluster_is_split_by_event` |
| "Forza Horizon 6Add to Wishlist…" as a source title; outlet shown as `store.playstation.com` | only the document's own `<title>` (not SVG icon titles); page chrome stripped; feed headline preferred; readable outlet names | `test_live_store_page_title_chrome_is_removed` |
| Uncertainties repeated what the body already said, under their own H2 | open questions already in the body are dropped; one paragraph unless ≥ 3 distinct | `test_live_uncertainties_…`, `test_live_forza_render_…` |
| Filler («اطلاعات موجود …», «طبق اطلاعات تأییدشده») and "see also" sentences («… را هم ببینید», «پوشش پیشین ما») around internal links; anchor «محتوای تازه بازی» | blocking, fixable rule checks; links only on a name contained in the target page's title; localization link is a fixed factual sentence; checker sees link targets | `test_live_filler_and_see_also_…`, `test_enrich.py` |
| «XBOX» and «Xbox» mixed in one article | `display_names` in `config/editorial.yaml` applied to the model's text and the fact sheet it reads | `test_live_name_spelling_is_made_consistent` |
| Pre-orders quoted from the PlayStation Store stayed "reported" | a claim stated verbatim (unhedged) by an official page is "confirmed" | `test_facts.py::test_reported_claim_stated_by_an_official_page_becomes_confirmed` |
| All four samples `media_id = 0` | validated, rotated Poormaz-owned fallback artwork (`NEWSBOT_FALLBACK_MEDIA_IDS`); check-mode image audit | `test_images.py`, `test_checks.py` |

## 5. Open weaknesses

1. **Live validation partly done** — the samples run confirmed GPT-6 Luna works with this key
   and measured cost (16 requests, ≈ $0.01346 for four samples). The stricter gate will reject
   more drafts and spend more on rewrites (up to 1 + 2 rewrites, each re-checked); this needs a
   repeat `samples` run, then `dry-run` and the draft canary.
2. **Lower volume by design** — Gamingbolt, DSOGaming and Wccftech items alone (medium
   authority) are only published with official confirmation or corroboration. Some runs, and
   possibly some days, will publish nothing. Tune with `NEWSBOT_SINGLE_SOURCE_MIN_AUTHORITY`,
   `allow_single_source_entities` or more corroborating sources.
3. **Lexical verification** — quote and value checks prove that text exists in a source, not
   that a paraphrase is semantically faithful; the model fact check covers semantics but is
   not infallible. Human review of the canary and early posts is recommended.
4. **Entity extraction is heuristic** — unknown new titles in Title Case headlines can be
   mis-segmented; mitigated by text similarity, aliases and learned names.
5. **Featured images** — until Poormaz-owned covers are uploaded and listed in
   `NEWSBOT_FALLBACK_MEDIA_IDS` (or `featured_media` is added to localization entries), posts
   have no featured image; the live samples all had `media_id = 0`. How the Gamxo theme renders
   that is unverified; `check` mode and the canary preview now show it (`docs/IMAGES.md`).
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
| Unit + mocked integration tests (258, incl. regressions built from the live samples) | passing locally (Python 3.11, same dependency versions as CI) |
| ruff, compileall, actionlint, config validation | passing locally |
| `tests` workflow on the pull request | runs automatically on GitHub |
| `samples` workflow run | run 37851559690 by the owner (before these fixes): succeeded; revealed the defects in §4 |
| `samples` (repeat) / `dry-run` / `draft` workflow runs | pending — to be started by the owner from the Actions tab (the development session cannot dispatch workflows: `403 Resource not accessible by integration`) |
| Live feeds, poormaz.com, OpenAI from the development container | **blocked** by the container's network policy |
