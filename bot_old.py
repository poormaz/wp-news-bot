import os
import re
import json
import time
import base64
import hashlib
import sqlite3
import traceback
import html as html_lib
import logging
import sys
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, quote_plus, urlparse, urlsplit, urlunsplit, parse_qsl, urlencode

import yaml
import feedparser
import requests
from dotenv import load_dotenv
from openai import OpenAI
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from difflib import SequenceMatcher


load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("wp_news_bot")


def log_print(*args, sep=" ", end="\n", file=None, flush=False):
    """Route legacy print calls through logging without changing call sites."""
    message = sep.join(str(a) for a in args)
    if end and end != "\n":
        message += end
    if file is sys.stderr:
        logger.error(message)
    else:
        logger.info(message)


print = log_print

# =======================
# ENV
# =======================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "1").strip() or "1")

WP_BASE_URL = os.getenv("WP_BASE_URL", "").strip().rstrip("/")
WP_USERNAME = os.getenv("WP_USERNAME", "").strip()
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "").strip()
WP_POST_STATUS = os.getenv("WP_POST_STATUS", "draft").strip()

LANG = os.getenv("LANG", "fa").strip()

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "25").strip() or "25")
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (WPNewsBot/1.0; +https://example.com)").strip()

DB_FILE = os.getenv("DB_FILE", "news_cache.db").strip()
SOURCES_FILE = os.getenv("SOURCES_FILE", "sources.yaml").strip()
MANUAL_LINKS_FILE = os.getenv("MANUAL_LINKS_FILE", "manual_links.txt").strip()

# How many posts per run
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "1").strip() or "1")

# How many top RSS items per site to import each run
FEED_ENTRIES_LIMIT = int(os.getenv("FEED_ENTRIES_LIMIT", "10").strip() or "10")

# Rotation order (comma-separated). If empty => order from sources.yaml
ROTATION_SOURCES = os.getenv("ROTATION_SOURCES", "").strip()

# Categories
CAT_ALL = int(os.getenv("CAT_ALL", "0").strip() or "0")
CAT_GAMING = int(os.getenv("CAT_GAMING", "0").strip() or "0")
CAT_HARDWARE = int(os.getenv("CAT_HARDWARE", "0").strip() or "0")
CAT_REVIEWS = int(os.getenv("CAT_REVIEWS", "0").strip() or "0")
WP_CATEGORY_ID_DEFAULT = int(os.getenv("WP_CATEGORY_ID", "0").strip() or "0")

# RankMath updater (optional)
RANKMATH_UPDATER_URL = os.getenv("RANKMATH_UPDATER_URL", "").strip()
RANKMATH_UPDATER_TOKEN = os.getenv("RANKMATH_UPDATER_TOKEN", "").strip()

# Images
SET_FEATURED_IMAGE = os.getenv("SET_FEATURED_IMAGE", "1").strip() == "1"
EMBED_IMAGE_IN_CONTENT = os.getenv("EMBED_IMAGE_IN_CONTENT", "1").strip() == "1"

# Pexels fallback
PEXELS_ENABLED = os.getenv("PEXELS_ENABLED", "0").strip() == "1"
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "").strip()
PEXELS_ORIENTATION = os.getenv("PEXELS_ORIENTATION", "landscape").strip()
PEXELS_PER_PAGE = int(os.getenv("PEXELS_PER_PAGE", "1").strip() or "1")

# Source page text extraction (page_text)
USESOURCEPAGETEXT = (os.getenv("USESOURCEPAGETEXT") or os.getenv("USE_SOURCE_PAGE_TEXT") or "1").strip() == "1"
SOURCETEXTMAXCHARS = int((os.getenv("SOURCETEXTMAXCHARS") or os.getenv("SOURCE_TEXT_MAX_CHARS") or "15000").strip() or "15000")
SOURCE_TEXT_MAX_CHARS = SOURCETEXTMAXCHARS
USE_SOURCE_PAGE_TEXT = USESOURCEPAGETEXT

# Dedup (fuzzy by title_en)
DEDUP_WINDOW_HOURS = int(os.getenv("DEDUP_WINDOW_HOURS", "48").strip() or "48")
DEDUP_SIM_THRESHOLD = float(os.getenv("DEDUP_SIM_THRESHOLD", "0.80").strip() or "0.80")  # 0.75..0.85 typical

DEDUP_WP_CHECK = os.getenv("DEDUP_WP_CHECK", "1").strip() == "1"
DEDUP_WP_SEARCH_PER_PAGE = int(os.getenv("DEDUP_WP_SEARCH_PER_PAGE", "10").strip() or "10")

# Safety: don't post if title is too short (reduces false positives)
DEDUP_MIN_TOKENS = int(os.getenv("DEDUP_MIN_TOKENS", "5").strip() or "5")
# SEO helpers / quality gates
SEO_INTERNAL_LINKS_ENABLED = os.getenv("SEO_INTERNAL_LINKS_ENABLED", "1").strip() == "1"
SEO_INTERNAL_LINKS_MAX = int(os.getenv("SEO_INTERNAL_LINKS_MAX", "4").strip() or "4")
SEO_INTERNAL_LINKS_SEARCH_PER_PAGE = int(os.getenv("SEO_INTERNAL_LINKS_SEARCH_PER_PAGE", "8").strip() or "8")
SEO_REJECT_SHORT_POSTS = os.getenv("SEO_REJECT_SHORT_POSTS", "0").strip() == "1"
SEO_MIN_WORDS = int(os.getenv("SEO_MIN_WORDS", "650").strip() or "650")
SEO_USE_LATIN_SLUG = os.getenv("SEO_USE_LATIN_SLUG", "1").strip() == "1"
WP_TAGS_ENABLED = os.getenv("WP_TAGS_ENABLED", "1").strip() == "1"
WP_TAGS_MAX = int(os.getenv("WP_TAGS_MAX", "6").strip() or "6")

# HTTP shared session with retry/backoff
_HTTP_SESSION = None

