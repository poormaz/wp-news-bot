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
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.4").strip() or "0.4")
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "2200").strip() or "2200")

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

# WordPress taxonomy (native tags + more reliable category routing)
AUTO_TAGS_ENABLED = os.getenv("AUTO_TAGS_ENABLED", "1").strip() == "1"
AUTO_TAGS_CREATE_MISSING = os.getenv("AUTO_TAGS_CREATE_MISSING", "1").strip() == "1"
AUTO_TAGS_MAX = max(0, min(5, int(os.getenv("AUTO_TAGS_MAX", "3").strip() or "3")))

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
        "AUTO_TAGS_ENABLED",
        "AUTO_TAGS_CREATE_MISSING",
        "AUTO_TAGS_MAX",
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


def clean_source_display_name(value: str) -> str:
    """Keep a publisher's readable brand name, but never show a trailing .com."""
    name = clean_text(value)
    # Site metadata occasionally calls itself "Example.com". The user sees the
    # linked label, so repeating the domain suffix adds nothing except visual clutter.
    name = re.sub(
        r"\.(?:com|net|org|io|co|gg|tv|info|biz|me|de|fr|it|es|us|ca|au|uk|co\.uk)\s*$",
        "",
        name,
        flags=re.I,
    ).strip()
    return name


def source_display_name_from_url(url: str) -> str:
    """A readable fallback for manual links when og:site_name is unavailable."""
    host = (urlparse(url).hostname or "").strip()
    host = re.sub(r"^www\.", "", host, flags=re.I)
    return clean_source_display_name(host) or "Source"

