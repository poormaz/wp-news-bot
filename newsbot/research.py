"""Stage 5: source research.

Builds the evidence set for one story:
  * one document per outlet in the cluster (full text from the feed when available,
    otherwise the article page, honouring robots.txt);
  * official pages linked from that coverage (developer/publisher/platform/press wire);
  * official Steam data (store listing, official announcements) for verified context.
Inaccessible, removed, anti-bot and paywalled pages are recorded and skipped, never
bypassed. Documents that copy the same press release are grouped so they do not
count as independent confirmation.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import Settings
from .htmlextract import extract_page
from .httpclient import HttpClient
from .models import SourceConfig, SourceDoc, Story
from .prompts import scrub_untrusted
from .textutil import (
    clean_text,
    collapse_ws,
    entity_key,
    host_matches,
    overlap_coefficient,
    url_identity,
    word_shingles,
)

log = logging.getLogger("newsbot.research")

NON_ARTICLE_HOSTS = ("x.com", "twitter.com", "youtube.com", "youtu.be", "instagram.com", "facebook.com",
                     "tiktok.com", "reddit.com", "discord.gg", "discord.com", "twitch.tv", "bsky.app",
                     "threads.net", "amazon.com", "bestbuy.com")
NON_ARTICLE_PATH = re.compile(r"\.(?:jpg|jpeg|png|gif|webp|mp4|pdf|zip)$|/(?:wishlist|cart|login|signin|account)\b", re.I)
STEAM_APP_RE = re.compile(r"(?:store\.steampowered\.com/app|steamcommunity\.com/(?:app|games))/(\d{2,8})", re.I)
FEED_TEXT_MIN = 1200


@dataclass
class ResearchResult:
    docs: list[SourceDoc] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    steam_appid: int | None = None

    @property
    def usable_docs(self) -> list[SourceDoc]:
        return [d for d in self.docs if d.usable]

    @property
    def independent_groups(self) -> int:
        return len({d.origin_group for d in self.usable_docs})

    def summary(self) -> dict:
        return {
            "documents": [{"id": d.doc_id, "outlet": d.outlet, "type": d.source_type, "url": d.url,
                           "status": d.fetch_status, "chars": len(d.text), "group": d.origin_group,
                           "notes": d.notes} for d in self.docs],
            "attempts": self.attempts,
            "independent_groups": self.independent_groups,
            "steam_appid": self.steam_appid,
        }


def _trim(text: str, max_chars: int) -> str:
    text = collapse_ws(text)
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > max_chars * 0.6 else cut


def _clean_bbcode(text: str) -> str:
    text = re.sub(r"\[/?[a-z0-9*]+(?:=[^\]]*)?\]", " ", text or "", flags=re.I)
    return clean_text(text)


class Researcher:
    def __init__(self, settings: Settings, http: HttpClient, sources: list[SourceConfig]):
        self.settings = settings
        self.http = http
        self.sources = {s.name: s for s in sources}

    # ------------------------------------------------------------------
    def is_official_url(self, url: str) -> bool:
        return any(host_matches(url, d) for d in self.settings.official_domains + self.settings.press_wire_domains)

    def research(self, story: Story) -> ResearchResult:
        result = ResearchResult()
        per_outlet: dict[str, object] = {}
        for item in sorted(story.items, key=lambda i: (i.source_type != "official", -i.authority_rank,
                                                       i.published_at)):
            per_outlet.setdefault(item.source, item)
        for item in list(per_outlet.values())[: self.settings.max_source_docs]:
            doc = self._doc_from_item(item, result)
            result.docs.append(doc)

        if self.settings.official_expansion:
            self._expand_official(result)
        if self.settings.steam_context:
            try:
                self._steam_context(story, result)
            except Exception as exc:  # noqa: BLE001 - optional context must never break research
                log.info("Steam context skipped: %s", type(exc).__name__)

        for index, doc in enumerate(result.docs, start=1):
            doc.doc_id = f"S{index}"
        assign_origin_groups(result.docs)
        log.info("RESEARCH %s: %d document(s), %d usable, %d independent group(s)", story.story_id,
                 len(result.docs), len(result.usable_docs), result.independent_groups)
        return result

    # ------------------------------------------------------------------
    def _make_doc(self, url: str, outlet: str, source_type: str, authority: str, title: str, text: str,
                  status: str, published: str = "", links=None, image_url: str = "", image_rights: str = "none"
                  ) -> SourceDoc:
        text, removed = scrub_untrusted(_trim(text, self.settings.max_doc_chars))
        doc = SourceDoc(doc_id="", url=url, outlet=outlet, source_type=source_type, authority=authority,
                        title=clean_text(title), text=text, published_at=published, fetch_status=status,
                        links=list(links or []), image_url=image_url, image_rights=image_rights)
        if removed:
            doc.notes.append(f"removed {removed} instruction-like sentence(s) from untrusted text")
            log.warning("Prompt-injection-like text removed from %s (%d sentence(s))", url, removed)
        return doc

    def _doc_from_item(self, item, result: ResearchResult) -> SourceDoc:
        source = self.sources.get(item.source)
        rights = source.images if source else "none"
        published = item.published_at.isoformat()
        feed_page = extract_page(f"<html><body><article>{item.content_html}</article></body></html>", item.url) \
            if item.content_html else None
        if feed_page and len(feed_page.text) >= FEED_TEXT_MIN:
            result.attempts.append({"url": item.url, "outlet": item.source, "status": "feed_fulltext"})
            return self._make_doc(item.url, item.source, item.source_type, item.authority, item.title,
                                  feed_page.text, "ok", published, feed_page.links, image_rights=rights)

        fetched = self.http.get(item.url, check_robots=True)
        result.attempts.append({"url": item.url, "outlet": item.source, "status": fetched.classification,
                                "http": fetched.status})
        if fetched.ok:
            page = extract_page(fetched.text, fetched.final_url or item.url)
            if len(page.text) >= 200:
                return self._make_doc(item.url, item.source, item.source_type, item.authority,
                                      page.title or item.title, page.text, "ok", page.published_at or published,
                                      page.links, page.image_url, rights)
        fallback = feed_page.text if feed_page and len(feed_page.text) > len(item.summary) else item.summary
        status = "feed_only" if len(fallback) >= 200 else (fetched.classification if not fetched.ok else "error")
        doc = self._make_doc(item.url, item.source, item.source_type, item.authority, item.title, fallback,
                             status, published, feed_page.links if feed_page else [], image_rights=rights)
        doc.notes.append(f"page {fetched.classification} (HTTP {fetched.status}); using feed text")
        return doc

    def _expand_official(self, result: ResearchResult) -> None:
        known = {url_identity(d.url) for d in result.docs}
        candidates: list[tuple[str, str]] = []
        for doc in result.docs:
            for href, anchor in doc.links:
                ident = url_identity(href)
                if ident in known or any(host_matches(href, h) for h in NON_ARTICLE_HOSTS):
                    continue
                if NON_ARTICLE_PATH.search(href.split("?")[0]):
                    continue
                if self.is_official_url(href) and ident not in {url_identity(c[0]) for c in candidates}:
                    candidates.append((href, anchor))
        for href, anchor in candidates[: self.settings.max_official_fetches]:
            if STEAM_APP_RE.search(href) and "/app/" in href and "store.steampowered.com" in href:
                continue  # store pages are covered by the Steam API below
            fetched = self.http.get(href, check_robots=True)
            result.attempts.append({"url": href, "outlet": "official", "status": fetched.classification,
                                    "http": fetched.status})
            if not fetched.ok:
                continue
            page = extract_page(fetched.text, fetched.final_url or href)
            if len(page.text) < 200:
                continue
            outlet = page.site_name or href.split("/")[2]
            doc = self._make_doc(href, outlet, "official", "high", page.title or anchor, page.text, "ok",
                                 page.published_at, page.links)
            doc.notes.append("official page linked from coverage")
            result.docs.append(doc)

    # ------------------------------------------------------------------
    def _steam_context(self, story: Story, result: ResearchResult) -> None:
        appid = None
        for doc in result.docs:
            for href, _ in doc.links:
                match = STEAM_APP_RE.search(href)
                if match:
                    appid = int(match.group(1))
                    break
            if appid:
                break
        name = story.primary_display
        if not appid and name and story.primary_entity:
            search = self.http.get("https://store.steampowered.com/api/storesearch/",
                                   params={"term": name, "l": "english", "cc": "US"}, accept="application/json")
            if search.ok:
                try:
                    for row in (json.loads(search.text).get("items") or [])[:10]:
                        if entity_key(row.get("name", "")) == story.primary_entity:
                            appid = int(row["id"])
                            break
                except (ValueError, KeyError, TypeError):
                    appid = None
        if not appid:
            return
        result.steam_appid = appid
        details = self.http.get("https://store.steampowered.com/api/appdetails",
                                params={"appids": str(appid), "l": "english", "cc": "us"}, accept="application/json")
        result.attempts.append({"url": f"steam:appdetails:{appid}", "outlet": "Steam", "status": details.classification})
        if details.ok:
            doc = steam_store_doc(appid, details.text, self._make_doc)
            if doc:
                result.docs.append(doc)
        if story.event_type in ("patch_update", "dlc_expansion", "content_update", "launch", "beta_playtest"):
            news = self.http.get("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
                                 params={"appid": str(appid), "count": "5", "maxlength": "4000", "format": "json"},
                                 accept="application/json")
            result.attempts.append({"url": f"steam:news:{appid}", "outlet": "Steam", "status": news.classification})
            if news.ok:
                doc = steam_news_doc(news.text, story, self._make_doc)
                if doc:
                    result.docs.append(doc)


def steam_store_doc(appid: int, payload: str, make_doc) -> SourceDoc | None:
    try:
        data = (json.loads(payload).get(str(appid)) or {})
    except ValueError:
        return None
    if not data.get("success"):
        return None
    info = data.get("data") or {}
    name = clean_text(info.get("name", ""))
    if not name:
        return None
    lines = [f"Official Steam store listing for {name} (Steam app {appid})."]
    if info.get("developers"):
        lines.append("Developer: " + ", ".join(clean_text(d) for d in info["developers"]) + ".")
    if info.get("publishers"):
        lines.append("Publisher: " + ", ".join(clean_text(p) for p in info["publishers"]) + ".")
    release = info.get("release_date") or {}
    if release.get("date"):
        state = "upcoming" if release.get("coming_soon") else "released"
        lines.append(f"Steam release date: {clean_text(release['date'])} ({state}).")
    platforms = [label for key, label in (("windows", "Windows"), ("mac", "macOS"), ("linux", "Linux"))
                 if (info.get("platforms") or {}).get(key)]
    if platforms:
        lines.append("Platforms listed on Steam: " + ", ".join(platforms) + ".")
    genres = [clean_text(g.get("description", "")) for g in info.get("genres") or [] if isinstance(g, dict)]
    if genres:
        lines.append("Genres on Steam: " + ", ".join(g for g in genres if g) + ".")
    if info.get("is_free"):
        lines.append("The game is free to play on Steam.")
    elif (info.get("price_overview") or {}).get("final_formatted"):
        lines.append(f"Price on the US Steam store: {info['price_overview']['final_formatted']}.")
    if info.get("short_description"):
        lines.append("Store description: " + clean_text(html_lib.unescape(info["short_description"])))
    url = f"https://store.steampowered.com/app/{appid}/"
    doc = make_doc(url, "Steam", "official_store", "high", f"{name} on Steam", " ".join(lines), "ok")
    doc.notes.append("official Steam store data (background)")
    return doc


def steam_news_doc(payload: str, story: Story, make_doc) -> SourceDoc | None:
    try:
        items = (json.loads(payload).get("appnews") or {}).get("newsitems") or []
    except ValueError:
        return None
    now = datetime.now(timezone.utc).timestamp()
    from .textutil import headline_tokens, jaccard
    story_tokens = set(headline_tokens(story.headline))
    best, best_score = None, 0.0
    for row in items:
        if row.get("feedname") != "steam_community_announcements":
            continue  # only the developer's own announcements are official
        if now - float(row.get("date") or 0) > 10 * 86400:
            continue
        score = jaccard(story_tokens, set(headline_tokens(row.get("title", ""))))
        if score > best_score:
            best, best_score = row, score
    if not best or best_score < 0.15:
        return None
    published = datetime.fromtimestamp(float(best.get("date") or 0), tz=timezone.utc).isoformat()
    doc = make_doc(best.get("url") or "", "Steam announcement", "official", "high", best.get("title", ""),
                   _clean_bbcode(best.get("contents", "")), "ok", published)
    doc.notes.append("official Steam announcement")
    return doc


def assign_origin_groups(docs: list[SourceDoc]) -> None:
    """Union documents whose text substantially overlaps (copied press release / syndication)."""
    parent = list(range(len(docs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    shingles = [word_shingles(d.text) for d in docs]
    for i in range(len(docs)):
        for j in range(i + 1, len(docs)):
            if not shingles[i] or not shingles[j]:
                continue
            if overlap_coefficient(shingles[i], shingles[j]) >= 0.35:
                parent[find(j)] = find(i)
    roots: dict[int, int] = {}
    for i, doc in enumerate(docs):
        doc.origin_group = roots.setdefault(find(i), len(roots) + 1)