def get_http_session():
    global _HTTP_SESSION
    if _HTTP_SESSION is None:
        s = requests.Session()
        retry = Retry(
            total=4,
            read=4,
            connect=4,
            backoff_factor=0.7,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _HTTP_SESSION = s
    return _HTTP_SESSION

def http_request(method: str, url: str, **kwargs):
    timeout = kwargs.pop("timeout", HTTP_TIMEOUT)
    return get_http_session().request(method.upper(), url, timeout=timeout, **kwargs)


# =======================
# Helpers
# =======================
def die(msg: str):
    raise SystemExit(msg)


def safe_env_report():
    keys = [
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "WP_BASE_URL",
        "WP_USERNAME",
        "WP_APP_PASSWORD",
        "PEXELS_API_KEY",
        "ROTATION_SOURCES",
        "FEED_ENTRIES_LIMIT",
        "MAX_POSTS_PER_RUN",
        "DEDUP_WINDOW_HOURS",
        "DEDUP_SIM_THRESHOLD",
        "DEDUP_WP_CHECK",
        "USE_SOURCE_PAGE_TEXT",
        "SOURCE_TEXT_MAX_CHARS",
        "SEO_INTERNAL_LINKS_ENABLED",
        "SEO_INTERNAL_LINKS_MAX",
        "SEO_REJECT_SHORT_POSTS",
        "SEO_MIN_WORDS",
        "SEO_USE_LATIN_SLUG",
        "WP_TAGS_ENABLED",
    ]
    print("ENV CHECK (safe):")
    for k in keys:
        v = os.getenv(k, "")
        print(f"- {k}: {'OK' if v else 'MISSING'} (len={len(v)})")


def clean_text(s: str) -> str:
    s = s or ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def html_escape(s: str) -> str:
    return html_lib.escape(clean_text(s), quote=True)


def normalize_list_field(value, max_items: int = 8) -> list[str]:
    """Accept a JSON list or a comma-separated string and return clean unique strings."""
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = re.split(r"[,،\n]+", value)
    elif isinstance(value, list):
        raw_items = value
    else:
        return []

    out = []
    seen = set()
    for item in raw_items:
        if isinstance(item, dict):
            # Common shapes: {"question": "..."} or {"name": "..."}
            item = item.get("name") or item.get("title") or item.get("keyword") or item.get("question") or ""
        item = clean_text(str(item))
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        out.append(item[:80])
        seen.add(key)
        if len(out) >= max_items:
            break
    return out


def normalize_faq_field(value, max_items: int = 4) -> list[dict]:
    """Normalize FAQ list returned by the model into [{question, answer}]."""
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if not isinstance(item, dict):
            continue
        q = clean_text(item.get("question") or item.get("q") or "")
        a = clean_text(item.get("answer") or item.get("a") or "")
        if q and a:
            out.append({"question": q[:160], "answer": a[:500]})
        if len(out) >= max_items:
            break
    return out


def persian_word_count(html: str) -> int:
    text_only = clean_text(html)
    words = re.findall(r"[\w\u0600-\u06FF]+", text_only, flags=re.UNICODE)
    return len([w for w in words if len(w) > 1])


def make_latin_slug(*parts: str, max_len: int = 85) -> str:
    """
    Build a readable Latin slug. Google can handle Persian slugs, but a short,
    stable Latin slug is easier to share and less absurd than percent-encoded soup.
    """
    if not SEO_USE_LATIN_SLUG:
        return ""

    base = " ".join(clean_text(p) for p in parts if clean_text(p))
    if not base:
        return ""

    # Prefer English words if present; otherwise transliteration is not attempted.
    base = unicodedata.normalize("NFKD", base)
    base = base.encode("ascii", "ignore").decode("ascii")
    base = base.lower()
    base = re.sub(r"[^a-z0-9\s-]", " ", base)
    base = re.sub(r"\s+", "-", base).strip("-")
    base = re.sub(r"-{2,}", "-", base)
    if not base:
        return ""
    return base[:max_len].rstrip("-")


def make_image_filename(base_slug: str, fallback_prefix: str, ext: str | None) -> str:
    ext = (ext or "jpg").lstrip(".").lower()
    slug = make_latin_slug(base_slug) or fallback_prefix
    return f"{slug[:75].strip('-')}.{ext}"


def read_manual_links() -> list[str]:
    if not os.path.exists(MANUAL_LINKS_FILE):
        return []
    with open(MANUAL_LINKS_FILE, "r", encoding="utf-8") as f:
        urls = []
        for line in f:
            line = (line or "").strip()
            if not line:
                continue
            if line.startswith("#"):
                continue
            urls.append(line)

    # unique با حفظ ترتیب
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            out.append(u)
            seen.add(u)
    return out

def clear_manual_links():
    with open(MANUAL_LINKS_FILE, "w", encoding="utf-8") as f:
        f.write("")

def title_from_url(url: str) -> str:
    p = urlparse(url)
    slug = (p.path.strip("/").split("/")[-1] if p.path else p.netloc) or p.netloc
    slug = re.sub(r"\.[a-z0-9]{3,4}$", "", slug, flags=re.I)
    slug = slug.replace("-", " ").replace("_", " ").strip()
    return clean_text(slug) or clean_text(p.netloc) or "News"

def process_manual_links_if_any() -> bool:
    print("MANUAL_LINKS_FILE =", os.path.abspath(MANUAL_LINKS_FILE))
    urls = read_manual_links()
    if not urls:
        return False

    print("=== MANUAL MODE: urls =", len(urls), "===")

    try:
        for url in urls:
            print("\nMANUAL URL:", url)
            
            source_name = urlparse(url).netloc.replace("www.", "") or "Source"
            print("MANUAL source_name:", source_name)
            
            # page_text (مثل روال فعلی)
            page_text = ""
            if USE_SOURCE_PAGE_TEXT:
                page_text = fetch_source_text_excerpt(url)
                print("page_text chars:", len(page_text))
                

            
            # عنوان از URL (سریع و بدون الکی‌کاری)
            title_en = title_from_url(url)

            # snippet کوتاه (اگر page_text داریم)
            snippet_en = clean_text((page_text or "")[:500])

            # Dedup (مثل RSS) - قبل از OpenAI برای صرفه‌جویی
            dup, why = is_duplicate_by_db(title_en)
            if dup:
                print("MANUAL SKIP duplicate (db):", why)
                continue

            dup2, why2 = wp_search_similar_posts(title_en)
            if dup2:
                print("MANUAL SKIP duplicate (wp):", why2)
                continue


            # تولید مقاله
            gen = openai_generate_fa_article(
                title_en=title_en,
                snippet_en=snippet_en,
                source_name=source_name,
                source_url=url,
                page_text=page_text,
            )
            post_slug = make_latin_slug(gen.get("slug_en", ""), title_en, gen.get("focus_keyword_fa", "")) or f"manual-{url_hash(url)[:12]}"
            image_alt = gen.get("image_alt_fa") or gen["title_fa"]
            tag_ids = wp_get_or_create_tag_ids(gen.get("tags_fa", []))
            related_links = wp_search_related_posts(title_en, gen.get("focus_keyword_fa", ""))

            # تصویر (همان منطق فعلی: سورس -> پیکسلز)
            featured_media_id = None
            image_html = ""
            image_credit_html = ""
            used_image_kind = "none"

            if SET_FEATURED_IMAGE:
                img_url = fetch_source_image_url(url)
                print("Source Image URL:", img_url)

                if not img_url:
                    photo = pexels_search_photo(normalize_en_title(title_en))
                    if photo:
                        img_url = pexels_pick_image_url(photo)
                        image_credit_html = pexels_attribution_html(photo)
                        used_image_kind = "pexels"
                        print("Pexels Image URL:", img_url)
                    else:
                        print("Pexels: no photo found.")
                else:
                    used_image_kind = "source"

                if img_url:
                    img_bytes, ext, mime = download_image_bytes(img_url)
                    if img_bytes:
                        fn = make_image_filename(post_slug, f"manual-{url_hash(url)[:12]}", ext)
                        media = wp_upload_media(
                            img_bytes,
                            fn,
                            mime_type=mime,
                            alt_text=image_alt,
                        )
                        featured_media_id = int(media["id"])
                        wp_src = (media.get("source_url") or "").strip()
                        if wp_src:
                            image_html = f'<p><img src="{html_escape(wp_src)}" alt="{html_escape(image_alt)}"></p>'
                        print("Featured media id:", featured_media_id, "| kind:", used_image_kind)
                    else:
                        print("No image bytes downloaded.")
                else:
                    print("No image found (source + pexels).")

            # ساخت محتوا + ارسال پست
            published_at = fetch_source_published_at(url) or datetime.utcnow().isoformat()

            content_html = build_wp_content(
                final_body_html=gen["content_html_fa"],
                source_name=source_name,
                source_url=url,
                published_at=published_at,
                image_html=image_html,
                image_credit_html=image_credit_html,
                related_links=related_links,
                faq_items=gen.get("faq_fa", []),
            )
            categories = pick_categories_manual(title_en, snippet_en)
            post_id = create_wp_post(
                title=gen["title_fa"],
                content_html=content_html,
                categories=categories,
                featured_media_id=featured_media_id,
                tags=tag_ids,
                slug=post_slug,
                excerpt=gen["meta_description_fa"],
            )

            push_rankmath_meta_wp(
                post_id=post_id,
                meta_title=gen["meta_title_fa"],
                meta_desc=gen["meta_description_fa"],
                focus_kw=gen["focus_keyword_fa"],
            )

            print("MANUAL POSTED:", post_id)
            time.sleep(1.2)

        return True

    finally:
        clear_manual_links()
        print("CLEARED:", os.path.abspath(MANUAL_LINKS_FILE), "size=", os.path.getsize(MANUAL_LINKS_FILE))


TRACKING_QS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid"}

def canonicalize_url(u: str) -> str:
    p = urlsplit((u or "").strip())
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in TRACKING_QS]
    qs.sort(key=lambda x: (x[0], x[1]))
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urlencode(qs), ""))