def process_manual_links_if_any() -> bool:
    print("MANUAL_LINKS_FILE =", os.path.abspath(MANUAL_LINKS_FILE))
    urls = read_manual_links()
    if not urls:
        return False

    print("=== MANUAL MODE: urls =", len(urls), "===")

    try:
        for url in urls:
            print("\nMANUAL URL:", url)

            # One source request provides a real title, site name, article text, image and date.
            # URL slugs are a poor substitute for headlines, particularly for tags.
            source_page = fetch_source_page_data(url)
            source_name = clean_source_display_name(source_page.get("site_name", "")) or source_display_name_from_url(url)
            print("MANUAL source_name:", source_name)

            page_text = source_page.get("text", "") if USE_SOURCE_PAGE_TEXT else ""
            print("page_text chars:", len(page_text))

            title_en = source_page.get("title") or title_from_url(url)
            snippet_en = clean_text((page_text or "")[:500])

            dup, why = is_duplicate_by_db(title_en)
            if dup:
                print("MANUAL SKIP duplicate (db):", why)
                continue

            dup2, why2 = wp_search_similar_posts(title_en)
            if dup2:
                print("MANUAL SKIP duplicate (wp):", why2)
                continue

            gen = openai_generate_fa_article(
                title_en=title_en,
                snippet_en=snippet_en,
                source_name=source_name,
                source_url=url,
                page_text=page_text,
            )
            categories = pick_categories_manual(
                title_en,
                snippet_en,
                content_type=gen.get("content_type"),
                page_text=page_text,
            )
            tag_ids = resolve_wp_tag_ids(gen.get("entity_tags", []))
            print("Picked category type:", gen.get("content_type"), "| categories:", categories, "| entity tags:", gen.get("entity_tags", []))

            featured_media_id = None
            image_html = ""
            image_credit_html = ""
            used_image_kind = "none"

            if SET_FEATURED_IMAGE:
                img_url = source_page.get("image_url") or fetch_source_image_url(url)
                if img_url and not is_valid_source_image_url(img_url):
                    print("Rejected non-article source image URL:", img_url)
                    img_url = None
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
                        fn = f"manual-{url_hash(url)[:12]}.{ext}"
                        media = wp_upload_media(img_bytes, fn, mime_type=mime, alt_text=gen["title_fa"])
                        featured_media_id = int(media["id"])
                        wp_src = (media.get("source_url") or "").strip()
                        if wp_src:
                            image_html = f'<p><img src="{wp_src}" alt="{html_lib.escape(gen["title_fa"], quote=True)}"></p>'
                        print("Featured media id:", featured_media_id, "| kind:", used_image_kind)
                    else:
                        print("No image bytes downloaded.")
                else:
                    print("No image found (source + pexels).")

            published_at = source_page.get("published_at") or datetime.utcnow().isoformat()
            content_html = build_wp_content(
                final_body_html=gen["content_html_fa"],
                source_name=source_name,
                source_url=url,
                published_at=published_at,
                image_html=image_html,
                image_credit_html=image_credit_html,
                featured_media_id=featured_media_id,
                image_alt=gen["title_fa"],
            )
            post_id = create_wp_post(
                title=gen["title_fa"],
                content_html=content_html,
                categories=categories,
                featured_media_id=featured_media_id,
                tag_ids=tag_ids,
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
# Categories + native WordPress tags
# =======================
CONTENT_TYPES = {"gaming", "hardware", "review", "general"}

# These are deliberately phrases rather than a single loose keyword. Humans already
# invented enough ways to mislabel a graphics-card story as a game just because it
# mentions Steam once.
REVIEW_PATTERNS = [
    r"\breview\b", r"\bhands[- ]on\b", r"\bpreview\b", r"\bimpressions?\b",
    r"\bbenchmark(?:s|ed|ing)?\b",
]
HARDWARE_PATTERNS = [
    r"\bgpu\b", r"\bgraphics card\b", r"\bgeforce\b", r"\brtx\b", r"\bradeon\b",
    r"\bcpu\b", r"\bprocessor\b", r"\bchipset\b", r"\bchip\b", r"\bap[u]\b",
    r"\bryzen\b", r"\bcore ultra\b", r"\bintel arc\b", r"\bnvidia\b", r"\bamd\b",
    r"\bssd\b", r"\bram\b", r"\bddr[345]\b", r"\bmotherboard\b", r"\blaptop\b",
    r"\bmonitor\b", r"\bdisplay\b", r"\boled\b", r"\bhandheld\b", r"\bsteam deck\b",
    r"\brog ally\b", r"\bkeyboard\b", r"\bmouse\b", r"\bheadset\b", r"\bcontroller\b",
    r"\bdriver\b", r"\bfirmware\b", r"\bqualcomm\b", r"\bmediatek\b",
]
GAMING_PATTERNS = [
    r"\bvideo game\b", r"\bgameplay\b", r"\bgaming\b", r"\bgame\b", r"\bdlc\b",
    r"\bexpansion\b", r"\btrailer\b", r"\bteaser\b", r"\brelease date\b", r"\blaunch\b",
    r"\bpatch\b", r"\bmod\b", r"\bremake\b", r"\bremaster\b", r"\bearly access\b",
    r"\bsteam\b", r"\bepic games?\b", r"\bplaystation\b", r"\bps[45]\b", r"\bxbox\b",
    r"\bnintendo\b", r"\bswitch\b", r"\bubisoft\b", r"\bactivision\b", r"\bea\b",
]

# Never turn a category, a vague marketing word, or a sentence fragment into a
# WordPress tag. That is how taxonomies become landfill.
GENERIC_TAG_NAMES = {
    "game", "games", "gaming", "video game", "news", "latest news", "update",
    "updates", "trailer", "teaser", "dlc", "review", "preview", "hands on",
    "hardware", "technology", "tech", "pc", "console", "playstation", "xbox",
    "nintendo", "steam", "announcement", "release", "launch", "performance",
    "benchmark", "driver", "rumor", "rumours", "leak", "leaks",
}


def _pattern_score(text: str, patterns: list[str]) -> int:
    text = (text or "").lower()
    return sum(1 for pattern in patterns if re.search(pattern, text, flags=re.I))


def infer_content_type(title_en: str, snippet_en: str = "", page_text: str = "") -> str:
    """
    Deterministic fallback for bad/partial model output. The model makes the primary
    editorial decision; this only keeps one stray keyword from derailing the category.
    """
    title = (title_en or "").lower()
    context = f"{title} {(snippet_en or '').lower()} {(page_text or '').lower()[:5000]}"

    if _pattern_score(title, REVIEW_PATTERNS) or _pattern_score(context, REVIEW_PATTERNS) >= 2:
        return "review"

    # Headlines deserve more weight than boilerplate from a publisher's page.
    hardware_score = (_pattern_score(title, HARDWARE_PATTERNS) * 3) + _pattern_score(context, HARDWARE_PATTERNS)
    gaming_score = (_pattern_score(title, GAMING_PATTERNS) * 3) + _pattern_score(context, GAMING_PATTERNS)

    if hardware_score and hardware_score > gaming_score:
        return "hardware"
    if gaming_score:
        return "gaming"
    if hardware_score:
        return "hardware"
    return "general"


def normalize_content_type(value: object, fallback: str = "general") -> str:
    candidate = clean_text(str(value or "")).lower()
    aliases = {
        "games": "gaming",
        "game": "gaming",
        "videogame": "gaming",
        "tech": "hardware",
        "technology": "hardware",
        "component": "hardware",
        "reviews": "review",
    }
    candidate = aliases.get(candidate, candidate)
    if candidate in CONTENT_TYPES:
        return candidate
    return fallback if fallback in CONTENT_TYPES else "general"


def tag_key(value: str) -> str:
    value = html_lib.unescape(value or "")
    value = clean_text(value).casefold()
    value = re.sub(r"[\u2018\u2019\u201a\u201b`´]", "'", value)
    value = re.sub(r"[^0-9a-z\u0600-\u06ff+&' -]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def sanitize_entity_tags(values: object, max_tags: int = AUTO_TAGS_MAX) -> list[str]:
    if max_tags <= 0 or not isinstance(values, list):
        return []

    output: list[str] = []
    seen: set[str] = set()

    for raw in values:
        tag = clean_text(str(raw or ""))
        tag = re.sub(r"\s+", " ", tag).strip(" -–—:;,.")
        key = tag_key(tag)

        if (
            len(tag) < 2
            or len(tag) > 80
            or len(key) < 2
            or key in GENERIC_TAG_NAMES
            or key in seen
            or re.fullmatch(r"\d+", key or "")
        ):
            continue

        seen.add(key)
        output.append(tag)
        if len(output) >= max_tags:
            break

    return output


def pick_categories(
    title_en: str,
    snippet_en: str,
    content_type: str | None = None,
    page_text: str = "",
) -> list[int]:
    content_type = normalize_content_type(
        content_type,
        infer_content_type(title_en, snippet_en, page_text),
    )

    if content_type == "review" and CAT_REVIEWS:
        return [CAT_REVIEWS]

    if content_type == "hardware" and CAT_HARDWARE:
        return [CAT_ALL, CAT_HARDWARE] if CAT_ALL else [CAT_HARDWARE]

    if content_type == "gaming" and CAT_GAMING:
        return [CAT_ALL, CAT_GAMING] if CAT_ALL else [CAT_GAMING]

    if CAT_ALL:
        return [CAT_ALL]
    if WP_CATEGORY_ID_DEFAULT > 0:
        return [WP_CATEGORY_ID_DEFAULT]
    return []


def pick_categories_manual(
    title_en: str,
    snippet_en: str,
    content_type: str | None = None,
    page_text: str = "",
) -> list[int]:
    # Manual links now use the same classifier. The old title-only check was fast,
    # but so is a car without brakes.
    return pick_categories(title_en, snippet_en, content_type=content_type, page_text=page_text)


def extract_source_title_from_html(html: str) -> str:
    raw = html or ""
    patterns = [
        r'<meta[^>]+(?:property|name)=["\'](?:og:title|twitter:title)["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:title|twitter:title)["\']',
        r'<title[^>]*>(.*?)</title>',
        r'<h1[^>]*>(.*?)</h1>',
    ]
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.I | re.S)
        if match:
            title = clean_text(html_lib.unescape(match.group(1)))
            if title:
                return title
    return ""


def extract_source_site_name_from_html(html: str) -> str:
    """Prefer the publisher's own brand label instead of displaying its .com domain."""
    raw = html or ""
    patterns = [
        r'<meta[^>]+(?:property|name)=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:site_name["\']',
        r'<meta[^>]+name=["\']application-name["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']application-name["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.I | re.S)
        if match:
            name = clean_text(html_lib.unescape(match.group(1)))
            if name:
                return name
    return ""


def fetch_source_page_data(source_url: str) -> dict:
    """
    Retrieve a source page once and reuse its title, text, image and date. The old
    flow fetched the same URL up to four times per post, a small monument to waste.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    try:
        response = http_request("GET", source_url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        print("SOURCE PAGE fetch failed:", repr(exc))
        return {"title": "", "site_name": "", "text": "", "image_url": None, "published_at": ""}

    print("SOURCE PAGE:", response.status_code, "| bytes:", len(response.content))
    if response.status_code >= 400:
        return {"title": "", "site_name": "", "text": "", "image_url": None, "published_at": ""}

    source_html = response.text or ""
    return {
        "title": extract_source_title_from_html(source_html),
        "site_name": extract_source_site_name_from_html(source_html),
        "text": extract_text_from_html(source_html, max_chars=SOURCE_TEXT_MAX_CHARS),
        "image_url": extract_image_url_from_html(source_html, source_url),
        "published_at": extract_published_at_from_html(source_html),
    }

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


def validate_article_payload(
    data: dict,
    fallback_content_type: str = "general",
) -> dict:
    """Validate and lightly normalize OpenAI output without extra runtime dependencies."""
    if not isinstance(data, dict):
        raise ValueError("OpenAI article payload must be a JSON object")

    out = dict(data)
    out["title_fa"] = _required_str(out, "title_fa", min_len=3)
    out["meta_title_fa"] = _required_str(out, "meta_title_fa", min_len=3, max_len=70)
    out["meta_description_fa"] = _required_str(out, "meta_description_fa", min_len=3, max_len=160)
    out["focus_keyword_fa"] = _required_str(out, "focus_keyword_fa", min_len=2)
    out["content_html_fa"] = _required_str(out, "content_html_fa", min_len=20)
    out["content_type"] = normalize_content_type(out.get("content_type"), fallback_content_type)
    out["entity_tags"] = sanitize_entity_tags(out.get("entity_tags"), AUTO_TAGS_MAX)
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
    fallback_type = infer_content_type(title_en, snippet_en, page_text)

    prompt = f"""
You are a careful Persian (Farsi) gaming and technology news editor. Using the English
source material below, write a publish-ready Persian news article and classify it for
a WordPress site.

Inputs (English):
- Title: {title_en}
- Snippet: {snippet_en}
- Source name: {source_name}
- Source page excerpt: {page_text}

Factual rules:
- Do NOT invent facts, numbers, quotes, release timings, names, features or claims.
- Use only the supplied inputs. When evidence is thin, keep the article shorter rather
  than padding it with generic background or speculation.
- Rewrite in original, natural Persian. Do not copy source sentences verbatim.
- Mention the source only in the opening paragraph. Do not include source URLs in the body.

Article rules:
- Fluent, natural newsroom Persian, not inflated marketing language.
- Aim for roughly 450–750 Persian words when the supplied material supports it.
- Use only <p>, <ul> and <li> in content_html_fa. Do not add headings, labels or
  conclusion headings. Do not include Gutenberg comments or Markdown.
- Begin with a short lead paragraph, follow with factual detail/context, and end with
  a cautious closing paragraph.

Taxonomy rules:
- content_type must be exactly one of: gaming, hardware, review, general.
- entity_tags must be an array with 0–3 specific official English names of games,
  hardware products, platforms, companies or franchises explicitly named in the inputs.
- Never use generic labels in entity_tags, including Game, Gaming, Hardware, Tech,
  News, Review, Trailer, Update, PC, Steam, PlayStation, Xbox or Nintendo.
- Do not create a tag from a guess, a sentence fragment, or a translated Persian title.

Return JSON only with exactly these keys:
- title_fa
- meta_title_fa (max 70 chars)
- meta_description_fa (max 160 chars)
- focus_keyword_fa
- content_html_fa
- content_type
- entity_tags
""".strip()

    def request_article():
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=OPENAI_TEMPERATURE,
            max_tokens=OPENAI_MAX_TOKENS,
            messages=[
                {"role": "system", "content": "Return one valid JSON object only."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        return _parse_json_strict((response.choices[0].message.content or "").strip())

    try:
        data = request_article()
    except Exception as exc:
        print("WARN: OpenAI JSON request failed; retrying once. Error:", repr(exc))
        data = request_article()

    # Clean metadata after parsing. Article markup is intentionally kept intact here
    # and converted to native Gutenberg blocks later, immediately before posting.
    data["title_fa"] = clean_text(data.get("title_fa", ""))
    data["meta_title_fa"] = clean_text(data.get("meta_title_fa", ""))[:70]
    data["meta_description_fa"] = clean_text(data.get("meta_description_fa", ""))[:160]
    data["focus_keyword_fa"] = clean_text(data.get("focus_keyword_fa", ""))

    htmlout = (data.get("content_html_fa") or "").strip()
    if source_url:
        htmlout = htmlout.replace(source_url, "").strip()
    htmlout = re.sub(r"(?im)\b(برای اطلاعات بیشتر.*|جزئیات بیشتر.*|در منبع.*)\b", "", htmlout).strip()
    data["content_html_fa"] = htmlout

    return validate_article_payload(data, fallback_content_type=fallback_type)


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


_TAG_ID_CACHE: dict[str, int] = {}


def wp_find_or_create_tag_id(tag_name: str) -> int | None:
    """
    Resolve a native wp/v2/tags term by exact normalized name, then create it only when
    the authenticated WordPress user is allowed to do so. Tag failures are non-fatal:
    a news post should not vanish because taxonomy decided to be dramatic.
    """
    if not AUTO_TAGS_ENABLED:
        return None

    tag_name = clean_text(tag_name)
    key = tag_key(tag_name)
    if not tag_name or not key:
        return None

    cached = _TAG_ID_CACHE.get(key)
    if cached:
        return cached

    endpoint = f"{WP_BASE_URL}/wp-json/wp/v2/tags"
    try:
        response = http_request(
            "GET",
            endpoint,
            headers=wp_request_headers(),
            params={"search": tag_name, "per_page": "100", "hide_empty": "false"},
            timeout=HTTP_TIMEOUT,
        )
        if response.status_code < 400:
            for term in response.json() or []:
                existing_name = clean_text(term.get("name", ""))
                if tag_key(existing_name) == key:
                    term_id = int(term["id"])
                    _TAG_ID_CACHE[key] = term_id
                    return term_id
        else:
            print("WP TAG SEARCH:", response.status_code, "| tag:", tag_name)
    except (requests.RequestException, ValueError, KeyError) as exc:
        print("WP TAG SEARCH failed:", tag_name, repr(exc))
        return None

    if not AUTO_TAGS_CREATE_MISSING:
        return None

    try:
        response = http_request(
            "POST",
            endpoint,
            headers=wp_request_headers(json_mode=True),
            json={"name": tag_name},
            timeout=HTTP_TIMEOUT,
        )
        if response.status_code < 400:
            term_id = int(response.json()["id"])
            _TAG_ID_CACHE[key] = term_id
            print("WP TAG CREATED:", tag_name, "=>", term_id)
            return term_id

        # WordPress returns the existing term ID when two posts race to create it.
        payload = response.json() if response.text else {}
        existing_id = ((payload.get("data") or {}).get("term_id")) if isinstance(payload, dict) else None
        if existing_id:
            term_id = int(existing_id)
            _TAG_ID_CACHE[key] = term_id
            return term_id

        print("WP TAG CREATE failed:", response.status_code, "| tag:", tag_name, "| body:", response.text[:250])
    except (requests.RequestException, ValueError, KeyError) as exc:
        print("WP TAG CREATE failed:", tag_name, repr(exc))
    return None


def resolve_wp_tag_ids(entity_tags: object) -> list[int]:
    if not AUTO_TAGS_ENABLED:
        return []

    tag_ids: list[int] = []
    for tag_name in sanitize_entity_tags(entity_tags, AUTO_TAGS_MAX):
        tag_id = wp_find_or_create_tag_id(tag_name)
        if tag_id and tag_id not in tag_ids:
            tag_ids.append(tag_id)

    print("Resolved WordPress tags:", tag_ids)
    return tag_ids


def create_wp_post(
    title: str,
    content_html: str,
    categories: list[int],
    featured_media_id: int | None = None,
    tag_ids: list[int] | None = None,
) -> int:
    endpoint = f"{WP_BASE_URL}/wp-json/wp/v2/posts"
    payload = {"title": title, "content": content_html, "status": WP_POST_STATUS}
    if categories:
        payload["categories"] = categories
    if tag_ids:
        payload["tags"] = tag_ids
    if featured_media_id is not None:
        payload["featured_media"] = featured_media_id

    response = http_request(
        "POST",
        endpoint,
        headers=wp_request_headers(json_mode=True),
        json=payload,
        timeout=HTTP_TIMEOUT,
    )
    print("WP POST:", response.status_code)
    if response.status_code >= 400:
        print("WP POST ERROR (first 500):", response.text[:500])
        response.raise_for_status()

    return int(response.json()["id"])

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
# Image extraction deliberately ranks candidates instead of blindly trusting the
# first `og:image`. Some publishers (including Wccftech on some responses) expose
# a site logo or an interstitial image there, which is disastrous as a featured image.
_IMAGE_URL_REJECT_WORDS = {
    "logo", "favicon", "gravatar", "placeholder", "site-icon",
    "blank.", "blank-", "advert", "advertisement",
    "site-branding", "site-logo", "wccftech-website",
}
_IMAGE_ATTR_REJECT_WORDS = {
    "logo", "branding", "avatar", "author", "advert", "banner-ad", "menu-icon",
    "site-header", "site-logo", "favicon", "placeholder",
}


def _clean_image_candidate(value: str, base_url: str) -> str:
    value = html_lib.unescape((value or "").strip())
    if not value or value.lower().startswith(("data:", "javascript:")):
        return ""
    return urljoin(base_url, value)


def _largest_srcset_url(value: str) -> str:
    """Pick the last/largest candidate in a normal srcset string."""
    choices = []
    for piece in (value or "").split(","):
        item = piece.strip()
        if not item:
            continue
        bits = item.split()
        if bits:
            choices.append(bits[0])
    return choices[-1] if choices else ""


def _as_int(value: str) -> int:
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else 0


class _ImageMetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.candidates: list[dict] = []

    def _add(self, url: str, kind: str, attrs: dict | None = None):
        url = (url or "").strip()
        if not url:
            return
        self.candidates.append({"url": url, "kind": kind, "attrs": attrs or {}})

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        attr = {str(k).lower(): (v or "") for k, v in attrs}

        if tag == "meta":
            prop = attr.get("property", "").lower()
            name = attr.get("name", "").lower()
            itemprop = attr.get("itemprop", "").lower()
            content = attr.get("content", "").strip()
            if content:
                if prop in {"og:image", "og:image:url", "og:image:secure_url"}:
                    self._add(content, "og", attr)
                elif name in {"twitter:image", "twitter:image:src"}:
                    self._add(content, "twitter", attr)
                elif itemprop in {"image", "thumbnailurl"}:
                    self._add(content, "schema-meta", attr)

        elif tag == "link":
            rel = attr.get("rel", "").lower()
            href = attr.get("href", "").strip()
            if href and ("image_src" in rel or "thumbnail" in rel):
                self._add(href, "link-image", attr)

        elif tag in {"img", "source"}:
            # Lazy-loading is common. The old parser only saw `src`, which is often
            # a tiny placeholder or the publication's own logo.
            for key in ("data-src", "data-lazy-src", "data-original", "data-flickity-lazyload", "src"):
                if attr.get(key):
                    self._add(attr[key], "img", attr)
            for key in ("data-srcset", "srcset"):
                if attr.get(key):
                    selected = _largest_srcset_url(attr[key])
                    if selected:
                        self._add(selected, "img-srcset", attr)


def _image_candidate_score(candidate: dict, base_url: str) -> tuple[int, str]:
    raw = str(candidate.get("url") or "")
    url = _clean_image_candidate(raw, base_url)
    if not url:
        return -10000, "empty-or-inline"

    parts = urlsplit(url)
    path = (parts.path or "").lower()
    attrs = candidate.get("attrs") or {}
    attr_text = " ".join(
        str(attrs.get(key, ""))
        for key in ("class", "id", "alt", "title", "aria-label")
    ).lower()

    if any(word in path for word in _IMAGE_URL_REJECT_WORDS):
        return -10000, "rejected-url"
    if any(word in attr_text for word in _IMAGE_ATTR_REJECT_WORDS):
        return -10000, "rejected-element"

    width = _as_int(attrs.get("width", ""))
    height = _as_int(attrs.get("height", ""))
    # Explicitly tiny images are never a sensible article hero.
    if (width and width < 420) or (height and height < 180):
        return -10000, f"too-small-{width}x{height}"

    kind = str(candidate.get("kind") or "")
    score_by_kind = {
        "og": 115,
        "twitter": 110,
        "schema-meta": 105,
        "link-image": 100,
        "img-srcset": 85,
        "img": 75,
    }
    score = score_by_kind.get(kind, 50)

    # Boost likely article/featured elements. This makes a real lazy-loaded hero
    # beat an otherwise generic social thumbnail when both are present.
    if any(word in attr_text for word in ("featured", "hero", "post-thumbnail", "wp-post-image", "article-image", "entry-image")):
        score += 45
    if width >= 1000 or height >= 600:
        score += 12
    elif width >= 700 or height >= 400:
        score += 6

    return score, "ok"


def extract_image_url_from_html(html: str, base_url: str) -> str | None:
    """Return the best plausible article image, never a logo/placeholder."""
    raw_html = html or ""
    parser = _ImageMetaParser()
    try:
        parser.feed(raw_html)
    except Exception:
        logger.debug("HTML parser could not fully parse image metadata", exc_info=True)

    # JSON-LD commonly contains the canonical article image even where OpenGraph
    # is populated with a generic site asset.
    for match in re.finditer(
        r'(?is)"(?:image|thumbnailUrl)"\s*:\s*(?:"([^"\\]+)"|\[\s*"([^"\\]+)")',
        raw_html,
    ):
        parser._add(match.group(1) or match.group(2) or "", "jsonld", {})

    best_url = ""
    best_score = -10000
    debug_rows = []
    seen = set()
    for candidate in parser.candidates:
        url = _clean_image_candidate(str(candidate.get("url") or ""), base_url)
        if not url or url in seen:
            continue
        seen.add(url)
        score, reason = _image_candidate_score(candidate, base_url)
        debug_rows.append((score, reason, str(candidate.get("kind") or ""), url))
        if score > best_score:
            best_score = score
            best_url = url

    debug_rows.sort(key=lambda row: row[0], reverse=True)
    for score, reason, kind, url in debug_rows[:5]:
        print(f"IMAGE CANDIDATE score={score} kind={kind} reason={reason}: {url}")

    # Do not fall back to the first <img>. Uploading a publisher logo is worse than
    # skipping the source image and allowing the configured Pexels fallback.
    if best_score < 0:
        print("No valid source article image found after logo/placeholder filtering.")
        return None
    return best_url or None


def is_valid_source_image_url(image_url: str | None) -> bool:
    """Extra safety for callers that receive a URL from a cached/older code path."""
    if not image_url:
        return False
    path = (urlsplit(image_url).path or "").lower()
    return not any(word in path for word in _IMAGE_URL_REJECT_WORDS)

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
    content_type = (r.headers.get("Content-Type") or "").lower()
    if content_type and not content_type.startswith("image/"):
        print("IMG rejected: response is not an image:", content_type, img_url)
        return None, None, None
    ext, mime = guess_ext_and_mime(content_type)
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
# Content builder: native Gutenberg blocks, not one giant Classic block
# =======================
def gutenberg_paragraph_block(inner_html: str) -> str:
    inner_html = (inner_html or "").strip()
    if not inner_html:
        return ""
    return f"<!-- wp:paragraph -->\n<p>{inner_html}</p>\n<!-- /wp:paragraph -->"


def gutenberg_list_block(list_inner_html: str) -> str:
    items = re.findall(r"(?is)<li\b[^>]*>(.*?)</li>", list_inner_html or "")
    if not items:
        fallback = clean_text(list_inner_html)
        return gutenberg_paragraph_block(fallback)

    rendered_items = []
    for item in items:
        item = item.strip()
        if item:
            rendered_items.append(
                "<!-- wp:list-item -->\n"
                f"<li>{item}</li>\n"
                "<!-- /wp:list-item -->"
            )
    if not rendered_items:
        return ""

    return (
        "<!-- wp:list -->\n"
        '<ul class="wp-block-list">\n'
        + "\n".join(rendered_items)
        + "\n</ul>\n<!-- /wp:list -->"
    )


def html_to_gutenberg_blocks(fragment_html: str) -> str:
    """
    Convert the restricted article HTML produced by the model into individual native
    Gutenberg blocks. WordPress then opens every paragraph/list as an editable block
    instead of offering the classic-block conversion ritual.
    """
    html_fragment = (fragment_html or "").strip()
    if not html_fragment:
        return ""

    pattern = re.compile(r"(?is)<p\b[^>]*>(.*?)</p>|<ul\b[^>]*>(.*?)</ul>")
    blocks: list[str] = []
    last_end = 0

    for match in pattern.finditer(html_fragment):
        between = clean_text(html_fragment[last_end:match.start()])
        if between:
            blocks.append(gutenberg_paragraph_block(html_lib.escape(between)))

        paragraph_inner, list_inner = match.groups()
        if paragraph_inner is not None:
            block = gutenberg_paragraph_block(paragraph_inner)
        else:
            block = gutenberg_list_block(list_inner)
        if block:
            blocks.append(block)
        last_end = match.end()

    trailing = clean_text(html_fragment[last_end:])
    if trailing:
        blocks.append(gutenberg_paragraph_block(html_lib.escape(trailing)))

    # A model that somehow returns plain text still gets an editable Paragraph block.
    if not blocks:
        plain = clean_text(html_fragment)
        if plain:
            blocks.append(gutenberg_paragraph_block(html_lib.escape(plain)))

    return "\n\n".join(blocks).strip()


def gutenberg_image_block(image_url: str, alt_text: str = "", media_id: int | None = None) -> str:
    image_url = (image_url or "").strip()
    if not image_url:
        return ""

    url_attr = html_lib.escape(image_url, quote=True)
    alt_attr = html_lib.escape(clean_text(alt_text), quote=True)
    if media_id:
        attrs = json.dumps({"id": int(media_id), "sizeSlug": "large", "linkDestination": "none"}, ensure_ascii=False)
        image_class = f"wp-image-{int(media_id)}"
    else:
        attrs = json.dumps({"sizeSlug": "large", "linkDestination": "none"}, ensure_ascii=False)
        image_class = ""

    class_attr = f' class="{image_class}"' if image_class else ""
    return (
        f"<!-- wp:image {attrs} -->\n"
        '<figure class="wp-block-image size-large">'
        f'<img src="{url_attr}" alt="{alt_attr}"{class_attr}/>'
        "</figure>\n<!-- /wp:image -->"
    )


def image_src_from_html(image_html: str) -> str:
    match = re.search(r"(?is)<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", image_html or "")
    return html_lib.unescape(match.group(1).strip()) if match else ""


def build_wp_content(
    final_body_html: str,
    source_name: str,
    source_url: str,
    published_at: str,
    image_html: str = "",
    image_credit_html: str = "",
    featured_media_id: int | None = None,
    image_alt: str = "",
) -> str:
    nice_date = format_rss_date(published_at) if (published_at or "").strip() else ""

    blocks: list[str] = []
    image_url = image_src_from_html(image_html)
    if image_url and EMBED_IMAGE_IN_CONTENT:
        image_block = gutenberg_image_block(image_url, alt_text=image_alt, media_id=featured_media_id)
        if image_block:
            blocks.append(image_block)

    if image_credit_html:
        credit_blocks = html_to_gutenberg_blocks(image_credit_html)
        if credit_blocks:
            blocks.append(credit_blocks)

    body_blocks = html_to_gutenberg_blocks(final_body_html)
    if body_blocks:
        blocks.append(body_blocks)

    source_name_safe = html_lib.escape(clean_text(source_name))
    source_url_safe = html_lib.escape((source_url or "").strip(), quote=True)
    date_safe = html_lib.escape(clean_text(nice_date))
    footer_inner = (
        f"<strong>منبع:</strong> "
        f'<a href="{source_url_safe}" target="_blank" rel="nofollow noopener noreferrer">{source_name_safe}</a>'
        f"<br/><strong>زمان انتشار منبع:</strong> {date_safe}"
    )
    blocks.append(
        "<!-- wp:separator -->\n"
        '<hr class="wp-block-separator has-alpha-channel-opacity"/>\n'
        "<!-- /wp:separator -->"
    )
    blocks.append(gutenberg_paragraph_block(footer_inner))

    return "\n\n".join(block for block in blocks if block).strip()


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
    for source in sources:
        print("-", source["name"], source["feed"])

    rotation = get_rotation_list(sources)
    print("Rotation order:", rotation)

    total_added = 0
    for source in sources:
        entries = fetch_feed_entries(source["feed"])
        newest = entries[: max(1, FEED_ENTRIES_LIMIT)]
        added = upsert_new_items(source["name"], newest)
        print(f"ADDED from {source['name']}: {added}")
        total_added += added

    processed_posts = 0
    for _ in range(max(1, MAX_POSTS_PER_RUN)):
        source_name_for_turn, source_index = choose_next_source_with_pending(rotation)
        if source_name_for_turn is None:
            print("No pending items found for any source.")
            break

        next_index = (int(source_index) + 1) % len(rotation)
        state_set("rr_index", str(next_index))
        print(f"\n=== TURN: source='{source_name_for_turn}' (next rr_index={next_index}) ===")

        while True:
            row = get_next_pending_for_source(source_name_for_turn)
            if not row:
                print(f"No more pending items for source '{source_name_for_turn}'.")
                break

            (item_id, source_name, title_en, snippet_en, url, published_at) = row
            print("\n--- ITEM ---")
            print("Source:", source_name)
            print("URL:", url)
            print("Title EN:", title_en)

            try:
                dup, why = is_duplicate_by_db(title_en)
                if dup:
                    mark_skipped(item_id, reason=f"duplicate: {why}")
                    continue

                dup2, why2 = wp_search_similar_posts(title_en)
                if dup2:
                    mark_skipped(item_id, reason=f"duplicate: {why2}")
                    continue

                # One page request replaces separate text/image/date requests.
                source_page = fetch_source_page_data(url) if (USE_SOURCE_PAGE_TEXT or SET_FEATURED_IMAGE) else {}
                page_text = source_page.get("text", "") if USE_SOURCE_PAGE_TEXT else ""
                print("page_text chars:", len(page_text))

                gen = openai_generate_fa_article(
                    title_en,
                    snippet_en,
                    source_name,
                    url,
                    page_text=page_text,
                )
                categories = pick_categories(
                    title_en,
                    snippet_en,
                    content_type=gen.get("content_type"),
                    page_text=page_text,
                )
                tag_ids = resolve_wp_tag_ids(gen.get("entity_tags", []))
                print(
                    "Picked category type:", gen.get("content_type"),
                    "| categories:", categories,
                    "| entity tags:", gen.get("entity_tags", []),
                )

                featured_media_id = None
                image_html = ""
                image_credit_html = ""
                used_image_kind = "none"

                if SET_FEATURED_IMAGE:
                    img_url = source_page.get("image_url") or fetch_source_image_url(url)
                    if img_url and not is_valid_source_image_url(img_url):
                        print("Rejected non-article source image URL:", img_url)
                        img_url = None
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
                            filename = f"news-{item_id[:12]}.{ext}"
                            media = wp_upload_media(img_bytes, filename, mime_type=mime, alt_text=gen["title_fa"])
                            featured_media_id = int(media["id"])
                            wp_src = (media.get("source_url") or "").strip()
                            if wp_src:
                                image_html = f'<p><img src="{wp_src}" alt="{html_lib.escape(gen["title_fa"], quote=True)}"/></p>'
                            print("Featured media id:", featured_media_id, "| kind:", used_image_kind)
                        else:
                            print("No image bytes downloaded.")
                    else:
                        print("No image found (source + pexels).")

                content_html = build_wp_content(
                    final_body_html=gen["content_html_fa"],
                    source_name=source_name,
                    source_url=url,
                    published_at=source_page.get("published_at") or published_at,
                    image_html=image_html,
                    image_credit_html=image_credit_html,
                    featured_media_id=featured_media_id,
                    image_alt=gen["title_fa"],
                )

                post_id = create_wp_post(
                    title=gen["title_fa"],
                    content_html=content_html,
                    categories=categories,
                    featured_media_id=featured_media_id,
                    tag_ids=tag_ids,
                )

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
                break

            except requests.RequestException as exc:
                logger.warning("Network failure for %s: %r", url, exc)
                mark_failed(item_id)
                break
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                logger.error("Data failure for %s: %r", url, exc)
                mark_failed(item_id)
                break
            except Exception:
                logger.exception("Unhandled failure for %s", url)
                mark_failed(item_id)
                break

        if processed_posts >= MAX_POSTS_PER_RUN:
            break

    print(f"\nDone. Added from feeds: {total_added}, posted now: {processed_posts}")


if __name__ == "__main__":
    run()


