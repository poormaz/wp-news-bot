"""Pipeline orchestration.

discovery -> normalization -> entities -> clustering/dedup -> research -> facts ->
verification -> newsworthiness -> composition -> fact check -> quality gate ->
enrichment -> publishing. Each stage has typed inputs/outputs; every story ends with
an explicit, logged decision. Zero posts is a normal outcome.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path

from . import __version__
from .cluster import Clusterer, assign_story_ids, story_title_tokens
from .compose import Composer, render_article, word_budget
from .config import ConfigError, Settings
from .enrich import (
    LinkBuilder,
    build_slug,
    content_type_for,
    find_localization,
    load_localizations,
    localization_sentence,
    pick_categories,
    resolve_tags,
)
from .entities import EntityExtractor, Gazetteer, classify_persian_event, events_compatible, latin_entity_keys
from .factcheck import llm_check, revision_feedback, rule_checks
from .facts import FactDesk, compose_view, story_status
from .httpclient import HttpClient
from .images import choose_featured_image
from .ingest import fetch_feed, load_sources, manual_item, prefilter, read_manual_links
from .llm import BudgetExceeded, Ledger, LLMBadOutput, LLMClient, LLMError, LLMTransient, LLMUnavailable
from .models import Article, CheckResult, FactSheet, Story, utcnow
from .newsworthiness import apply_triage, assess, rule_score, seedable, triage
from .quality import decide, source_checks
from .research import Researcher, ResearchResult
from .state import StateStore, iso, legacy_sync, parse_iso
from .textutil import entity_key, extract_numbers, make_latin_slug, url_identity
from .wordpress import DryRunViolation, PublishNotAllowed, WordPressClient, WordPressError, parse_post_fingerprint

log = logging.getLogger("newsbot.pipeline")

TRANSIENT_FETCH = {"error", "rate_limited"}


@dataclass
class RunReport:
    run_id: str
    mode: str
    version: str = __version__
    started_at: str = ""
    finished_at: str = ""
    elapsed_seconds: float = 0.0
    model_requested: str = ""
    feeds: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    prefilter_reasons: dict = field(default_factory=dict)
    stories: list[dict] = field(default_factory=list)
    published: list[dict] = field(default_factory=list)
    llm: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    exit_code: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class Pipeline:
    def __init__(self, settings: Settings, *, http: HttpClient | None = None, wp: WordPressClient | None = None,
                 openai_client=None, store: StateStore | None = None, sleep=time.sleep, samples: int = 0,
                 compare_legacy: bool = False, offline: bool = False):
        self.s = settings
        self.http = http or HttpClient(settings.user_agent, settings.http_timeout, settings.respect_robots)
        self.wp = wp
        self.store = store
        self._own_store = store is None
        self.sleep = sleep
        self.samples = samples
        self.compare_legacy = compare_legacy
        self.offline = offline
        self.report = RunReport(run_id=settings.run_id, mode=settings.mode, model_requested=settings.openai_model)
        self._openai_client = openai_client
        self.ledger: Ledger | None = None
        self.llm: LLMClient | None = None
        self.posts_made = 0

    # ------------------------------------------------------------------
    def run(self) -> RunReport:
        started = time.monotonic()
        self.report.started_at = iso(utcnow())
        try:
            self._setup()
            self._execute()
        except ConfigError as exc:
            log.error("CONFIGURATION ERROR: %s", exc)
            self.report.errors.append(f"config: {exc}")
            self.report.exit_code = 2
        except Exception as exc:  # noqa: BLE001 - always produce a report
            log.error("RUN FAILED: %s\n%s", exc, traceback.format_exc())
            self.report.errors.append(f"crash: {type(exc).__name__}: {exc}")
            self.report.exit_code = 1
        finally:
            self.report.elapsed_seconds = round(time.monotonic() - started, 2)
            self.report.finished_at = iso(utcnow())
            if self.ledger is not None:
                self.report.llm = self.ledger.totals()
                self.report.llm["fallback_used"] = bool(self.llm and self.llm.fallback_used)
                self.report.llm["model_in_use"] = self.llm.model if self.llm else self.s.openai_model
            if self.store is not None:
                try:
                    self.store.finish_run(self.s.run_id, {"counts": self.report.counts, "llm": self.report.llm})
                except Exception:  # noqa: BLE001
                    pass
                if self._own_store:
                    self.store.close()
        return self.report

    # ------------------------------------------------------------------
    def _setup(self) -> None:
        needs_llm = not self.offline
        problems = self.s.validate(needs_llm=needs_llm)
        if problems:
            raise ConfigError("; ".join(problems))
        if self.store is None:
            self.store = open_state(self.s.state_db, self.report)
        if self.s.legacy_sync:
            self.store.import_legacy(self.s.legacy_db)
        self.store.prune(content_hours=self.s.cluster_window_hours + 24)
        self.store.start_run(self.s.run_id, self.s.mode)
        self.ledger = Ledger(self.s, self.store, self.s.run_id)
        self.llm = LLMClient(self.s, self.ledger, client=self._openai_client, sleep=self.sleep)
        if self.offline:
            self.llm.disabled_reason = "offline run (no model calls)"
        if self.wp is None and self.s.wp_base_url:
            self.wp = WordPressClient(self.s.wp_base_url, self.s.wp_username, self.s.wp_app_password, self.s.mode,
                                      self.s.user_agent, timeout=max(self.s.http_timeout, 25.0), sleep=self.sleep)
        if self.wp is not None and self.wp.authenticated:
            try:
                me = self.wp.check_me()
                log.info("WordPress authenticated as user id %s", me.get("id"))
            except WordPressError as exc:
                msg = f"WordPress authentication check failed: {exc}"
                if self.s.mode in ("draft", "publish"):
                    raise ConfigError(msg) from exc
                self.report.warnings.append(msg)

    def _execute(self) -> None:
        s = self.s
        sources = [src for src in load_sources(s.sources_file) if src.enabled]
        localizations = load_localizations(s.localization_file, s.wp_base_url)
        gazetteer = Gazetteer(s.entities_cfg, {**{e["name"]: "game" for e in localizations.values()},
                                               **self.store.learned_entities()})
        extractor = EntityExtractor(gazetteer)
        self.gazetteer = gazetteer
        self.localizations = localizations
        self.platform_names = {e.display for e in gazetteer.index.values() if e.kind in ("platform", "event")} | \
            {k for k, e in gazetteer.index.items() if e.kind == "platform"}

        self._recover_from_wordpress()
        self._resolve_pending_publications()

        # 1-2. Discovery + normalization ---------------------------------------
        now = utcnow()
        all_items = []
        for source in sources:
            items, health = fetch_feed(source, self.http, extractor, s.feed_entries_limit, now)
            self.report.feeds.append(asdict(health))
            all_items.extend(items)
        manual = self._manual_items(extractor, sources)
        new_items = [i for i in all_items if not self.store.item_known(i.url_identity)]
        reasons: dict[str, int] = {}
        filtered = []
        for item in new_items + manual:
            reason = prefilter(item, s.max_story_age_hours, (s.editorial or {}).get("overrides") or {}, now)
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
                filtered.append((item, reason))
        self.store.upsert_items(new_items + manual)
        for item, reason in filtered:
            self.store.set_item_status(item.item_id, "filtered", reason)
        self.store.conn.commit()
        self.report.prefilter_reasons = reasons
        self.report.counts.update({"feed_items": len(all_items), "new_items": len(new_items),
                                   "manual_items": len(manual), "prefiltered": len(filtered)})
        log.info("DISCOVERY: %d feed item(s), %d new, %d filtered before any API call", len(all_items),
                 len(new_items), len(filtered))

        # 3-4. Clustering + dedup -----------------------------------------------
        pool = self.store.recent_items(s.cluster_window_hours)
        manual_ids = {m.item_id for m in manual}
        for item in pool:
            if item.item_id in manual_ids:
                item.manual = True
        clusters = Clusterer(s.cluster_threshold, s.cluster_window_hours).cluster(pool)
        item_story = {r["item_id"]: r["story_id"] for r in self.store.conn.execute(
            "SELECT item_id, story_id FROM articles WHERE story_id != ''").fetchall()}
        stories = assign_story_ids(clusters, item_story, self.store.recent_stories(s.story_dedup_days),
                                   s.cluster_threshold)
        for story in stories:
            self.store.save_story(story, story_title_tokens(story.items))
        self.report.counts.update({"clusters": len(stories),
                                   "multi_source_clusters": sum(1 for st in stories if len(st.outlets) > 1),
                                   "clustered_duplicates": sum(len(st.items) - 1 for st in stories)})

        eligible: list[Story] = []
        duplicates = 0
        for story in stories:
            verdict = self._eligibility(story)
            if verdict:
                if verdict.startswith("duplicate"):
                    duplicates += 1
                self._record(story, "skipped", [verdict], quiet=True)
                continue
            eligible.append(story)
        self.report.counts["duplicate_stories"] = duplicates
        self.report.counts["eligible_stories"] = len(eligible)

        # 8a. Rank + triage ----------------------------------------------------
        recent_outlets = self._recent_outlets()
        for story in eligible:
            story.score, story.score_reasons = rule_score(story, set(self.localizations), recent_outlets)
            if any(entity_key(e) in story.entity_keys for e in
                   ((s.editorial or {}).get("overrides") or {}).get("priority_entities") or []):
                story.score += 0.5
                story.score_reasons.append("priority entity (+0.50)")
            if story.manual:
                story.score += 5.0
        eligible.sort(key=lambda st: st.score, reverse=True)
        shortlist = eligible[: max(s.max_candidates_per_run * 3, 6)]
        if s.triage_llm and self.llm.available and len(shortlist) > 1:
            try:
                verdicts = triage(shortlist, self.llm, s)
                kept = []
                for story in shortlist:
                    if apply_triage(story, verdicts.get(story.story_id)) or story.manual:
                        kept.append(story)
                    else:
                        self._record(story, "rejected", story.score_reasons[-1:], decision_reason="triage")
                shortlist = sorted(kept, key=lambda st: st.score, reverse=True)
            except (LLMError, BudgetExceeded) as exc:
                self.report.warnings.append(f"triage skipped: {exc}")
        limit = self.samples if self.samples else s.max_candidates_per_run
        candidates = shortlist[:limit]
        self.report.counts["candidates"] = len(candidates)
        log.info("CANDIDATES: %s", [(st.story_id, round(st.score, 2), st.headline[:70]) for st in candidates])

        # 5-12. Per-story processing ---------------------------------------------
        for story in candidates:
            if not self.samples and self.posts_made >= s.max_posts_per_run:
                break
            if not self.llm.available:
                self._record(story, "skipped", [f"model unavailable: {self.llm.disabled_reason}"])
                continue
            try:
                outcome = self.process_story(story)
            except BudgetExceeded as exc:
                # Not the story's fault: retry it on the next run without backoff.
                self.store.update_story(story.story_id, status="transient", next_retry_at=iso(utcnow()))
                self._record(story, "budget", [str(exc)])
                self.report.warnings.append(f"budget: {exc}")
                break
            except LLMUnavailable as exc:
                self._record(story, "skipped", [f"model unavailable: {exc}"])
                self.report.errors.append(f"model unavailable: {exc}")
                break
            if outcome in ("published", "drafted", "dry_run_pass") and not self.samples:
                self.posts_made += 1

        self.report.counts["published"] = sum(1 for p in self.report.published if p.get("status") == "publish")
        self.report.counts["drafted"] = sum(1 for p in self.report.published if p.get("status") == "draft")
        self.report.counts["rejected"] = sum(1 for st in self.report.stories if st["decision"] == "rejected")
        if self.llm.disabled_reason and not self.offline and self.report.exit_code == 0:
            self.report.exit_code = 3

    # ------------------------------------------------------------------
    def _manual_items(self, extractor, sources) -> list:
        urls = read_manual_links(self.s.manual_links_file)
        cfg_urls = ((self.s.editorial or {}).get("overrides") or {}).get("force_include_urls") or []
        items = []
        for url in list(dict.fromkeys(urls + [str(u) for u in cfg_urls])):
            ident = url_identity(url)
            if self.store.manual_status(ident) in ("done", "rejected"):
                continue
            item, err = manual_item(url, self.http, extractor, sources)
            if item is None:
                self.store.set_manual_status(ident, url, "unavailable", detail=err)
                self.report.warnings.append(f"manual link unavailable: {url} ({err})")
                continue
            items.append(item)
            self.store.set_manual_status(ident, url, "queued")
        return items

    def _recent_outlets(self) -> list[str]:
        rows = self.store.conn.execute(
            "SELECT a.source FROM stories s JOIN articles a ON a.story_id = s.story_id "
            "WHERE s.status IN ('published','drafted') ORDER BY s.published_at DESC LIMIT 6").fetchall()
        return [r["source"] for r in rows]

    # -- recovery -----------------------------------------------------------------
    def _recover_from_wordpress(self) -> None:
        if not (self.s.wp_recovery and self.wp is not None):
            return
        try:
            posts = self.wp.recent_posts(self.s.wp_recent_posts)
        except WordPressError as exc:
            self.report.warnings.append(f"WordPress recovery skipped: {exc}")
            return
        snapshot = []
        for post in posts:
            fp = parse_post_fingerprint(post)
            snapshot.append({
                "id": post["id"], "link": post["link"], "title": post["title"], "date": post["date"],
                "status": post["status"], "story_id": fp["story_id"], "source_urls": fp["source_urls"],
                "entity_keys": latin_entity_keys(post["title"], self.gazetteer),
                "event_type": classify_persian_event(post["title"]), "numbers": fp["numbers"],
            })
            if fp["story_id"]:
                stored = self.store.get_story(fp["story_id"])
                status = "published" if post["status"] in ("publish", "future") else "drafted"
                if stored and stored.get("status") not in ("published", "drafted"):
                    self.store.update_story(fp["story_id"], status=status, wp_post_id=post["id"], wp_link=post["link"])
        self.store.replace_wp_recent(snapshot)
        log.info("RECOVERY: %d recent WordPress post(s) fingerprinted (%d with v2 markers)", len(snapshot),
                 sum(1 for p in snapshot if p["story_id"]))
        self.report.counts["wp_recent_posts"] = len(snapshot)

    def _resolve_pending_publications(self) -> None:
        """A crashed run may have created a post without recording it; adopt it instead of duplicating."""
        if self.wp is None:
            return
        for pub in self.store.pending_publications():
            try:
                found = self.wp.find_by_marker(pub["story_id"])
            except WordPressError:
                continue
            if found:
                status = "published" if found["status"] in ("publish", "future") else "drafted"
                self.store.update_publication(pub["story_id"], status=status, wp_post_id=found["id"],
                                              wp_link=found["link"])
                self.store.update_story(pub["story_id"], status=status, wp_post_id=found["id"], wp_link=found["link"])
                log.warning("Adopted post %s left by an interrupted run for story %s", found["id"], pub["story_id"])
            else:
                self.store.update_publication(pub["story_id"], status="abandoned", error="no post found")

    # -- dedup / eligibility ----------------------------------------------------------
    def _eligibility(self, story: Story) -> str:
        s = self.s
        stored = self.store.get_story(story.story_id) or {}
        status = stored.get("status") or "new"
        if not seedable(story):
            return "corroboration-only sources"
        if not story.manual and utcnow() - story.first_published > timedelta(hours=s.max_story_age_hours) \
                and status not in ("published", "drafted"):
            return f"stale: first reported more than {s.max_story_age_hours}h ago"
        if status in ("published", "drafted"):
            if self._has_new_numbers(story, json.loads(stored.get("fact_values_json") or "[]")):
                story.is_development = True
                story.parent_post = {"link": stored.get("wp_link", ""), "title": stored.get("headline", ""),
                                     "id": stored.get("wp_post_id")}
            else:
                return f"duplicate: story already {status} (post {stored.get('wp_post_id')})"
        if status == "rejected":
            if int(stored.get("attempts") or 0) >= s.max_story_attempts:
                return "rejected earlier; attempt limit reached"
            if len(story.outlets) <= int(stored.get("source_count_at_decision") or 0):
                return "rejected earlier; no new sources since"
        if status == "transient":
            retry_at = parse_iso(stored.get("next_retry_at"))
            if retry_at and retry_at > utcnow():
                return f"waiting to retry after {stored.get('next_retry_at')}"
            if int(stored.get("transient_failures") or 0) >= s.max_story_attempts:
                return "transient failures exhausted"
        if status in ("dry_run_pass",) and s.mode == "dry-run" and not self.samples:
            return "already evaluated in an earlier dry run"
        dup = self._wordpress_duplicate(story)
        if dup:
            return dup
        return ""

    def _has_new_numbers(self, story: Story, known: list[str]) -> bool:
        known_set = set(known)
        recent = [i for i in story.items if utcnow() - i.published_at < timedelta(hours=self.s.max_story_age_hours)]
        new_numbers = {n for i in recent for n in extract_numbers(i.title) if len(n) >= 1}
        return bool(known_set) and bool(new_numbers - known_set - {"1", "2", "3", "4", "5"})

    def _wordpress_duplicate(self, story: Story) -> str:
        cutoff = utcnow() - timedelta(days=self.s.story_dedup_days)
        idents = {i.url_identity for i in story.items}
        for post in self.store.wp_recent(self.s.story_dedup_days + 5):
            if post.get("story_id") == story.story_id:
                return f"duplicate: WordPress post {post['post_id']} carries this story's marker"
            if idents & {url_identity(u) for u in post["source_urls"]}:
                return f"duplicate: source already cited by WordPress post {post['post_id']}"
            date = parse_iso(post.get("date"))
            if date and date < cutoff:
                continue
            if story.primary_entity and story.primary_entity in post["entity_keys"] and \
                    events_compatible(story.event_type, post.get("event_type") or "other"):
                hours = (story.first_published - date).total_seconds() / 3600 if date else 0
                new_numbers = {n for i in story.items for n in extract_numbers(i.title)} - set(post["numbers"])
                if hours > 6 and new_numbers - {"1", "2", "3", "4", "5"}:
                    story.is_development = True
                    story.parent_post = {"link": post["link"], "title": post["title"], "id": post["post_id"]}
                    continue
                return (f"duplicate: WordPress post {post['post_id']} already covers "
                        f"{story.primary_display or story.primary_entity} ({post.get('event_type')})")
        return ""

    # -- per story --------------------------------------------------------------------
    def process_story(self, story: Story) -> str:
        s = self.s
        log.info("=== STORY %s | %s | outlets=%s | score=%.2f", story.story_id, story.headline[:90], story.outlets,
                 story.score)
        stored = self.store.get_story(story.story_id) or {}
        self.store.update_story(story.story_id, attempts=int(stored.get("attempts") or 0) + 1)
        cost_before = self.ledger.cost
        record: dict = {}
        try:
            research = Researcher(s, self.http, load_sources(s.sources_file)).research(story)
            record["research"] = research.summary()
            if not research.usable_docs:
                transient = all(a.get("status") in TRANSIENT_FETCH for a in research.attempts) and research.attempts
                return self._fail(story, record, "sources unavailable: " + ", ".join(
                    f"{a['outlet']}={a['status']}" for a in research.attempts[:5]), transient=bool(transient))

            sheet = FactDesk(s, self.llm, self.store).extract(story, research)
            self._learn(sheet, story)
            record["facts"] = facts_summary(sheet, research)
            docs = research.usable_docs

            pre_checks = assess(sheet, story, s) + source_checks(story, sheet, research, s,
                                                                 self._post_fact_duplicate(story, sheet))
            early = decide(pre_checks)
            if not early.passed:
                return self._reject(story, record, early.to_dict(), cost_before, len(story.outlets))

            localization = find_localization(story, self.localizations, sheet.entities.get("games"))
            links = LinkBuilder(s, self.http, self.wp, self.store).candidates(story, localization)
            view = compose_view(sheet, docs)
            min_words, max_words = word_budget(sheet, s)
            composer = Composer(s, self.llm, self.store)
            recent_openings = self.store.recent_openings(30)
            recent_anchors = {c.url: self.store.recent_anchors(c.url, 10) for c in links}

            article = composer.compose(story, sheet, view, links)
            article = self._render(article, story, sheet, docs, links, localization, recent_anchors)
            checks = rule_checks(article, story, sheet, docs, s, min_words, max_words, recent_openings,
                                 self.platform_names)
            llm_checks: list[CheckResult] = []
            revisions = 0
            while True:
                failed = [c for c in checks + llm_checks if c.blocking and not c.passed]
                if failed and revisions < s.max_revisions and revision_feedback(failed):
                    revisions += 1
                    log.info("REVISION %d for %s: %s", revisions, story.story_id, [c.name for c in failed])
                    article = composer.compose(story, sheet, view, links, revision_feedback(failed), revisions)
                    article = self._render(article, story, sheet, docs, links, localization, recent_anchors)
                    checks = rule_checks(article, story, sheet, docs, s, min_words, max_words, recent_openings,
                                         self.platform_names)
                    llm_checks = []
                    continue
                if not failed and s.llm_factcheck and not llm_checks:
                    llm_checks, raw_check = llm_check(article, view, self.llm, s)
                    record["model_factcheck"] = raw_check
                    continue
                break
            decision = decide(pre_checks + checks + llm_checks)
            record["article"] = article_summary(article)
            record["article_html"] = article.html
            if self.compare_legacy:
                record["legacy_comparison"] = self._legacy_compare(story, sheet, docs, min_words, max_words)
            if not decision.passed:
                return self._reject(story, record, decision.to_dict(), cost_before, len(story.outlets),
                                    article=article)
            record["gate"] = decision.to_dict()
            return self._publish(story, sheet, research, article, localization, record, cost_before)
        except (LLMTransient, LLMBadOutput, WordPressError) as exc:
            transient = isinstance(exc, LLMTransient) or getattr(exc, "transient", False)
            if isinstance(exc, WordPressError) and not exc.transient:
                self.report.errors.append(f"WordPress: {exc}")
                self.report.exit_code = 3
            return self._fail(story, record, f"{type(exc).__name__}: {exc}", transient=transient or
                              isinstance(exc, LLMBadOutput), cost_before=cost_before)
        except (DryRunViolation, PublishNotAllowed, BudgetExceeded, LLMUnavailable):
            raise
        except LLMError as exc:
            return self._fail(story, record, f"model error: {exc}", transient=True, cost_before=cost_before)

    def _render(self, article: Article, story: Story, sheet: FactSheet, docs, links, localization,
                recent_anchors) -> Article:
        article = render_article(article, story, sheet, docs, links, self.s, recent_anchors)
        loc_link = next((c for c in links if c.kind == "localization"), None)
        if loc_link and not any(u["url"] == loc_link.url for u in article.links_used):
            sentence, anchor = localization_sentence(localization, story.story_id,
                                                     recent_anchors.get(loc_link.url, []))
            article = render_article(article, story, sheet, docs, links, self.s, recent_anchors, sentence)
            article.links_used.append({"id": loc_link.id, "url": loc_link.url, "anchor": anchor,
                                       "kind": "localization", "placement": "fallback sentence"})
        return article

    def _post_fact_duplicate(self, story: Story, sheet: FactSheet) -> str:
        if not story.is_development:
            return ""
        parent_numbers: set[str] = set()
        stored = self.store.get_story(story.story_id) or {}
        parent_numbers |= set(json.loads(stored.get("fact_values_json") or "[]"))
        for post in self.store.wp_recent(self.s.story_dedup_days + 5):
            if story.parent_post and post["post_id"] == story.parent_post.get("id"):
                parent_numbers |= set(post["numbers"])
        core_values = {n for c in sheet.core_claims for n in extract_numbers(c.value or c.text_en)}
        if core_values and core_values <= parent_numbers:
            return "duplicate: no new verified facts compared with the earlier Poormaz post"
        return ""

    def _learn(self, sheet: FactSheet, story: Story) -> None:
        games = sheet.entities.get("games") or []
        rows = [(entity_key(g), g, "game") for g in games]
        rows += [(entity_key(p), p, "hardware") for p in sheet.entities.get("products") or []]
        self.store.learn_entities([r for r in rows if r[0] and len(r[0]) >= 3])
        for game in games:
            key = entity_key(game)
            if key and story.primary_entity and (key == story.primary_entity or key.startswith(story.primary_entity)
                                                  or story.primary_entity.startswith(key)):
                story.primary_display = game
                break

    # -- outcomes ----------------------------------------------------------------------
    def _record(self, story: Story, decision: str, reasons: list[str], quiet: bool = False,
                decision_reason: str = "", extra: dict | None = None) -> None:
        entry = {
            "story_id": story.story_id, "headline": story.headline, "outlets": story.outlets,
            "items": [{"source": i.source, "title": i.title, "url": i.url} for i in story.items[:6]],
            "event_type": story.event_type, "kind": story.kind, "score": round(story.score, 3),
            "score_reasons": story.score_reasons, "decision": decision, "reasons": reasons,
            "development": story.is_development,
        }
        entry.update(extra or {})
        self.report.stories.append(entry)
        if not quiet:
            log.info("DECISION %s: %s | %s", story.story_id, decision.upper(), "; ".join(reasons)[:400])
        if decision in ("rejected", "transient", "published", "drafted", "dry_run_pass", "budget", "error"):
            self.store.record_decision(self.s.run_id, story.story_id, decision, reasons)

    def _reject(self, story: Story, record: dict, gate: dict, cost_before: float, sources: int,
                article: Article | None = None) -> str:
        reasons = gate.get("reasons") or ["rejected"]
        retryable = gate.get("retryable")
        record["gate"] = gate
        record["llm_cost_usd"] = round(self.ledger.cost - cost_before, 6)
        if retryable:
            return self._fail(story, record, "; ".join(reasons), transient=True, cost_before=cost_before)
        self.store.update_story(story.story_id, status="rejected", last_reasons_json=json.dumps(reasons, ensure_ascii=False),
                                source_count_at_decision=sources)
        if self.s.legacy_sync and self.s.mode != "dry-run":
            legacy_sync(self.s.legacy_db, story.items, "skipped")
        self._record(story, "rejected", reasons, extra=record)
        return "rejected"

    def _fail(self, story: Story, record: dict, reason: str, transient: bool, cost_before: float | None = None) -> str:
        stored = self.store.get_story(story.story_id) or {}
        if cost_before is not None:
            record["llm_cost_usd"] = round(self.ledger.cost - cost_before, 6)
        if transient:
            failures = int(stored.get("transient_failures") or 0) + 1
            retry_at = utcnow() + timedelta(minutes=self.s.retry_backoff_minutes * failures)
            self.store.update_story(story.story_id, status="transient", transient_failures=failures,
                                    next_retry_at=iso(retry_at), last_reasons_json=json.dumps([reason]))
            self._record(story, "transient", [reason, f"retry after {iso(retry_at)}"], extra=record)
            return "transient"
        self.store.update_story(story.story_id, status="rejected", last_reasons_json=json.dumps([reason]),
                                source_count_at_decision=len(story.outlets))
        self._record(story, "rejected", [reason], extra=record)
        return "rejected"

    # -- publishing ------------------------------------------------------------------------
    def _publish(self, story: Story, sheet: FactSheet, research: ResearchResult, article: Article,
                 localization: dict | None, record: dict, cost_before: float) -> str:
        s = self.s
        content_type = content_type_for(article, story)
        categories = pick_categories(content_type, s)
        slug = build_slug(article, story)
        tag_ids, tag_notes = resolve_tags(article.entity_tags or [story.primary_display], self.wp, s,
                                          dry_run=s.mode == "dry-run")
        image = choose_featured_image(s.image_policy, research.usable_docs, localization,
                                      s.fallback_featured_media_id, s.reuse_localization_image, self.http)
        meta_title = article.meta_title_fa[:65]
        meta_desc = article.meta_description_fa[:160]
        record.update({"categories": categories, "content_type": content_type, "slug": slug, "tags": tag_ids,
                       "tag_notes": tag_notes, "image": {"media_id": image.media_id, "basis": image.basis,
                                                         "upload_from": image.source_url}})
        record["llm_cost_usd"] = round(self.ledger.cost - cost_before, 6)
        fact_values = sorted({n for c in sheet.usable_claims for n in extract_numbers(c.value or c.text_en)})

        if s.mode == "dry-run":
            self.store.update_story(story.story_id, status="dry_run_pass")
            self._write_article_artifact(story, article, record)
            self._record(story, "dry_run_pass", ["passed the quality gate; dry-run made no WordPress writes"],
                         extra=record)
            return "dry_run_pass"

        existing = self.wp.find_by_marker(story.story_id)
        if existing:
            status = "published" if existing["status"] in ("publish", "future") else "drafted"
            self.store.update_story(story.story_id, status=status, wp_post_id=existing["id"], wp_link=existing["link"])
            self._record(story, "skipped", [f"duplicate: post {existing['id']} already exists for this story"],
                         extra=record)
            return "skipped"

        self.store.begin_publication(story.story_id, s.run_id, slug, s.mode)
        media_id = image.media_id
        if image.upload:
            content, ext, mime = image.upload
            try:
                media = self.wp.upload_media(content, f"{slug[:60]}-{story.story_id[:6]}.{ext}", mime, article.title_fa)
                media_id = int(media["id"])
            except WordPressError as exc:
                self.report.warnings.append(f"featured image upload failed (post continues without it): {exc}")
                media_id = 0
        payload = {"title": article.title_fa, "content": article.html, "slug": slug, "excerpt": meta_desc,
                   "categories": categories, "tags": tag_ids}
        if media_id:
            payload["featured_media"] = media_id
        post = self.wp.create_post(payload)
        post_id = int(post["id"])
        self.store.update_publication(story.story_id, status="draft_created", wp_post_id=post_id,
                                      wp_link=post.get("link", ""))
        log.info("WordPress draft %s created for story %s", post_id, story.story_id)
        if s.rankmath_enabled:
            record["rankmath"] = self.wp.update_rankmath(post_id, meta_title, meta_desc, article.focus_keyword_fa)

        problems = verify_saved_post(self.wp.get_post(post_id), story.story_id, categories)
        record["verification"] = problems or ["ok"]
        if problems:
            self.store.update_publication(story.story_id, status="draft_created", error="; ".join(problems))
            self.store.update_story(story.story_id, status="drafted", wp_post_id=post_id)
            self.report.errors.append(f"post {post_id} failed verification and stays a draft: {problems}")
            self.report.exit_code = 3
            self._record(story, "drafted", [f"verification failed: {problems}"], extra=record)
            return "drafted"

        status = "draft"
        link = post.get("link", "")
        if s.mode == "publish":
            published = self.wp.update_post(post_id, {"status": "publish"})
            status = published.get("status", "publish")
            link = published.get("link", link)
            record["public_check"] = self._public_check(link)
        story_status_value = "published" if status in ("publish", "future") else "drafted"
        self.store.update_publication(story.story_id, status=story_status_value, wp_link=link)
        self.store.update_story(story.story_id, status=story_status_value, wp_post_id=post_id, wp_link=link,
                                published_at=iso(utcnow()), fact_values_json=json.dumps(fact_values))
        self.store.add_opening(story.story_id, article.lead.get("text", "")[:200])
        for used in article.links_used:
            self.store.add_anchor(used["url"], used["anchor"])
        if s.legacy_sync:
            legacy_sync(s.legacy_db, story.items, "posted" if story_status_value == "published" else "skipped",
                        post_id)
        for item in story.items:
            if item.manual:
                self.store.set_manual_status(item.url_identity, item.url, "done", story.story_id)
        entry = {"story_id": story.story_id, "post_id": post_id, "status": status, "link": link,
                 "title": article.title_fa, "slug": slug}
        self.report.published.append(entry)
        record["wp"] = entry
        self._write_article_artifact(story, article, record)
        self._record(story, story_status_value, [f"WordPress post {post_id} ({status})"], extra=record)
        return story_status_value

    def _public_check(self, link: str) -> dict:
        if not link:
            return {}
        result = self.http.get(link)
        html = result.text or ""
        lower = html.lower()
        return {
            "http": result.status,
            "h1_count": lower.count("<h1"),
            "canonical_count": lower.count('rel="canonical"') + lower.count("rel='canonical'"),
            "og_title_count": lower.count('property="og:title"') + lower.count("property='og:title'"),
            "json_ld_blocks": lower.count("application/ld+json"),
        }

    def _write_article_artifact(self, story: Story, article: Article, record: dict) -> None:
        out = self.s.report_dir / "articles"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{story.story_id}.html").write_text(
            f"<!doctype html><html lang=\"fa\" dir=\"rtl\"><meta charset=\"utf-8\"><title>{article.title_fa}</title>"
            f"<body style=\"max-width:760px;margin:auto;font-family:Tahoma,sans-serif;line-height:1.9\">"
            f"<h1>{article.title_fa}</h1>{article.html}</body></html>", encoding="utf-8")

    def _legacy_compare(self, story: Story, sheet: FactSheet, docs, min_words: int, max_words: int) -> dict:
        from .legacy_compare import compare_with_legacy
        try:
            return compare_with_legacy(story, sheet, docs, self.s, min_words, max_words, self.platform_names)
        except Exception as exc:  # noqa: BLE001 - comparison is informational
            return {"error": f"{type(exc).__name__}: {exc}"}


def verify_saved_post(post: dict, story_id: str, categories: list[int]) -> list[str]:
    problems = []
    content = (post.get("content") or {})
    raw = content.get("raw") or content.get("rendered") or ""
    if f"nbstory-{story_id}" not in raw:
        problems.append("story marker missing from saved content")
    if "<h1" in raw.lower():
        problems.append("content contains an H1 (the theme provides the title H1)")
    if categories and sorted(post.get("categories") or []) != sorted(categories):
        problems.append(f"categories {post.get('categories')} != {categories}")
    if len(raw) < 400:
        problems.append("saved content is unexpectedly short")
    return problems


def facts_summary(sheet: FactSheet, research: ResearchResult) -> dict:
    by_id = {d.doc_id: d.outlet for d in research.docs}
    return {
        "status": story_status(sheet),
        "claims_total": len(sheet.claims),
        "claims_usable": len(sheet.usable_claims),
        "core_claims": len(sheet.core_claims),
        "independent_groups": research.independent_groups,
        "contradictions": sheet.contradictions,
        "from_cache": sheet.from_cache,
        "injection_detected": sheet.injection_detected,
        "newsworthiness": sheet.newsworthiness,
        "claims": [{"id": c.id, "text": c.text_en, "category": c.category, "value": c.value, "status": c.status,
                    "importance": c.importance, "verification": c.verification,
                    "sources": [by_id.get(s, s) for s in c.verified_sources], "problems": c.problems}
                   for c in sheet.claims],
    }


def article_summary(article: Article) -> dict:
    return {"title": article.title_fa, "meta_title": article.meta_title_fa,
            "meta_description": article.meta_description_fa, "focus_keyword": article.focus_keyword_fa,
            "slug_suggestion": article.slug_en, "words": article.word_count, "links": article.links_used,
            "sections": len(article.sections), "headings": [s["heading_fa"] for s in article.sections if s["heading_fa"]],
            "uncertainties": article.uncertainties, "entity_tags": article.entity_tags,
            "revision": article.revision, "plain_text": article.plain_text}


def open_state(path: Path, report: RunReport) -> StateStore:
    """Open the state DB; a corrupt file is set aside and rebuilt (WordPress recovery fills the gap)."""
    if path.exists():
        try:
            store = StateStore(path)
            if store.integrity_ok():
                return store
            store.close()
        except Exception:  # noqa: BLE001
            pass
        backup = path.with_suffix(".corrupt")
        shutil.move(str(path), str(backup))
        report.warnings.append(f"state DB was corrupt; moved to {backup.name} and rebuilt")
    else:
        report.warnings.append("no state DB restored (first run or cache miss); rebuilding from WordPress")
    return StateStore(path)


def write_reports(report: RunReport, report_dir: Path) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    data = report.to_dict()
    for story in data["stories"]:
        story.pop("article_html", None)
    (report_dir / "run-report.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str),
                                                 encoding="utf-8")
    summary = markdown_summary(report)
    (report_dir / "run-summary.md").write_text(summary, encoding="utf-8")
    step_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(summary + "\n")


def markdown_summary(report: RunReport) -> str:
    llm = report.llm or {}
    lines = [
        f"## Poormaz News Bot {report.version} — {report.mode}",
        "",
        "| Metric | Value |", "|---|---|",
        f"| Run | `{report.run_id}` |",
        f"| Model requested / served | `{report.model_requested}` / `{', '.join(llm.get('models_served') or []) or 'none'}` |",
        f"| API requests | {llm.get('requests', 0)} |",
        f"| Tokens in (cached) / out (reasoning) | {llm.get('input_tokens', 0)} ({llm.get('cached_tokens', 0)}) / "
        f"{llm.get('output_tokens', 0)} ({llm.get('reasoning_tokens', 0)}) |",
        f"| Estimated cost | ${llm.get('estimated_cost_usd', 0):.5f}"
        + (f" (+{llm.get('unpriced_calls')} unpriced call(s))" if llm.get("unpriced_calls") else "") + " |",
        f"| Elapsed | {report.elapsed_seconds:.1f}s |",
    ]
    for key in ("feed_items", "new_items", "prefiltered", "clusters", "multi_source_clusters", "clustered_duplicates",
                "duplicate_stories", "eligible_stories", "candidates", "rejected", "published", "drafted"):
        if key in report.counts:
            lines.append(f"| {key.replace('_', ' ')} | {report.counts[key]} |")
    lines.append(f"| Exit code | {report.exit_code} |")
    decided = [st for st in report.stories if st["decision"] not in ("skipped",)]
    if decided:
        lines += ["", "### Story decisions", "", "| Decision | Story | Reasons |", "|---|---|---|"]
        for st in decided[:20]:
            reasons = "; ".join(st.get("reasons") or [])[:220].replace("|", "/")
            lines.append(f"| {st['decision']} | {st['headline'][:80].replace('|', '/')} ({len(st['outlets'])} outlet(s)) "
                         f"| {reasons} |")
    if report.published:
        lines += ["", "### WordPress", ""]
        for p in report.published:
            lines.append(f"- {p['status']}: post {p['post_id']} — {p['title']} {p.get('link', '')}")
    if report.warnings:
        lines += ["", "### Warnings", ""] + [f"- {w}" for w in report.warnings[:20]]
    if report.errors:
        lines += ["", "### Errors", ""] + [f"- {e}" for e in report.errors[:20]]
    if (llm.get("models_served") or []) and not all(m.startswith(report.model_requested)
                                                    for m in llm.get("models_served") or []):
        lines += ["", f"> **Model mismatch:** requested `{report.model_requested}`, served "
                      f"`{', '.join(llm.get('models_served'))}`."]
    return "\n".join(lines)


def make_slug_preview(story: Story) -> str:
    return make_latin_slug(story.headline)