def url_hash(url: str) -> str:
    return hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()


def wp_auth_header(username: str, app_password: str) -> str:
    token = base64.b64encode(f"{username}:{app_password}".encode("utf-8")).decode("utf-8")
    return f"Basic {token}"


def parse_published_ts(published_at: str) -> int:
    try:
        dt = parsedate_to_datetime(published_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return int(datetime.now(timezone.utc).timestamp())


def format_rss_date(published_at: str) -> str:
    months_fa = {
        1: "ژانویه",
        2: "فوریه",
        3: "مارس",
        4: "اپریل",
        5: "می",
        6: "جوئن",
        7: "جولای",
        8: "آگوست",
        9: "سپتامبر",
        10: "اکتبر",
        11: "نوامبر",
        12: "دسامبر",
    }

    s = (published_at or "").strip()
    if not s:
        return ""

    dt = None

    # RSS/RFC dates
    try:
        dt = parsedate_to_datetime(s)
    except Exception:
        dt = None

    # ISO-8601 dates (common in meta tags / JSON-LD)
    if dt is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            dt = None

    if dt is None:
        return (s.split("+")[0]).strip()

    return f"{dt.day} {months_fa.get(dt.month, '')} {dt.year}"



def guess_ext_and_mime(content_type: str | None) -> tuple[str, str]:
    ct = (content_type or "").lower()
    if "png" in ct:
        return "png", "image/png"
    if "webp" in ct:
        return "webp", "image/webp"
    if "gif" in ct:
        return "gif", "image/gif"
    return "jpg", "image/jpeg"


STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "from", "by",
    "at", "is", "are", "was", "were", "be", "been", "being", "as", "this", "that",
    "these", "those", "new", "latest", "update", "updates",
}


def normalize_en_title(t: str) -> str:
    t = (t or "").lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    words = [w for w in t.split(" ") if w and w not in STOPWORDS and len(w) >= 3]
    return " ".join(words)[:220].strip()


def title_tokens(norm: str) -> list[str]:
    norm = (norm or "").strip()
    if not norm:
        return []
    return [w for w in norm.split(" ") if w]


def jaccard(a_tokens: list[str], b_tokens: list[str]) -> float:
    a = set(a_tokens)
    b = set(b_tokens)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def seq_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(a=a, b=b).ratio()


def title_similarity(title_a: str, title_b: str) -> float:
    """
    Combine sequence ratio + token Jaccard for robust fuzzy matching.
    """
    na = normalize_en_title(title_a)
    nb = normalize_en_title(title_b)
    ta = title_tokens(na)
    tb = title_tokens(nb)
    if len(ta) < DEDUP_MIN_TOKENS or len(tb) < DEDUP_MIN_TOKENS:
        return 0.0
    s1 = seq_ratio(na, nb)
    s2 = jaccard(ta, tb)
    return (0.65 * s1) + (0.35 * s2)


def basic_keywords_for_wp_search(title_en: str, max_words: int = 6) -> str:
    norm = normalize_en_title(title_en)
    toks = title_tokens(norm)[:max_words]
    return " ".join(toks).strip()


# =======================
# DB
# =======================
@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_FILE)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            source_name TEXT,
            title_en TEXT,
            title_norm TEXT,
            snippet_en TEXT,
            url TEXT,
            published_at TEXT,
            published_ts INTEGER,
            created_at TEXT,
            status TEXT,
            wp_post_id INTEGER
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )

    # Migrations (safe if older DB)
    c.execute("PRAGMA table_info(items)")
    cols = {row[1] for row in c.fetchall()}

    if "title_norm" not in cols:
        c.execute("ALTER TABLE items ADD COLUMN title_norm TEXT")
    if "published_ts" not in cols:
        c.execute("ALTER TABLE items ADD COLUMN published_ts INTEGER")

    c.execute("CREATE INDEX IF NOT EXISTS idx_items_status_source ON items(status, source_name)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_items_published_ts ON items(published_ts DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_items_title_norm ON items(title_norm)")

    conn.commit()
    conn.close()


def state_get(key: str, default: str = "") -> str:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM state WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default


