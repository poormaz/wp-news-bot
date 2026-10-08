# Architecture

News Bot 2.0 is a staged pipeline. Every stage has typed inputs and outputs
(`newsbot/models.py`), logs what it did, and ends a story with an explicit decision.
A run publishes **at most one** post; zero is a normal outcome.

```
                       ┌──────────────── state DB (SQLite, Actions cache) ────────────────┐
                       │ articles · stories · facts_cache · publications · llm_calls · …  │
                       └──────────────────────────────────────────────────────────────────┘
 feeds ─► 1 discovery ─► 2 normalize ─► 3 entities ─► 4 cluster/dedup ─► rank (+ triage call)
                                                                │
          WordPress recent posts ─► recovery fingerprints ──────┘
                                                                ▼
 5 research ─► 6 fact extraction (call) ─► 7 verification ─► 8 newsworthiness ─► early gate
                                                                                    │
 9 composition (call) ─► 10 fact check (rules + call) ─► 11 quality gate ─► 12 enrichment ─► 13 publish
```

## Stages

| # | Module | Input → output | Cost | Failure handling |
|---|---|---|---|---|
| 1 | `ingest.fetch_feed` | `sources.yaml` → `FeedItem`s | HTTP only | feed errors are logged; the run continues |
| 2 | `ingest.parse_feed` | RSS/Atom → normalized items (canonical URL, UTC date, plain summary, full text if the feed has it) | 0 | malformed entries skipped |
| 3 | `entities` | headline + summary → entities (games, companies, hardware, platforms, events), event type, editorial kind | 0 | rule-based; learns canonical names from verified fact sheets |
| — | `ingest.prefilter` | drops guides, deals, reviews, lists, opinion, sponsored, stale, blocked items | 0 | reason recorded per item |
| 4 | `cluster` | items from the last 72 h → `Story` clusters with persistent IDs | 0 | see "Deduplication" |
| — | `newsworthiness.rule_score` + `triage` | stories → ranked shortlist; one batched model call judges up to ~9 headlines | 1 small call | triage failure only skips the refinement |
| 5 | `research` | story → `SourceDoc`s: one per outlet, linked official pages, Steam store/announcement data | HTTP | blocked/removed/robots-disallowed pages are recorded and skipped (never bypassed); feed text is the fallback |
| 6 | `facts.FactDesk.extract` | documents → `FactSheet` (claims with verbatim quotes, status, importance, contradictions, origins, newsworthiness) | 1 call (cached) | invalid output → transient failure with backoff |
| 7 | `facts.verify_fact_sheet` | claims → verified claims | 0 | quotes and exact values must exist in the cited source; numbers never fuzzy; hedged wording downgrades "confirmed"; official sources win conflicts; unresolved conflicts are kept, not merged |
| 8 | `newsworthiness.assess`, `quality.source_checks` | early gate before writing | 0 | rejection recorded; no writing call is spent |
| 9 | `compose` | verified fact sheet + validated link candidates → Persian article (plain text + `[[L1|anchor]]`) → Gutenberg HTML built in code | 1 call | model cannot emit markup or URLs |
| 10 | `factcheck` | article → rule checks (numbers, names, quotes, fabrication, rumor framing, Persian quality, SEO) + model fact check | 0–1 call | one bounded revision with concrete feedback |
| 11 | `quality.decide` | all checks → `GateDecision` (pass/reject + reasons) | 0 | rejected stories retry only when new sources join |
| 12 | `enrich`, `images` | categories, tags (restrained), slug, featured image policy, internal links | WP reads | link targets must return 200 without redirect |
| 13 | `pipeline._publish` | create **draft** → Rank Math meta → re-read & verify → switch to publish | WP writes | verification failure leaves a draft; nothing half-public |

## Deduplication and story identity

* **Within a run**: clustering combines weighted entity overlap, character-3-gram TF-IDF
  similarity of title+summary (a local semantic-similarity stand-in), headline tokens,
  event-type compatibility and time proximity. Headline similarity alone never merges two
  different subjects.
* **Across runs**: every item keeps its `story_id`; new coverage of a known story joins it.
  Items that already left the window are matched to stored story signatures.
* **Against WordPress** (survives cache loss): each run fingerprints the 40 most recent posts
  (including drafts): v2 story marker (`<!-- poormaz-newsbot v2 nbstory-… -->`), cited source
  URLs, English entity names in the Persian title, event type from Persian keywords, numbers.
  A story is skipped if a post carries its marker, cites one of its URLs, or covers the same
  entity/event recently.
* **Developments are not suppressed**: a story about an already-covered entity is allowed when
  its event differs or it carries new numbers (dates, prices, versions). After fact extraction
  the core values are compared with the earlier post; if nothing is new it is rejected as a
  duplicate. Accepted developments link to the earlier Poormaz post.

## Verification model

Each claim has: text, category, exact value, status (`confirmed`/`reported`/`speculative`),
importance (`core`/`supporting`/`background`), and per-source verbatim quotes. After code
verification each claim is labelled:

* `official` — supported by an official page (developer, publisher, platform, Steam);
* `corroborated` — supported by ≥ 2 independent origins (copies of one press release or of one
  leak count once: text-overlap grouping + the model's origin attribution);
* `single_source` — one non-official source;
* `unverified` — quote or value not found; never shown to the writer.

The gate requires verified core news, ≥ 3 verified claims, and either an official source,
independent corroboration, or a single source of sufficient authority. Rumors require two
independent origins (configurable) and must be framed as rumors in title and text.

## Idempotency and recovery

* WordPress writes are never auto-retried (a retried POST can duplicate a post).
* A `publications` row (`creating` → `draft_created` → `published`) is written around each
  WordPress write; an interrupted run is resolved next time by searching for the story marker
  and adopting the existing post. A draft left by an interrupted **publish**-mode run is
  re-verified and published as the next run's single post (only if it never failed
  verification and the story is still fresh); canary drafts and drafts that failed
  verification stay drafts for an editor.
* After any WordPress post write whose outcome is uncertain (timeout, 5xx), the run stops
  processing further stories and exits with code 3, so one run can never create two posts.
* State lives in `newsbot_state.db` (Actions cache, saved even when the job fails). A missing or
  corrupt DB is rebuilt; WordPress fingerprints prevent duplicates in the meantime.
* Items bot v1 already handled are imported once from `news_cache.db`, and v2 writes its
  decisions back into `news_cache.db`, so a rollback to v1 does not re-post v2's stories.

## Security

* Source text is untrusted: `<`/`>` are neutralised so documents cannot close their delimiter,
  instruction-like sentences are removed before any prompt, prompts state that documents are
  data, outputs must match strict JSON schemas, quotes are verified in code, and the model has
  no way to choose WordPress status, categories, URLs or markup.
* WordPress client write guard: dry-run raises before any request; draft mode cannot set
  `publish`.
* Secrets: never in prompts, reports or artifacts; a logging filter redacts secret values,
  `Basic`/`Bearer` tokens and `sk-` keys.

## Models and cost

`newsbot/llm.py` uses the OpenAI Responses API (`store=false`, strict JSON schema, reasoning
effort per stage, prompt-cache key per stage). Usage (input, cached, output, reasoning tokens)
and the served model come from each response; cost uses `config/pricing.yaml`. Limits are
checked before every call: per-run cost, rolling-24h cost (from the state DB), request count.
Rate limits/timeouts/5xx are retried with bounded backoff (honouring `Retry-After`); quota,
auth and unknown-model errors stop model use for the run (optional explicit fallback model).
