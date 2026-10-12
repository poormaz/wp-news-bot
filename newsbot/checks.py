"""Read-only diagnostics (`python bot.py --check`). Never writes to WordPress.

Verifies configuration, feeds, WordPress access and category IDs, the OpenAI model
actually served (one minimal call), discovers and validates live Poormaz localization
pages, audits the head markup of a published post, checks the fallback featured-image IDs
and how the theme renders posts with and without a featured image, and snapshots feed
metadata.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from dataclasses import asdict

from .config import Settings
from .enrich import load_localizations
from .entities import EntityExtractor, Gazetteer
from .httpclient import HttpClient
from .ingest import fetch_feed, load_sources
from .llm import Ledger, LLMClient, LLMError
from .textutil import clean_text, collapse_ws, latin_phrases
from .wordpress import WordPressClient, WordPressError

log = logging.getLogger("newsbot.check")

PROBE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok", "echo"],
                "properties": {"ok": {"type": "boolean"}, "echo": {"type": "string"}}}
LOCALIZATION_TERMS = ["زیرنویس فارسی", "زیرنویس", "فارسی‌ساز", "فارسی ساز", "persian subtitles", "subtitle"]
LOCALIZATION_HINT = re.compile(r"(زیرنویس|فارسی[‌ ]?ساز|persian|farsi|subtitle)", re.I)


def run_checks(settings: Settings, http: HttpClient | None = None, openai_client=None,
               wp: WordPressClient | None = None) -> dict:
    http = http or HttpClient(settings.user_agent, settings.http_timeout, settings.respect_robots)
    out: dict = {"config_problems": {m: _problems(settings, m) for m in ("dry-run", "draft", "publish")}}

    sources = load_sources(settings.sources_file)
    extractor = EntityExtractor(Gazetteer(settings.entities_cfg))
    feeds, snapshot = [], []
    for source in sources:
        items, health = fetch_feed(source, http, extractor, settings.feed_entries_limit)
        row = asdict(health)
        row.update({"enabled": source.enabled, "role": source.role, "type": source.type})
        feeds.append(row)
        for item in items:
            snapshot.append({"source": item.source, "title": item.title, "url": item.url,
                             "published_at": item.published_at.isoformat(), "event_type": item.event_type,
                             "kind": item.kind, "entities": [[e.display, e.kind] for e in item.entities],
                             "has_fulltext": len(clean_text(item.content_html)) >= 1200})
    out["feeds"] = feeds
    out["feed_snapshot"] = snapshot

    if wp is None and settings.wp_base_url:
        wp = WordPressClient(settings.wp_base_url, settings.wp_username, settings.wp_app_password, "dry-run",
                             settings.user_agent)
    if wp is not None:
        out["wordpress"] = _wordpress_checks(wp, settings)
        out["localization"] = discover_localizations(wp, http, settings)
        out["post_audit"] = audit_latest_post(wp, http)
        out["featured_images"] = audit_featured_images(wp, http, settings)
    out["model_probe"] = probe_model(settings, openai_client)
    return out


def _problems(settings: Settings, mode: str) -> list[str]:
    original = settings.mode
    settings.mode = mode
    try:
        return settings.validate(needs_llm=True)
    finally:
        settings.mode = original


def _wordpress_checks(wp: WordPressClient, settings: Settings) -> dict:
    result: dict = {}
    if wp.authenticated:
        try:
            me = wp.check_me()
            result["auth"] = {"ok": True, "user_id": me.get("id"), "roles": me.get("roles"),
                              "can_publish": bool((me.get("capabilities") or {}).get("publish_posts")),
                              "can_create_tags": bool((me.get("capabilities") or {}).get("manage_categories"))}
        except WordPressError as exc:
            result["auth"] = {"ok": False, "error": str(exc)}
    try:
        cats = {int(c["id"]): {"name": html_lib.unescape(c.get("name", "")), "count": c.get("count")}
                for c in wp.categories()}
        result["categories"] = {name: {"id": cid, **(cats.get(cid) or {"missing": True})}
                                for name, cid in (("CAT_ALL", settings.cat_all), ("CAT_GAMING", settings.cat_gaming),
                                                  ("CAT_HARDWARE", settings.cat_hardware)) if cid}
    except WordPressError as exc:
        result["categories"] = {"error": str(exc)}
    try:
        resp = wp._get("wp/v2/tags", {"per_page": "1", "_fields": "id"}, auth=False)
        result["tag_total"] = int(resp.headers.get("X-WP-Total") or 0)
        resp = wp._get("wp/v2/posts", {"per_page": "1", "_fields": "id"}, auth=False)
        result["post_total"] = int(resp.headers.get("X-WP-Total") or 0)
    except (WordPressError, ValueError) as exc:
        result["totals_error"] = str(exc)
    return result


def discover_localizations(wp: WordPressClient, http: HttpClient, settings: Settings) -> dict:
    found: dict[str, dict] = {}
    for term in LOCALIZATION_TERMS:
        try:
            resp = wp._get("wp/v2/search", {"search": term, "per_page": "100", "type": "post"}, auth=False)
        except WordPressError as exc:
            log.info("search %r failed: %s", term, exc)
            continue
        for row in resp.json() or []:
            url = row.get("url", "")
            title = clean_text(html_lib.unescape(row.get("title", "")))
            if url and (LOCALIZATION_HINT.search(title) or LOCALIZATION_HINT.search(url)):
                found.setdefault(url, {"url": url, "title": title, "type": row.get("subtype", ""),
                                       "wp_id": row.get("id")})
    for path in ("wp/v2/pages", "wp/v2/product"):
        for page in range(1, 4):
            try:
                resp = wp._get(path, {"per_page": "100", "page": str(page), "_fields": "id,link,title,slug,featured_media"},
                               auth=False)
            except WordPressError:
                break
            rows = resp.json() or []
            for row in rows:
                title = clean_text(html_lib.unescape((row.get("title") or {}).get("rendered", "")))
                url = row.get("link", "")
                if url and (LOCALIZATION_HINT.search(title) or LOCALIZATION_HINT.search(row.get("slug", ""))):
                    entry = found.setdefault(url, {"url": url, "title": title, "type": path.split("/")[-1],
                                                   "wp_id": row.get("id")})
                    entry["featured_media"] = int(row.get("featured_media") or 0)
            if len(rows) < 100:
                break
    for entry in found.values():
        result = http.get(entry["url"], allow_redirects=False)
        entry["http_status"] = result.status
        entry["valid"] = result.status == 200 and result.classification == "ok"
        if result.final_url and result.final_url != entry["url"]:
            entry["redirects_to"] = result.final_url
        names = sorted(latin_phrases(entry["title"]), key=len, reverse=True)
        entry["game_name_guess"] = collapse_ws(names[0]) if names else ""
    configured = load_localizations(settings.localization_file, settings.wp_base_url)
    existing = []
    for entry in {e["url"]: e for e in configured.values()}.values():
        result = http.get(entry["url"], allow_redirects=False)
        existing.append({"name": entry["name"], "url": entry["url"], "http_status": result.status,
                         "valid": result.status == 200 and result.classification == "ok",
                         "redirects_to": result.final_url if result.final_url != entry["url"] else ""})
    data = {"discovered": sorted(found.values(), key=lambda e: e["url"]), "configured": existing}
    log.info("LOCALIZATION_DISCOVERY_JSON=%s", json.dumps(data, ensure_ascii=False))
    return data


def audit_latest_post(wp: WordPressClient, http: HttpClient) -> dict:
    try:
        posts = wp._get("wp/v2/posts", {"per_page": "1", "status": "publish", "_fields": "id,link"}, auth=False).json()
    except WordPressError as exc:
        return {"error": str(exc)}
    if not posts:
        return {"error": "no published posts"}
    link = posts[0]["link"]
    result = http.get(link)
    page = result.text or ""
    lower = page.lower()
    types = re.findall(r'"@type"\s*:\s*"([^"]+)"', page)
    return {
        "url": link, "http": result.status,
        "h1_count": len(re.findall(r"<h1[\s>]", lower)),
        "canonical_count": len(re.findall(r"<link[^>]+rel=[\"']canonical[\"']", lower)),
        "og_title_count": len(re.findall(r"<meta[^>]+property=[\"']og:title[\"']", lower)),
        "og_image_count": len(re.findall(r"<meta[^>]+property=[\"']og:image[\"']", lower)),
        "json_ld_blocks": lower.count("application/ld+json"),
        "schema_types": sorted(set(types))[:20],
        "robots_meta": re.findall(r"<meta[^>]+name=[\"']robots[\"'][^>]+content=[\"']([^\"']+)", lower)[:2],
    }


def _image_markers(html: str) -> dict:
    lower = (html or "").lower()
    return {
        "og_image": bool(re.search(r"<meta[^>]+property=[\"']og:image[\"']", lower)),
        "featured_img_element": "wp-post-image" in lower,
        "theme_placeholder_hint": bool(re.search(r"(no-?image|placeholder|default-?thumb)", lower)),
    }


def audit_featured_images(wp: WordPressClient, http: HttpClient, settings: Settings) -> dict:
    """Read-only: which fallback media IDs are usable, which theme renders posts, and how a post page
    looks with and without a featured image (evidence for the image fallback decision)."""
    out: dict = {"configured": []}
    for kind, ids in sorted(settings.fallback_featured_media_ids.items()):
        for media_id in ids:
            media = wp.get_media(media_id) or {}
            out["configured"].append({"kind": kind, "id": media_id, "found": bool(media),
                                      "is_image": media.get("media_type") == "image",
                                      "source_url": media.get("source_url", "")})
    home = http.get(settings.wp_base_url + "/") if settings.wp_base_url else None
    themes = sorted(set(re.findall(r"/wp-content/themes/([a-z0-9_-]+)/", (home.text if home else "") or "", re.I)))
    out["themes_in_homepage"] = themes
    try:
        posts = wp._get("wp/v2/posts", {"per_page": "30", "status": "publish", "_fields": "id,link,featured_media"},
                        auth=False).json() or []
    except WordPressError as exc:
        out["error"] = str(exc)
        return out
    without = [p for p in posts if not int(p.get("featured_media") or 0)]
    with_image = [p for p in posts if int(p.get("featured_media") or 0)]
    out["recent_posts"] = len(posts)
    out["recent_without_featured_image"] = [p["link"] for p in without][:10]
    for label, sample in (("example_with_image", with_image[:1]), ("example_without_image", without[:1])):
        if sample:
            page = http.get(sample[0]["link"])
            out[label] = {"url": sample[0]["link"], "http": page.status, **_image_markers(page.text)}
    log.info("FEATURED_IMAGE_AUDIT_JSON=%s", json.dumps(out, ensure_ascii=False))
    return out


def probe_model(settings: Settings, openai_client=None) -> dict:
    if not settings.openai_api_key and openai_client is None:
        return {"ok": False, "error": "OPENAI_API_KEY not set"}
    ledger = Ledger(settings, None, settings.run_id)
    llm = LLMClient(settings, ledger, client=openai_client)
    try:
        data = llm.call_json("probe", "Reply with ok=true and echo the word given.", "Word: poormaz",
                             "probe", PROBE_SCHEMA, 400, settings.reasoning_effort)
        ok = bool(data.get("ok"))
        error = ""
    except LLMError as exc:
        ok, error = False, str(exc)
    totals = ledger.totals()
    return {"ok": ok, "error": error, "model_requested": settings.openai_model, "model_in_use": llm.model,
            "models_served": totals["models_served"], "reasoning_supported": llm.reasoning_supported,
            "api_style": llm.api_style, "fallback_used": llm.fallback_used, "usage": {
                k: totals[k] for k in ("requests", "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens",
                                       "estimated_cost_usd")}}