def state_set(key: str, value: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def upsert_new_items(source_name: str, entries: list) -> int:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    added = 0
    now_iso = datetime.utcnow().isoformat()

    for e in entries:
        link = (e.get("link") or "").strip()
        if not link:
            continue

        hid = url_hash(link)
        title = clean_text(e.get("title", ""))
        snippet = clean_text(e.get("summary", "") or e.get("description", ""))
        published = (e.get("published") or e.get("updated") or "").strip()
        if not published:
            published = now_iso

        pts = parse_published_ts(published)
        tnorm = normalize_en_title(title)

        try:
            c.execute(
                """
                INSERT INTO items (
                    id, source_name, title_en, title_norm, snippet_en, url,
                    published_at, published_ts, created_at, status, wp_post_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (hid, source_name, title, tnorm, snippet, link, published, pts, now_iso, "pending", None),
            )
            added += 1
        except sqlite3.IntegrityError:
            pass

    conn.commit()
    conn.close()
    return added


def mark_posted(item_id: str, wp_post_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE items SET status='posted', wp_post_id=? WHERE id=?", (wp_post_id, item_id))
    conn.commit()
    conn.close()


def mark_failed(item_id: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE items SET status='failed' WHERE id=?", (item_id,))
    conn.commit()
    conn.close()


def mark_skipped(item_id: str, reason: str = ""):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE items SET status='skipped' WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    if reason:
        print("SKIPPED reason:", reason)


def get_recent_posted_or_skipped_titles() -> list[tuple[str, str, int]]:
    """
    returns list of (source_name, title_norm, published_ts)
    """
    cutoff = int(time.time()) - (DEDUP_WINDOW_HOURS * 3600)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """
        SELECT source_name, COALESCE(title_norm,''), COALESCE(published_ts,0)
        FROM items
        WHERE status IN ('posted','skipped') AND COALESCE(published_ts,0) >= ?
        ORDER BY COALESCE(published_ts,0) DESC
        LIMIT 300
        """,
        (cutoff,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def is_duplicate_by_db(title_en: str) -> tuple[bool, str]:
    candidates = get_recent_posted_or_skipped_titles()
    if not candidates:
        return False, ""

    best = 0.0
    best_src = ""
    for (src, tnorm, pts) in candidates:
        score = title_similarity(title_en, tnorm)
        if score > best:
            best = score
            best_src = src

    if best >= DEDUP_SIM_THRESHOLD:
        return True, f"db-sim={best:.3f} vs {best_src}"
    return False, ""


def get_next_pending_for_source(source_name: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """
        SELECT id, source_name, title_en, snippet_en, url, published_at
        FROM items
        WHERE status='pending' AND source_name=?
        ORDER BY COALESCE(published_ts, 0) DESC, created_at DESC
        LIMIT 1
        """,
        (source_name,),
    )
    row = c.fetchone()
    conn.close()
    return row


# =======================
# Sources (RSS)
# =======================
def load_sources():
    if not os.path.exists(SOURCES_FILE):
        die(f"Missing {SOURCES_FILE}")

    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    sources = cfg.get("sources", [])
    if not isinstance(sources, list) or not sources:
        die("sources.yaml is empty. Expected:\nsources:\n - name: ...\n   feed: ...")

    normalized = []
    for s in sources:
        if "name" not in s or "feed" not in s:
            die("Each source must have 'name' and 'feed'")
        normalized.append({"name": str(s["name"]).strip(), "feed": str(s["feed"]).strip()})
    return normalized


def fetch_feed_entries(feed_url: str):
    headers = {"User-Agent": USER_AGENT}
    try:
        r = http_request("GET", feed_url, headers=headers, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        print("FEED GET failed:", feed_url, "err:", repr(e))
        return []

    print("FEED GET", feed_url, "status", r.status_code, "| bytes", len(r.content))
    if r.status_code >= 400:
        print("FEED ERROR BODY first 250:", (r.text or "")[:250])
        return []

    parsed = feedparser.parse(r.text)
    entries = parsed.entries or []
    print("FEED PARSED entries", len(entries))
    return entries


# =======================
# Rotation
# =======================
def get_rotation_list(sources: list[dict]) -> list[str]:
    if ROTATION_SOURCES:
        return [x.strip() for x in ROTATION_SOURCES.split(",") if x.strip()]
    return [s["name"] for s in sources]


def choose_next_source_with_pending(rotation: list[str]) -> tuple[str | None, int | None]:
    if not rotation:
        return None, None

    try:
        rr_index = int(state_get("rr_index", "0") or "0")
    except ValueError:
        rr_index = 0

    for i in range(len(rotation)):
        idx = (rr_index + i) % len(rotation)
        src = rotation[idx]
        if get_next_pending_for_source(src):
            return src, idx
    return None, None


# =======================
# Categories
# =======================
def pick_categories(title_en: str, snippet_en: str) -> list[int]:
    # RSS behavior: keep as before (use title + snippet)
    text = ((title_en or "") + " " + (snippet_en or "")).lower()

    if CAT_REVIEWS and any(k in text for k in ["review", "hands-on", "preview", "impressions", "benchmark"]):
        return [CAT_REVIEWS]

    if CAT_HARDWARE and any(
        k in text
        for k in [
            "gpu",
            "rtx",
            "radeon",
            "cpu",
            "intel",
            "amd",
            "nvidia",
            "laptop",
            "ssd",
            "ram",
            "motherboard",
            "dlss",
            "fsr",
        ]
    ):
        return [CAT_ALL, CAT_HARDWARE] if CAT_ALL else [CAT_HARDWARE]

    if CAT_GAMING and any(k in text for k in ["game", "gaming", "steam", "ps5", "xbox", "nintendo", "dlc", "trailer"]):
        return [CAT_ALL, CAT_GAMING] if CAT_ALL else [CAT_GAMING]

    if CAT_ALL:
        return [CAT_ALL]

    if WP_CATEGORY_ID_DEFAULT > 0:
        return [WP_CATEGORY_ID_DEFAULT]

    return []

def pick_categories_manual(title_en: str, snippet_en: str) -> list[int]:
    # Manual behavior: decide by title only (prevents page_text containing "review" from forcing Reviews)
    title = (title_en or "").lower()

    if CAT_REVIEWS and any(k in title for k in ["review", "hands-on", "preview", "impressions", "benchmark"]):
        return [CAT_REVIEWS]

    if CAT_HARDWARE and any(
        k in title
        for k in [
            "gpu",
            "rtx",
            "radeon",
            "cpu",
            "intel",
            "amd",
            "nvidia",
            "laptop",
            "ssd",
            "ram",
            "motherboard",
            "dlss",
            "fsr",
        ]
    ):
        return [CAT_ALL, CAT_HARDWARE] if CAT_ALL else [CAT_HARDWARE]

    if CAT_GAMING and any(k in title for k in ["game", "gaming", "steam", "ps5", "xbox", "nintendo", "dlc", "trailer"]):
        return [CAT_ALL, CAT_GAMING] if CAT_ALL else [CAT_GAMING]

    if CAT_ALL:
        return [CAT_ALL]

    if WP_CATEGORY_ID_DEFAULT > 0:
        return [WP_CATEGORY_ID_DEFAULT]

    return []



def fetch_source_html(source_url: str) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    r = http_request("GET", source_url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
    if r.status_code >= 400:
        print("SOURCE HTML fetch failed:", r.status_code)
        return ""
    return r.text or ""


def extract_text_from_html(html: str, max_chars: int = SOURCE_TEXT_MAX_CHARS) -> str:
    raw = html or ""
    raw = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw)
    raw = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", raw)
    raw = re.sub(r"(?is)<noscript[^>]*>.*?</noscript>", " ", raw)
    raw = re.sub(r"(?s)<!--.*?-->", " ", raw)

    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?i)\b(read more|continue reading|see more|learn more)\b\s*[›>]+", " ", text)
    text = re.sub(r"\s*[›>]+\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return ""
    return text[:max_chars]


def fetch_source_text_excerpt(source_url: str, max_chars: int = SOURCE_TEXT_MAX_CHARS) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    try:
        r = http_request("GET", source_url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        print("SOURCE TEXT fetch failed:", repr(e))
        return ""

    if r.status_code >= 400:
        print("SOURCE TEXT fetch failed:", r.status_code)
        return ""

    return extract_text_from_html(r.text or "", max_chars=max_chars)

def _parse_json_strict(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("OpenAI returned empty content")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])

    raise ValueError("OpenAI returned non-JSON content that could not be parsed")
    



def _required_str(data: dict, key: str, min_len: int = 1, max_len: int | None = None) -> str:
    value = str(data.get(key) or "").strip()
    if len(value) < min_len:
        raise ValueError(f"Missing/too-short OpenAI field: {key}")
    if max_len is not None and len(value) > max_len:
        value = value[:max_len].rstrip()
    return value


def validate_article_payload(data: dict) -> dict:
    """Validate and lightly normalize OpenAI output without extra runtime dependencies."""
    if not isinstance(data, dict):
        raise ValueError("OpenAI article payload must be a JSON object")

    out = dict(data)
    out["title_fa"] = _required_str(out, "title_fa", min_len=3)
    out["meta_title_fa"] = _required_str(out, "meta_title_fa", min_len=3, max_len=70)
    out["meta_description_fa"] = _required_str(out, "meta_description_fa", min_len=3, max_len=160)
    out["focus_keyword_fa"] = _required_str(out, "focus_keyword_fa", min_len=2)
    out["content_html_fa"] = _required_str(out, "content_html_fa", min_len=20)

    # Optional SEO fields. If the model forgets them, the bot still works.
    out["slug_en"] = make_latin_slug(str(out.get("slug_en") or ""), str(out.get("title_fa") or ""))
    out["tags_fa"] = normalize_list_field(out.get("tags_fa"), max_items=WP_TAGS_MAX)
    out["image_alt_fa"] = clean_text(out.get("image_alt_fa") or out["title_fa"])[:140]
    out["faq_fa"] = normalize_faq_field(out.get("faq_fa"), max_items=4)

    wc = persian_word_count(out["content_html_fa"])
    if SEO_REJECT_SHORT_POSTS and wc < SEO_MIN_WORDS:
        raise ValueError(f"Generated article too short for SEO gate: {wc} words < {SEO_MIN_WORDS}")
    if wc < SEO_MIN_WORDS:
        print(f"WARN: generated article is short for SEO: {wc} words < {SEO_MIN_WORDS}")

    return out


def openai_generate_fa_article(
    title_en: str,
    snippet_en: str,
    source_name: str,
    source_url: str,
    page_text: str = "",
) -> dict:
    if not OPENAI_API_KEY:
        die("OPENAI_API_KEY is missing")

    client = OpenAI(api_key=OPENAI_API_KEY)

    prompt = f"""
You are a Persian (Farsi) gaming/tech editor and SEO editor for Poormaz.
Using the English inputs below, write a publish-ready Persian article that is useful enough to deserve indexing.

Inputs (English):
- Title: {title_en}
- Snippet: {snippet_en}
- Source name: {source_name}
- Source page excerpt: {page_text}

Hard rules:
- Do NOT invent facts, numbers, quotes, dates, names, platforms, prices, or claims.
- Rewrite in your own Persian words; do not copy source sentences verbatim.
- Mention the source only once near the beginning. Do not put the source URL inside the body.
- Keep a natural Persian newsroom tone, clear and readable for gaming readers.
- No clickbait. No fake certainty. No made-up controversy. Humanity has suffered enough.

SEO/content rules:
- Minimum 700 Persian words when the source excerpt has enough information.
- Use a strong lead paragraph, then 2 or 3 useful H2 headings.
- Allowed HTML: <p>, <h2>, <h3>, <ul>, <li>, <strong>.
- Use the focus keyword naturally in the title, first paragraph, at least one H2, and meta description.
- Add real context and implications only when they logically follow from the input.
- Avoid empty filler. Every paragraph should add something.
- Create 3 or 4 FAQ items based only on the article content.
- Create 4 to 6 useful Persian tags, not random one-word junk.

Output JSON only with these keys:
- title_fa
- meta_title_fa (max 70 chars)
- meta_description_fa (max 160 chars)
- focus_keyword_fa
- slug_en (lowercase English words with hyphens; include the main game/product name if present)
- tags_fa (array of 4-6 Persian tags)
- image_alt_fa
- faq_fa (array of objects: question, answer)
- content_html_fa (valid HTML using only the allowed tags)
""".strip()

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=OPENAI_TEMPERATURE,
            max_tokens=2400,
            messages=[
                {"role": "system", "content": "Return valid JSON only."},
                {
                    "role": "user",
                    "content": (
                        prompt
                        + "\n\nReturn JSON with keys: title_fa, meta_title_fa, meta_description_fa, focus_keyword_fa, slug_en, tags_fa, image_alt_fa, faq_fa, content_html_fa"
                    ),
                },
            ],
            response_format={"type": "json_object"},
        )
        text = (resp.choices[0].message.content or "").strip()
        data = _parse_json_strict(text)

    except Exception as e:
        print("WARN: json_object failed; retrying once. Error:", repr(e))
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=OPENAI_TEMPERATURE,
            max_tokens=2400,
            messages=[
                {"role": "system", "content": "Return valid JSON only."},
                {
                    "role": "user",
                    "content": (
                        prompt
                        + "\n\nReturn JSON with keys: title_fa, meta_title_fa, meta_description_fa, focus_keyword_fa, slug_en, tags_fa, image_alt_fa, faq_fa, content_html_fa"
                    ),
                },
            ],
            response_format={"type": "json_object"},
        )
        text = (resp.choices[0].message.content or "").strip()
        data = _parse_json_strict(text)


    # Validate required fields
    required = [
        "title_fa",
        "meta_title_fa",
        "meta_description_fa",
        "focus_keyword_fa",
        "content_html_fa",
    ]
    for k in required:
        if k not in data:
            raise ValueError(f"Missing key in OpenAI JSON: {k}")

    # Clean meta strings (assuming clean_text exists in your file)
    data["title_fa"] = clean_text(data.get("title_fa", ""))
    data["meta_title_fa"] = clean_text(data.get("meta_title_fa", ""))[:70]
    data["meta_description_fa"] = clean_text(data.get("meta_description_fa", ""))[:160]
    data["focus_keyword_fa"] = clean_text(data.get("focus_keyword_fa", ""))
    data["slug_en"] = make_latin_slug(data.get("slug_en", ""), title_en)
    data["tags_fa"] = normalize_list_field(data.get("tags_fa"), max_items=WP_TAGS_MAX)
    data["image_alt_fa"] = clean_text(data.get("image_alt_fa", "") or data["title_fa"])[:140]
    data["faq_fa"] = normalize_faq_field(data.get("faq_fa"), max_items=4)

    htmlout = (data.get("content_html_fa") or "").strip()

    # Remove accidental source URL if model leaked it
    if source_url:
        htmlout = htmlout.replace(source_url, "").strip()

    # Optional: remove “see source” phrases
    htmlout = re.sub(r"(?im)\b(برای اطلاعات بیشتر.*|جزئیات بیشتر.*|در منبع.*)\b", "", htmlout).strip()

    data["content_html_fa"] = htmlout
    data = validate_article_payload(data)
    return data



# =======================
# WordPress API
# =======================
def wp_request_headers(json_mode: bool = False) -> dict:
    h = {"Authorization": wp_auth_header(WP_USERNAME, WP_APP_PASSWORD), "User-Agent": USER_AGENT}
    if json_mode:
        h["Content-Type"] = "application/json"
        h["Accept"] = "application/json"
    return h


def wp_check_me():
    endpoint = f"{WP_BASE_URL}/wp-json/wp/v2/users/me"
    r = http_request("GET", endpoint, headers=wp_request_headers(), timeout=HTTP_TIMEOUT)
    print("WP ME:", r.status_code)
    if r.status_code >= 400:
        print("WP ME error body (first 300):", r.text[:300])
        r.raise_for_status()
    return r.json()


def wp_upload_media(image_bytes: bytes, filename: str, mime_type: str, alt_text: str = "") -> dict:
    endpoint = f"{WP_BASE_URL}/wp-json/wp/v2/media"
    headers = wp_request_headers()
    headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    headers["Content-Type"] = mime_type

    r = http_request("POST", endpoint, headers=headers, data=image_bytes, timeout=HTTP_TIMEOUT)
    print("WP MEDIA UPLOAD:", r.status_code)
    if r.status_code >= 400:
        print("WP MEDIA ERROR (first 400):", r.text[:400])
        r.raise_for_status()

    media = r.json()

    # Set alt text (optional)
    if alt_text:
        patch = http_request("POST", 
            f"{WP_BASE_URL}/wp-json/wp/v2/media/{media['id']}",
            headers=wp_request_headers(json_mode=True),
            json={"alt_text": alt_text},
            timeout=HTTP_TIMEOUT,
        )
        print("WP MEDIA ALT PATCH:", patch.status_code)

    return media


def wp_get_or_create_tag_ids(tag_names: list[str]) -> list[int]:
    if not (WP_TAGS_ENABLED and tag_names):
        return []

    ids = []
    endpoint = f"{WP_BASE_URL}/wp-json/wp/v2/tags"

    for tag in normalize_list_field(tag_names, max_items=WP_TAGS_MAX):
        try:
            # Search first to avoid duplicate tags with tiny spelling differences.
            sr = http_request(
                "GET",
                endpoint,
                headers=wp_request_headers(),
                params={"search": tag, "per_page": "10"},
                timeout=HTTP_TIMEOUT,
            )
            found_id = None
            if sr.status_code < 400:
                for t in (sr.json() or []):
                    if clean_text(t.get("name", "")).lower() == tag.lower():
                        found_id = int(t["id"])
                        break
                if found_id:
                    ids.append(found_id)
                    continue

            cr = http_request(
                "POST",
                endpoint,
                headers=wp_request_headers(json_mode=True),
                json={"name": tag},
                timeout=HTTP_TIMEOUT,
            )
            print("WP TAG:", cr.status_code, "|", tag)
            if cr.status_code < 400:
                ids.append(int(cr.json()["id"]))
            else:
                # If term already exists, WordPress may return term_id in error data.
                try:
                    data = cr.json()
                    term_id = ((data.get("data") or {}).get("term_id"))
                    if term_id:
                        ids.append(int(term_id))
                except Exception:
                    print("WP TAG ERROR:", (cr.text or "")[:200])
        except requests.RequestException as e:
            print("WP TAG failed:", tag, repr(e))

    # unique, stable order
    out = []
    seen = set()
    for tid in ids:
        if tid not in seen:
            out.append(tid)
            seen.add(tid)
    return out[:WP_TAGS_MAX]


def wp_search_related_posts(title_en: str, focus_keyword_fa: str = "", limit: int | None = None) -> list[dict]:
    """
    Find existing published posts for an internal-link block.
    This is deliberately conservative: published posts only, same WP site only.
    """
    if not SEO_INTERNAL_LINKS_ENABLED:
        return []

    limit = int(limit or SEO_INTERNAL_LINKS_MAX)
    if limit <= 0:
        return []

    queries = []
    q1 = basic_keywords_for_wp_search(title_en, max_words=5)
    if q1:
        queries.append(q1)
    q2 = clean_text(focus_keyword_fa)
    if q2 and q2 not in queries:
        queries.append(q2)

    endpoint = f"{WP_BASE_URL}/wp-json/wp/v2/posts"
    found = []
    seen = set()

    for q in queries:
        try:
            r = http_request(
                "GET",
                endpoint,
                headers=wp_request_headers(),
                params={
                    "search": q,
                    "per_page": str(max(limit, SEO_INTERNAL_LINKS_SEARCH_PER_PAGE)),
                    "status": "publish",
                    "_fields": "id,link,title",
                },
                timeout=HTTP_TIMEOUT,
            )
            print("WP RELATED SEARCH:", r.status_code, "| q:", q)
            if r.status_code >= 400:
                continue

            for p in (r.json() or []):
                link = (p.get("link") or "").strip()
                title = clean_text(((p.get("title") or {}).get("rendered") or ""))
                pid = int(p.get("id") or 0)
                if not (pid and link and title):
                    continue
                if WP_BASE_URL and not link.startswith(WP_BASE_URL):
                    continue
                if pid in seen:
                    continue
                found.append({"id": pid, "title": title, "link": link})
                seen.add(pid)
                if len(found) >= limit:
                    return found
        except requests.RequestException as e:
            print("WP RELATED SEARCH failed:", repr(e))

    return found[:limit]


def create_wp_post(
    title: str,
    content_html: str,
    categories: list[int],
    featured_media_id: int | None = None,
    tags: list[int] | None = None,
    slug: str = "",
    excerpt: str = "",
) -> int:
    endpoint = f"{WP_BASE_URL}/wp-json/wp/v2/posts"
    payload = {"title": title, "content": content_html, "status": WP_POST_STATUS}

    if categories:
        payload["categories"] = categories
    if tags:
        payload["tags"] = tags
    if featured_media_id is not None:
        payload["featured_media"] = featured_media_id
    if slug:
        payload["slug"] = make_latin_slug(slug)
    if excerpt:
        payload["excerpt"] = clean_text(excerpt)[:300]

    r = http_request("POST", endpoint, headers=wp_request_headers(json_mode=True), json=payload, timeout=HTTP_TIMEOUT)
    print("WP POST:", r.status_code)
    if r.status_code >= 400:
        print("WP POST ERROR (first 500):", r.text[:500])
        r.raise_for_status()

    return int(r.json()["id"])


def wp_search_similar_posts(title_en: str) -> tuple[bool, str]:
    """
    Layer 2 dedup: ask WP if a similar post already exists (draft/publish).
    Uses wp/v2/posts?search=...
    """
    if not DEDUP_WP_CHECK:
        return False, ""

    q = basic_keywords_for_wp_search(title_en)
    if not q:
        return False, ""

    endpoint = f"{WP_BASE_URL}/wp-json/wp/v2/posts"
    params = {"search": q, "per_page": str(DEDUP_WP_SEARCH_PER_PAGE), "status": "any"}

    r = http_request("GET", endpoint, headers=wp_request_headers(), params=params, timeout=HTTP_TIMEOUT)
    print("WP SEARCH:", r.status_code, "| q:", q)
    if r.status_code >= 400:
        print("WP SEARCH error (first 300):", r.text[:300])
        return False, ""

    posts = r.json() or []
    best = 0.0
    best_title = ""
    for p in posts:
        rendered = ((p.get("title") or {}).get("rendered") or "").strip()
        rendered = clean_text(rendered)
        score = title_similarity(title_en, rendered)
        if score > best:
            best = score
            best_title = rendered

    if best >= DEDUP_SIM_THRESHOLD:
        return True, f"wp-sim={best:.3f} title={best_title[:80]}"
    return False, ""

def push_rankmath_meta_wp(post_id: int, meta_title: str, meta_desc: str, focus_kw: str):
    endpoint = f"{WP_BASE_URL}/wp-json/rankmath/v1/updateMeta"
    payload = {
        "objectType": "post",
        "objectID": int(post_id),
        "meta": {
            "rank_math_title": meta_title or "",
            "rank_math_description": meta_desc or "",
            "rank_math_focus_keyword": focus_kw or "",
        }
    }
    r = http_request("POST", endpoint, headers=wp_request_headers(json_mode=True), json=payload, timeout=HTTP_TIMEOUT)
    print("RANKMATH updateMeta:", r.status_code, "| body:", (r.text or "")[:200])
    return r.status_code < 400

# =======================
# Images
# =======================
class _ImageMetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.og_image = ""
        self.twitter_image = ""
        self.first_img = ""

    def handle_starttag(self, tag: str, attrs):
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() == "meta":
            prop = attr.get("property", "").lower()
            name = attr.get("name", "").lower()
            content = attr.get("content", "").strip()
            if prop == "og:image" and content and not self.og_image:
                self.og_image = content
            elif name == "twitter:image" and content and not self.twitter_image:
                self.twitter_image = content
        elif tag.lower() == "img" and not self.first_img:
            src = attr.get("src", "").strip()
            if src:
                self.first_img = src


def extract_image_url_from_html(html: str, base_url: str) -> str | None:
    """Extract a representative image using stdlib HTML parsing, then regex fallback."""
    parser = _ImageMetaParser()
    try:
        parser.feed(html or "")
    except Exception:
        logger.debug("HTML parser could not fully parse image metadata", exc_info=True)

    for candidate in (parser.og_image, parser.twitter_image, parser.first_img):
        if candidate:
            return urljoin(base_url, candidate.strip())

    # Regex fallback for malformed markup
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html or "", re.I)
    if m:
        return urljoin(base_url, m.group(1).strip())

    m = re.search(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', html or "", re.I)
    if m:
        return urljoin(base_url, m.group(1).strip())

    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html or "", re.I)
    if m:
        return urljoin(base_url, m.group(1).strip())

    return None

def extract_published_at_from_html(html: str) -> str:
    html = html or ""
    patterns = [
        r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:published_time["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']pubdate["\'][^>]+content=["\']([^"\']+)["\']',
        r'<time[^>]+datetime=["\']([^"\']+)["\']',
        r'"datePublished"\\s*:\\s*"([^"]+)"',  # JSON-LD
    ]
    for pat in patterns:
        m = re.search(pat, html, re.I)
        if m:
            return (m.group(1) or "").strip()
    return ""

def fetch_source_published_at(source_url: str) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    r = http_request("GET", source_url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
    if r.status_code >= 400:
        return ""
    return extract_published_at_from_html(r.text)

def fetch_source_image_url(source_url: str) -> str | None:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    r = http_request("GET", source_url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
    print("SOURCE HTML:", r.status_code, "| bytes:", len(r.content))
    if r.status_code >= 400:
        print("SOURCE HTML fetch failed:", r.status_code)
        return None
    return extract_image_url_from_html(r.text, source_url)

def download_image_bytes(img_url: str) -> tuple[bytes | None, str | None, str | None]:
    headers = {"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    r = http_request("GET", img_url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
    if r.status_code >= 400 or not r.content:
        print("IMG download failed:", r.status_code, img_url)
        return None, None, None
    ext, mime = guess_ext_and_mime(r.headers.get("Content-Type"))
    return r.content, ext, mime


def pexels_search_photo(query: str) -> dict | None:
    if not (PEXELS_ENABLED and PEXELS_API_KEY):
        return None

    q = (query or "").strip() or "technology"
    endpoint = "https://api.pexels.com/v1/search"
    url = f"{endpoint}?query={quote_plus(q)}&per_page={PEXELS_PER_PAGE}&orientation={quote_plus(PEXELS_ORIENTATION)}"
    headers = {"Authorization": PEXELS_API_KEY, "User-Agent": USER_AGENT, "Accept": "application/json"}

    r = http_request("GET", url, headers=headers, timeout=HTTP_TIMEOUT)
    print("PEXELS SEARCH:", r.status_code, "| query:", q)
    if r.status_code >= 400:
        print("PEXELS ERROR (first 300):", r.text[:300])
        return None

    data = r.json() or {}
    photos = data.get("photos") or []
    return photos[0] if photos else None


def pexels_pick_image_url(photo: dict) -> str | None:
    if not photo:
        return None
    src = (photo.get("src") or {})
    return src.get("large2x") or src.get("large") or src.get("original") or src.get("medium")


def pexels_attribution_html(photo: dict) -> str:
    if not photo:
        return ""
    photographer = clean_text(photo.get("photographer", ""))
    photopage = (photo.get("url") or "").strip()
    if photopage and photographer:
        return f"<p><small>Image: Pexels — {photographer} — <a href=\"{photopage}\" target=\"_blank\" rel=\"nofollow noopener\">Link</a></small></p>"
    if photopage:
        return f"<p><small>Image: Pexels — <a href=\"{photopage}\" target=\"_blank\" rel=\"nofollow noopener\">Link</a></small></p>"
    return "<p><small>Image: Pexels</small></p>"


# =======================
# Content builder
# =======================
def build_related_links_html(related_links: list[dict]) -> str:
    if not related_links:
        return ""

    lis = []
    for item in related_links[:SEO_INTERNAL_LINKS_MAX]:
        title = html_escape(item.get("title", ""))
        link = html_escape(item.get("link", ""))
        if title and link:
            lis.append(f'<li><a href="{link}">{title}</a></li>')

    if not lis:
        return ""

    return "<h2>مطالب مرتبط</h2>\n<ul>\n" + "\n".join(lis) + "\n</ul>"


def build_faq_html(faq_items: list[dict]) -> str:
    faq_items = normalize_faq_field(faq_items, max_items=4)
    if not faq_items:
        return ""

    parts = ["<h2>سوالات متداول</h2>"]
    for item in faq_items:
        q = html_escape(item["question"])
        a = html_escape(item["answer"])
        parts.append(f"<h3>{q}</h3>")
        parts.append(f"<p>{a}</p>")
    return "\n".join(parts)


def build_wp_content(
    final_body_html: str,
    source_name: str,
    source_url: str,
    published_at: str,
    image_html: str = "",
    image_credit_html: str = "",
    related_links: list[dict] | None = None,
    faq_items: list[dict] | None = None,
) -> str:
    nice_date = format_rss_date(published_at) if (published_at or "").strip() else ""

    header_media = ""
    if image_html and EMBED_IMAGE_IN_CONTENT:
        header_media = (image_html or "").strip() + "\n" + (image_credit_html or "").strip()

    related_html = build_related_links_html(related_links or [])
    faq_html = build_faq_html(faq_items or [])

    footer = (
        "<hr/>"
        f"<p><strong>منبع:</strong> {html_escape(source_name)} — "
        f"<a href=\"{html_escape(source_url)}\" target=\"_blank\" rel=\"nofollow noopener\">لینک</a>"
        f"<br/><strong>زمان انتشار منبع:</strong> {html_escape(nice_date)}</p>"
    )

    parts = []
    if header_media.strip():
        parts.append(header_media.strip())
    parts.append((final_body_html or "").strip())
    if related_html.strip():
        parts.append(related_html.strip())
    if faq_html.strip():
        parts.append(faq_html.strip())
    parts.append(footer)

    return "\n\n".join([p for p in parts if p]).strip()


# =======================
# Main
# =======================
def run():
    print("=== WP News Bot starting ===")
    safe_env_report()

    if LANG.lower() != "fa":
        die("LANG باید fa باشد")

    if not (WP_BASE_URL and WP_USERNAME and WP_APP_PASSWORD):
        die("WP_BASE_URL / WP_USERNAME / WP_APP_PASSWORD خالی است")

    wp_check_me()
    init_db()
    
    if process_manual_links_if_any():
        return
    
    sources = load_sources()
    print("Sources loaded:", len(sources))
    for s in sources:
        print("-", s["name"], s["feed"])

    rotation = get_rotation_list(sources)
    print("Rotation order:", rotation)

    # 1) Import newest items from each source
    total_added = 0
    for s in sources:
        entries = fetch_feed_entries(s["feed"])
        newest = entries[: max(1, FEED_ENTRIES_LIMIT)]
        added = upsert_new_items(s["name"], newest)
        print(f"ADDED from {s['name']}: {added}")
        total_added += added

    # 2) Post up to MAX_POSTS_PER_RUN, respecting rotation
    processed_posts = 0

    for _ in range(max(1, MAX_POSTS_PER_RUN)):
        src, src_idx = choose_next_source_with_pending(rotation)
        if src is None:
            print("No pending items found for any source.")
            break

        # advance rr_index to next source (so we don't get stuck)
        next_idx = (int(src_idx) + 1) % len(rotation)
        state_set("rr_index", str(next_idx))
        print(f"\n=== TURN: source='{src}' (next rr_index={next_idx}) ===")

        # For this source: try newest pending; if duplicate => skip and try next pending (same source, same run)
        while True:
            row = get_next_pending_for_source(src)
            if not row:
                print(f"No more pending items for source '{src}'.")
                break

            (item_id, source_name, title_en, snippet_en, url, published_at) = row

            print("\n--- ITEM ---")
            print("Source:", source_name)
            print("URL:", url)
            print("Title EN:", title_en)

            try:
                # Dedup layer 1: DB fuzzy by title_en within window
                dup, why = is_duplicate_by_db(title_en)
                if dup:
                    mark_skipped(item_id, reason=f"duplicate: {why}")
                    continue

                # Dedup layer 2: WP search fuzzy
                dup2, why2 = wp_search_similar_posts(title_en)
                if dup2:
                    mark_skipped(item_id, reason=f"duplicate: {why2}")
                    continue
                categories = pick_categories(title_en, snippet_en)   # RSS = مثل قبل
                print("Picked categories:", categories)


                # Build page_text
                page_text = ""
                if USE_SOURCE_PAGE_TEXT:
                    page_text = fetch_source_text_excerpt(url)
                    print("page_text chars:", len(page_text))

                # Generate Persian article
                gen = openai_generate_fa_article(title_en, snippet_en, source_name, url, page_text=page_text)
                post_slug = make_latin_slug(gen.get("slug_en", ""), title_en, gen.get("focus_keyword_fa", "")) or f"news-{item_id[:12]}"
                image_alt = gen.get("image_alt_fa") or gen["title_fa"]
                tag_ids = wp_get_or_create_tag_ids(gen.get("tags_fa", []))
                related_links = wp_search_related_posts(title_en, gen.get("focus_keyword_fa", ""))

                # Featured image handling
                featured_media_id = None
                image_html = ""
                image_credit_html = ""
                used_image_kind = "none"

                if SET_FEATURED_IMAGE:
                    img_url = fetch_source_image_url(url)
                    print("Source Image URL:", img_url)

                    if not img_url:
                        photo = pexels_search_photo(normalize_en_title(title_en))
                        if photo:
                            img_url = pexels_pick_image_url(photo)
                            image_credit_html = pexels_attribution_html(photo)
                            used_image_kind = "pexels"
                            print("Pexels Image URL:", img_url)
                        else:
                            print("Pexels: no photo found.")
                    else:
                        used_image_kind = "source"

                    if img_url:
                        img_bytes, ext, mime = download_image_bytes(img_url)
                        if img_bytes:
                            fn = make_image_filename(post_slug, f"news-{item_id[:12]}", ext)
                            media = wp_upload_media(img_bytes, fn, mime_type=mime, alt_text=image_alt)
                            featured_media_id = int(media["id"])
                            wp_src = (media.get("source_url") or "").strip()
                            if wp_src:
                                image_html = f'<p><img src="{html_escape(wp_src)}" alt="{html_escape(image_alt)}"/></p>'
                            print("Featured media id:", featured_media_id, "| kind:", used_image_kind)
                        else:
                            print("No image bytes downloaded.")
                    else:
                        print("No image found (source + pexels).")

                # Build full WP post body
                content_html = build_wp_content(
                    final_body_html=gen["content_html_fa"],
                    source_name=source_name,
                    source_url=url,
                    published_at=published_at,
                    image_html=image_html,
                    image_credit_html=image_credit_html,
                    related_links=related_links,
                    faq_items=gen.get("faq_fa", []),
                )

                # Create WP post
                post_id = create_wp_post(
                    title=gen["title_fa"],
                    content_html=content_html,
                    categories=categories,
                    featured_media_id=featured_media_id,
                    tags=tag_ids,
                    slug=post_slug,
                    excerpt=gen["meta_description_fa"],
                )

                # RankMath meta (optional)
                push_rankmath_meta_wp(
                    post_id=post_id,
                    meta_title=gen["meta_title_fa"],
                    meta_desc=gen["meta_description_fa"],
                    focus_kw=gen["focus_keyword_fa"],
                )

                print("POSTED:", post_id)
                mark_posted(item_id, post_id)
                processed_posts += 1

                time.sleep(1.2)
                break  # done with this source for this "turn"

            except requests.RequestException as e:
                logger.warning("Network failure for %s: %r", url, e)
                mark_failed(item_id)
                break
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                logger.error("Data failure for %s: %r", url, e)
                mark_failed(item_id)
                break
            except Exception as e:
                logger.exception("Unhandled failure for %s", url)
                mark_failed(item_id)
                break

        if processed_posts >= MAX_POSTS_PER_RUN:
            break

    print(f"\nDone. Added from feeds: {total_added}, posted now: {processed_posts}")


if __name__ == "__main__":
    run()


