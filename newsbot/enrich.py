"""Stage 12: WordPress enrichment - categories, tags, slug, internal links.

Internal links are only ever taken from (a) the hand-maintained, validated
localization_pages.yaml, (b) existing published Poormaz posts returned by the
WordPress API, and (c) the earlier Poormaz post for the same story. Every target must
answer 200 without a redirect. URLs are never guessed or invented.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import logging
import re
from pathlib import Path

import yaml

from .config import Settings
from .httpclient import HttpClient
from .models import Article, LinkCandidate, Story
from .textutil import clean_text, entity_key, make_latin_slug, site_host, url_identity

log = logging.getLogger("newsbot.enrich")

GENERIC_TAGS = {
    "game", "games", "gaming", "video game", "video games", "news", "latest news", "update", "updates",
    "trailer", "teaser", "dlc", "review", "preview", "hands on", "hardware", "technology", "tech", "pc", "console",
    "playstation", "xbox", "nintendo", "steam", "announcement", "release", "launch", "performance", "benchmark",
    "driver", "rumor", "rumors", "leak", "leaks", "pc gaming", "ps5", "switch", "switch 2",
}
HARDWARE_HINT = re.compile(r"\b(gpu|graphics card|geforce|rtx|radeon|cpu|processor|ryzen|core ultra|intel arc|"
                           r"nvidia|amd|ssd|ddr[345]|motherboard|laptop|monitor|oled|handheld|steam deck|rog ally|"
                           r"driver|firmware|qualcomm|snapdragon)\b", re.I)


# ---------------------------------------------------------------------------
# Localization map
# ---------------------------------------------------------------------------
def load_localizations(path: Path, wp_base_url: str) -> dict[str, dict]:
    """{entity_key: entry}. Entries whose URL is not on the WordPress site are skipped."""
    index: dict[str, dict] = {}
    if not path.exists():
        log.info("Localization map %s not found; localization links disabled", path.name)
        return index
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        log.warning("Localization map %s is not valid YAML; localization links disabled", path.name)
        return index
    entries = data.get("localizations") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return index
    site = site_host(wp_base_url) if wp_base_url else "poormaz.com"
    for name, cfg in entries.items():
        if not isinstance(cfg, dict):
            continue
        url = str(cfg.get("url") or "").strip()
        if not url.startswith("https://") or site_host(url) != site:
            log.warning("Localization map: skipping %r (url must be https on %s)", name, site)
            continue
        entry = {
            "name": clean_text(str(name)),
            "url": url,
            "anchor": clean_text(str(cfg.get("anchor") or "")),
            "kind": str(cfg.get("kind") or ("full" if "فارسی‌ساز" in str(cfg.get("anchor") or "") else "subtitle")),
            "featured_media": int(cfg.get("featured_media") or 0),
        }
        aliases = cfg.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        for label in [name, *aliases]:
            key = entity_key(str(label))
            if key and key not in index:
                index[key] = entry
    return index


def find_localization(story: Story, index: dict[str, dict], extra_names: list[str] | None = None) -> dict | None:
    keys = [story.primary_entity] if story.primary_entity else []
    keys += sorted(story.entity_keys)
    keys += [entity_key(n) for n in extra_names or []]
    for key in keys:
        if key and key in index:
            return index[key]
    return None


ANCHOR_TEMPLATES = {
    "subtitle": ["زیرنویس فارسی {name}", "نسخه زیرنویس فارسی {name}", "صفحه زیرنویس فارسی {name}"],
    "full": ["فارسی‌ساز {name}", "نسخه فارسی‌شده {name}", "صفحه فارسی‌ساز {name}"],
}
SENTENCE_TEMPLATES = [
    "برای تجربه {name} به زبان فارسی، {link} در پورماز در دسترس است.",
    "اگر قصد دارید {name} را با متن فارسی بازی کنید، می‌توانید به {link} سر بزنید.",
    "{link} پیش‌تر در پورماز منتشر شده است.",
]


def localization_sentence(entry: dict, story_id: str, recent_anchors: list[str]) -> tuple[str, str]:
    """A natural one-sentence internal link with an anchor that was not used recently."""
    name = entry["name"]
    anchors = [a.format(name=name) for a in ANCHOR_TEMPLATES.get(entry.get("kind", "subtitle"), [])]
    if entry.get("anchor"):
        anchors.append(re.sub(r"^دانلود\s+", "", entry["anchor"]))
    fresh = [a for a in anchors if a not in recent_anchors] or anchors
    seed = int(hashlib.sha256(story_id.encode()).hexdigest(), 16)
    anchor = fresh[seed % len(fresh)]
    template = SENTENCE_TEMPLATES[(seed // 7) % len(SENTENCE_TEMPLATES)]
    link = f'<a href="{html_lib.escape(entry["url"], quote=True)}">{html_lib.escape(anchor)}</a>'
    return template.format(name=html_lib.escape(name), link=link), anchor


# ---------------------------------------------------------------------------
# Internal links
# ---------------------------------------------------------------------------
class LinkBuilder:
    def __init__(self, settings: Settings, http: HttpClient, wp, store=None):
        self.settings = settings
        self.http = http
        self.wp = wp
        self.store = store

    def validate(self, url: str, must_mention: str = "") -> bool:
        """200 without redirect; optionally the page must mention a name (guards against wrong mappings)."""
        if not self.settings.validate_links:
            return True
        cache_key = url + (f"#mentions={entity_key(must_mention)}" if must_mention else "")
        if self.store is not None:
            cached = self.store.get_url_check(cache_key)
            if cached is not None:
                return bool(cached["ok"])
        result = self.http.get(url, allow_redirects=False)
        ok = result.status == 200 and result.classification == "ok"
        if not ok:
            log.warning("Internal link rejected (%s %s%s): %s", result.status, result.classification,
                        f" -> {result.final_url}" if result.final_url and result.final_url != url else "", url)
        elif must_mention and entity_key(must_mention) not in entity_key(clean_text(result.text[:200000])):
            ok = False
            log.warning("Internal link rejected: page does not mention %r: %s", must_mention, url)
        if self.store is not None:
            self.store.put_url_check(cache_key, result.status, result.final_url, ok)
        return ok

    def candidates(self, story: Story, localization: dict | None, exclude_urls: list[str] | None = None
                   ) -> list[LinkCandidate]:
        if not self.settings.internal_links_enabled or self.settings.max_internal_links <= 0:
            return []
        excluded = {url_identity(u) for u in exclude_urls or [] if u}
        out: list[LinkCandidate] = []
        seen: set[str] = set(excluded)

        def add(url: str, title: str, kind: str, entity: str = "", media: int = 0) -> None:
            ident = url_identity(url)
            if not url or ident in seen or len(out) >= self.settings.max_internal_links:
                return
            if self.settings.wp_base_url and site_host(url) != site_host(self.settings.wp_base_url):
                return
            if not self.validate(url, must_mention=entity if kind == "localization" else ""):
                return
            seen.add(ident)
            out.append(LinkCandidate(id=f"L{len(out) + 1}", url=url, title=title, kind=kind, entity=entity,
                                     media_id=media))

        if localization:
            add(localization["url"], f"{localization['name']} - "
                + ("فارسی‌ساز" if localization.get("kind") == "full" else "زیرنویس فارسی"),
                "localization", localization["name"], localization.get("featured_media", 0))
        if story.parent_post and story.parent_post.get("link"):
            add(story.parent_post["link"], story.parent_post.get("title", ""), "earlier_coverage", story.primary_display)
        if self.wp is not None and story.primary_display and story.primary_entity:
            try:
                posts = self.wp.search_posts(story.primary_display, 10)
            except Exception as exc:  # noqa: BLE001 - related links are optional
                log.info("Related-post search failed: %s", type(exc).__name__)
                posts = []
            for post in posts:
                if story.primary_entity in entity_key(post["title"]):
                    add(post["link"], post["title"], "related_coverage", story.primary_display)
                if len(out) >= self.settings.max_internal_links:
                    break
        return out


# ---------------------------------------------------------------------------
# Categories, tags, slug
# ---------------------------------------------------------------------------
def content_type_for(article: Article, story: Story) -> str:
    kinds = {e.kind for i in story.items for e in i.entities if e.key == story.primary_entity}
    if "hardware" in kinds:
        return "hardware"
    if "game" in kinds or "franchise" in kinds:
        return "gaming"
    if article.content_type in ("gaming", "hardware", "general"):
        return article.content_type
    return "hardware" if HARDWARE_HINT.search(story.headline) else "gaming"


def pick_categories(content_type: str, settings: Settings) -> list[int]:
    """Same category scheme as bot v1 (CAT_ALL + section). Reviews are never assigned by the news bot."""
    section = {"hardware": settings.cat_hardware, "gaming": settings.cat_gaming}.get(content_type, 0)
    cats = [c for c in (settings.cat_all, section) if c]
    if not cats and settings.cat_default:
        cats = [settings.cat_default]
    return list(dict.fromkeys(cats))


def sanitize_tags(names: list[str], max_tags: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in names:
        tag = re.sub(r"\s+", " ", clean_text(str(raw))).strip(" -–—:;,.")
        key = entity_key(tag)
        if len(tag) < 2 or len(tag) > 60 or not key or key in GENERIC_TAGS or key in seen or key.isdigit():
            continue
        seen.add(key)
        out.append(tag)
        if len(out) >= max_tags:
            break
    return out


def resolve_tags(names: list[str], wp, settings: Settings, dry_run: bool) -> tuple[list[int], list[str]]:
    """Reuse existing tags; create a new tag only when the policy allows (avoids thin tag archives)."""
    if not settings.tags_enabled or settings.tags_max <= 0 or wp is None:
        return [], ["tags disabled"]
    ids: list[int] = []
    notes: list[str] = []
    for name in sanitize_tags(names, settings.tags_max):
        try:
            existing = wp.find_tag(name)
        except Exception as exc:  # noqa: BLE001 - tags are optional
            notes.append(f"{name}: lookup failed ({type(exc).__name__})")
            continue
        if existing:
            ids.append(existing)
            notes.append(f"{name}: existing tag {existing}")
            continue
        create = settings.tag_create_policy == "always"
        if settings.tag_create_policy == "covered":
            try:
                create = wp.count_posts_mentioning(name) >= settings.tag_create_min_posts
            except Exception:  # noqa: BLE001
                create = False
        if not create:
            notes.append(f"{name}: no tag created (policy {settings.tag_create_policy})")
            continue
        if dry_run:
            notes.append(f"{name}: would create tag (dry-run)")
            continue
        try:
            ids.append(wp.create_tag(name))
            notes.append(f"{name}: created tag")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{name}: tag creation failed ({type(exc).__name__})")
    return list(dict.fromkeys(ids)), notes


def build_slug(article: Article, story: Story) -> str:
    slug = article.slug_en or make_latin_slug(story.headline)
    entity_slug = make_latin_slug(story.primary_display, max_words=5) if story.primary_display else ""
    if entity_slug and entity_slug not in slug:
        words = entity_slug.split("-") + [w for w in slug.split("-") if w not in entity_slug.split("-")]
        slug = "-".join(words[:8])
    return make_latin_slug(slug.replace("-", " "), max_words=8) or make_latin_slug(story.headline)
