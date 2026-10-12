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
| 4 | `cluster` | items from the last 72 h → event-level `Story` clusters with persistent IDs | 0 | see "Deduplication" |
| — | `newsworthiness.rule_score` + `triage` | stories → ranked shortlist; one batched model call judges up to ~9 headlines | 1 small call | triage failure only skips the refinement |
| 5 | `research` | story → `SourceDoc`s: one per outlet, linked official pages, Steam store/announcement data | HTTP | blocked/removed/robots-disallowed pages are recorded and skipped (never bypassed); feed text is the fallback |
| 6 | `facts.FactDesk.extract` | documents → `FactSheet` (claims with verbatim quotes, status, importance, contradictions, origins, newsworthiness) | 1 call (cached) | invalid output → transient failure with backoff |
| 7 | `facts.verify_fact_sheet` | claims → verified claims | 0 | quotes and exact values must exist in the cited source; numbers never fuzzy; hedged wording downgrades "confirmed"; official sources win conflicts; unresolved conflicts are kept, not merged |
| 8 | `newsworthiness.assess`, `quality.source_checks` | early gate before writing | 0 | rejection recorded; no writing call is spent |
| 9 | `compose` | verified fact sheet + validated link candidates → Persian article (plain text + `[[L2|name]]`) → Gutenberg HTML built in code | 1 call | model cannot emit markup or URLs; links only on a name that the target page's title contains; localization links are a fixed sentence built in code; open questions already stated in the body are dropped and rendered as one paragraph (a list only for ≥ 3) |
| 10 | `factcheck` | article → rule checks (numbers, names, quotes, fabrication, rumor framing, Persian quality, filler, "see also" sentences, restated points, link grounding, SEO) + model fact check | 1 call per draft | see "Fact-check gate" |
| 11 | `quality.decide` | all checks → `GateDecision` (pass/reject + reasons) | 0 | rejected stories retry only when new sources join |
| 12 | `enrich`, `images` | categories, tags (restrained), slug, featured image policy, internal links | WP reads | link targets must return 200 without redirect; media IDs must exist as images |
| 13 | `pipeline._publish` | create **draft** → Rank Math meta → re-read & verify → switch to publish | WP writes | verification failure leaves a draft; nothing half-public |

## Deduplication and story identity

* **Within a run**: a story is one *event*, not one game. Clustering combines weighted entity
  overlap, **topic similarity** (headline words left after removing the subject's names and
  aliases, platforms and generic verbs such as "reveals/features/includes"; release/launch/date
  wording is normalised), character-3-gram TF-IDF similarity of title+summary, event-type
  agreement and time proximity. Two items about the same game join only when their topic
  words overlap (≥ 0.25) or the text is near-identical; "other" is an unknown event type,
  never a wildcard. A newcomer must be the same event as at least half of a cluster's
  members, so chains (A~B, B~C) cannot pull an unrelated item in. Headline similarity alone
  never merges two different subjects. Example from the live run: "GTA 6 Reveals 6 Radio
  Stations…" + "GTA 6 Features Bad Bunny… as Radio Station Hosts" form one story; the GTA 6
  merch article and the PC Gamer column about Jason are two separate stories.
* **Story IDs**: one ID is never given to two clusters of a run. If earlier state grouped
  unrelated items under one ID, the cluster holding most of them keeps it and the others
  become new stories.
* **Across runs**: every item keeps its `story_id`; new coverage of a known story joins it.
  Items that already left the window are matched to stored story signatures.
* **Against WordPress** (survives cache loss): each run fingerprints the 40 most recent posts
  (including drafts): v2 story marker (`<!-- poormaz-newsbot v2 nbstory-… -->`), cited source
  URLs, English entity names in the Persian title, event type from Persian keywords, numbers.
  A story is skipped if a post carries its marker, cites one of its URLs, or covers the same
  entity/event recently. When a post's marker names a story the state DB knows, the topic
  comparison above decides instead of the entity/event heuristic, so a different development
  of the same game is not suppressed.
* **Developments are not suppressed**: a story about an already-covered entity is allowed when
  its event differs or it carries new numbers (dates, prices, versions). After fact extraction
  the core values are compared with the earlier post; if nothing is new it is rejected as a
  duplicate. Accepted developments link to the earlier Poormaz post.

## Fact-check gate

After the rule checks pass, the model fact checker compares the article with the fact sheet
(it also receives the internal-link targets). Its result becomes four blocking checks:

| Check | Passes only when |
|---|---|
| `factcheck.model_verdict` | verdict is exactly `pass` (a missing or unknown verdict fails) |
| `factcheck.model_issues` | no issue of severity high or medium, whatever the verdict |
| `factcheck.model_rejected` | verdict is not `reject` |
| `language.fluent` | fluent Persian, no translationese |

* `revise` (or `pass` with a medium issue) triggers a corrective rewrite. The finding's
  excerpt and explanation are sent back as revision instructions. The rule checks **and**
  the model check then run again on the new text, so an earlier `pass` never carries over to
  a rewritten article.
* At most `NEWSBOT_MAX_REVISIONS` (default 2) rewrites are made. If anything blocking still
  fails, the story is rejected. It is not published, and it is re-evaluated only when new
  sources join.
* `reject` is final: no rewrite is attempted.
* The loop is bounded (each pass spends a revision, runs one model check, or stops) and also
  by the per-run request and cost limits.
* Editorial overrides cannot relax any of this. Draft and publish mode refuse to start with
  `NEWSBOT_LLM_FACTCHECK=0`.

## Verification model

Each claim has: text, category, exact value, status (`confirmed`/`reported`/`speculative`),
importance (`core`/`supporting`/`background`), and per-source verbatim quotes. After code
verification each claim is labelled:

* `official` — supported by an official page (developer, publisher, platform, Steam). The
  quote from that page must itself state the claim: its specific value (date, price, version,
  number), or else the words for what happened ("trailer", "wishlist"). A quote that only
  names the game or its developer, such as a Steam listing's first line, confirms nothing. A
  claim the model marked "reported" becomes "confirmed" when an official page states it
  verbatim without hedging;
* `corroborated` — supported by ≥ 2 independent origins (copies of one press release or of one
  leak count once: text-overlap grouping + the model's origin attribution);
* `single_source` — one non-official source;
* `unverified` — quote or value not found; never shown to the writer.

The gate requires verified core news, ≥ 3 verified claims, and that **every core claim on its
own** is official, independently corroborated, or from a single source of sufficient authority
(`NEWSBOT_ALLOW_SINGLE_SOURCE`, `NEWSBOT_SINGLE_SOURCE_MIN_AUTHORITY`,
`allow_single_source_entities`). An official page confirming one detail does not vouch for the
others. Example from live run 38232433566: Steam confirmed Squadron 42's Q2 2027 window, but
the new trailer and the wishlist were reported by Gamingbolt alone, so the story is rejected.
Each rumored core claim needs two independent origins of its own (configurable), and rumors
must be framed as rumors in title and text.

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
