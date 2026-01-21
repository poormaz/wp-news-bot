import os
import re
import json
import time
import base64
import hashlib
import sqlite3
import traceback
import html as html_lib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, quote_plus

import yaml
import feedparser
import requests
from dotenv import load_dotenv
from openai import OpenAI
from difflib import SequenceMatcher


load_dotenv()

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
PEXELS_ENABLED = os.getenv("PEXELS_ENABLED", "1").strip() == "1"
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "").strip()
PEXELS_ORIENTATION = os.getenv("PEXELS_ORIENTATION", "landscape").strip()
PEXELS_PER_PAGE = int(os.getenv("PEXELS_PER_PAGE", "1").strip() or "1")

# Source page text extraction (page_text)
USE_SOURCE_PAGE_TEXT = os.getenv("USE_SOURCE_PAGE_TEXT", "1").strip() == "1"
SOURCE_TEXT_MAX_CHARS = int(os.getenv("SOURCE_TEXT_MAX_CHARS", "3000").strip() or "3000")

# Dedup (fuzzy by title_en)
DEDUP_WINDOW_HOURS = int(os.getenv("DEDUP_WINDOW_HOURS", "48").strip() or "48")
DEDUP_SIM_THRESHOLD = float(os.getenv("DEDUP_SIM_THRESHOLD", "0.80").strip() or "0.80")  # 0.75..0.85 typical

DEDUP_WP_CHECK = os.getenv("DEDUP_WP_CHECK", "1").strip() == "1"
DEDUP_WP_SEARCH_PER_PAGE = int(os.getenv("DEDUP_WP_SEARCH_PER_PAGE", "10").strip() or "10")

# Safety: don't post if title is too short (reduces false positives)
DEDUP_MIN_TOKENS = int(os.getenv("DEDUP_MIN_TOKENS", "5").strip() or "5")


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


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


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
    try:
        dt = parsedate_to_datetime(published_at)
        return dt.strftime("%a, %d %b %Y")
    except Exception:
        return (published_at or "").split("+")[0].strip()


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
    r = requests.get(feed_url, headers=headers, timeout=HTTP_TIMEOUT)
    print(f"FEED GET: {feed_url} | status={r.status_code} | bytes={len(r.content)}")
    if r.status_code >= 400:
        print("FEED ERROR BODY (first 250):", r.text[:250])
        return []
    parsed = feedparser.parse(r.text)
    entries = parsed.entries or []
    print(f"FEED PARSED: entries={len(entries)}")
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
    text = (title_en + " " + snippet_en).lower()

    if CAT_REVIEWS and any(k in text for k in ["review", "hands-on", "preview", "impressions", "benchmark"]):
        return [CAT_REVIEWS]

    if CAT_HARDWARE and any(
        k in text for k in ["gpu", "rtx", "radeon", "cpu", "intel", "amd", "nvidia", "laptop", "ssd", "ram", "motherboard", "dlss", "fsr"]
    ):
        return [CAT_ALL, CAT_HARDWARE] if CAT_ALL else [CAT_HARDWARE]

    if CAT_GAMING and any(k in text for k in ["game", "gaming", "steam", "ps5", "xbox", "nintendo", "dlc", "trailer"]):
        return [CAT_ALL, CAT_GAMING] if CAT_ALL else [CAT_GAMING]

    if CAT_ALL:
        return [CAT_ALL]

    if WP_CATEGORY_ID_DEFAULT > 0:
        return [WP_CATEGORY_ID_DEFAULT]

    return []


# =======================
# Source page text (page_text)
# =======================
def fetch_source_text_excerpt(source_url: str, max_chars: int = SOURCE_TEXT_MAX_CHARS) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    r = requests.get(source_url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
    if r.status_code >= 400:
        print("SOURCE TEXT fetch failed:", r.status_code)
        return ""

    raw = r.text or ""
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


# =======================
# OpenAI (Structured Outputs)
# =======================
ARTICLE_JSON_SCHEMA = {
    "name": "wp_news_article_fa",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title_fa": {"type": "string"},
            "meta_title_fa": {"type": "string"},
            "meta_description_fa": {"type": "string"},
            "focus_keyword_fa": {"type": "string"},
            "content_html_fa": {"type": "string"},
        },
        "required": [
            "title_fa",
            "meta_title_fa",
            "meta_description_fa",
            "focus_keyword_fa",
            "content_html_fa",
        ],
    },
}


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


def _is_too_short(htmlout: str) -> bool:
    htmlout = htmlout or ""
    if len(htmlout) < 2500:
        return True
    if htmlout.count("<p") < 10:
        return True
    if htmlout.count("<h2") < 3:
        return True
    return False


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
You are a Persian (Farsi) tech & gaming news editor for a WordPress site.

Input (English):
- Title: {title_en}
- Snippet: {snippet_en}
- Source name: {source_name}
- Source URL: {source_url}
- Source page excerpt (English, may be long): {page_text}

Hard constraints:
- Write ORIGINAL Persian content according to the page_text. Do not copy phrases verbatim.
- Do NOT invent facts/specs/numbers. You may ONLY use facts that appear in the input.
- IMPORTANT: Do NOT omit factual details that appear in the input (dates, prices, platforms, names, editions, quantities).
- Do NOT mention the source link inside the body.

Output requirements (HTML):
- content_html_fa must be valid HTML using <p>, <h2>, <ul><li>.
""".strip()

    # 1) Try Structured Outputs (json_schema)
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=OPENAI_TEMPERATURE,
            messages=[
                {"role": "system", "content": "Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_schema", "json_schema": ARTICLE_JSON_SCHEMA},
        )
        text = (resp.choices[0].message.content or "").strip()
        data = _parse_json_strict(text)

    # 2) Fallback json_object
    except Exception as e:
        print("WARN: structured outputs failed; falling back to json_object mode. Error:", repr(e))
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=OPENAI_TEMPERATURE,
            messages=[
                {"role": "system", "content": "Return valid JSON only."},
                {
                    "role": "user",
                    "content": prompt
                    + "\n\nReturn JSON with keys: title_fa, meta_title_fa, meta_description_fa, focus_keyword_fa, content_html_fa",
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

    htmlout = (data.get("content_html_fa") or "").strip()

    # Remove accidental source URL if model leaked it
    if source_url:
        htmlout = htmlout.replace(source_url, "").strip()

    # Optional: remove “see source” phrases
    htmlout = re.sub(r"(?im)\b(برای اطلاعات بیشتر.*|جزئیات بیشتر.*|در منبع.*)\b", "", htmlout).strip()

    data["content_html_fa"] = htmlout
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
    r = requests.get(endpoint, headers=wp_request_headers(), timeout=HTTP_TIMEOUT)
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

    r = requests.post(endpoint, headers=headers, data=image_bytes, timeout=HTTP_TIMEOUT)
    print("WP MEDIA UPLOAD:", r.status_code)
    if r.status_code >= 400:
        print("WP MEDIA ERROR (first 400):", r.text[:400])
        r.raise_for_status()

    media = r.json()

    # Set alt text (optional)
    if alt_text:
        patch = requests.post(
            f"{WP_BASE_URL}/wp-json/wp/v2/media/{media['id']}",
            headers=wp_request_headers(json_mode=True),
            json={"alt_text": alt_text},
            timeout=HTTP_TIMEOUT,
        )
        print("WP MEDIA ALT PATCH:", patch.status_code)

    return media


def create_wp_post(title: str, content_html: str, categories: list[int], featured_media_id: int | None = None) -> int:
    endpoint = f"{WP_BASE_URL}/wp-json/wp/v2/posts"
    payload = {"title": title, "content": content_html, "status": WP_POST_STATUS}
    if categories:
        payload["categories"] = categories
    if featured_media_id is not None:
        payload["featured_media"] = featured_media_id

    r = requests.post(endpoint, headers=wp_request_headers(json_mode=True), json=payload, timeout=HTTP_TIMEOUT)
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

    r = requests.get(endpoint, headers=wp_request_headers(), params=params, timeout=HTTP_TIMEOUT)
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
    r = requests.post(endpoint, headers=wp_request_headers(json_mode=True), json=payload, timeout=HTTP_TIMEOUT)
    print("RANKMATH updateMeta:", r.status_code, "| body:", (r.text or "")[:200])
    return r.status_code >= 400

# =======================
# Images
# =======================
def extract_image_url_from_html(html: str, base_url: str) -> str | None:
    """
    Try to get a representative image from source HTML.
    Priority: og:image, twitter:image, then first <img src>.
    """
    html = html or ""

    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if m:
        return urljoin(base_url, m.group(1).strip())

    m = re.search(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if m:
        return urljoin(base_url, m.group(1).strip())

    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.I)
    if m:
        return urljoin(base_url, m.group(1).strip())

    return None


def fetch_source_image_url(source_url: str) -> str | None:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    r = requests.get(source_url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
    print("SOURCE HTML:", r.status_code, "| bytes:", len(r.content))
    if r.status_code >= 400:
        print("SOURCE HTML fetch failed:", r.status_code)
        return None
    return extract_image_url_from_html(r.text, source_url)


def download_image_bytes(img_url: str) -> tuple[bytes | None, str | None, str | None]:
    headers = {"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    r = requests.get(img_url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
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

    r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
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
def build_wp_content(
    final_body_html: str,
    source_name: str,
    source_url: str,
    published_at: str,
    image_html: str = "",
    image_credit_html: str = "",
) -> str:
    nice_date = format_rss_date(published_at)

    header_media = ""
    if image_html and EMBED_IMAGE_IN_CONTENT:
        header_media = (image_html or "").strip() + "\n" + (image_credit_html or "").strip()

    footer = (
        "<hr/>"
        f"<p><strong>منبع:</strong> {clean_text(source_name)} — "
        f"<a href=\"{source_url}\" target=\"_blank\" rel=\"nofollow noopener\">لینک</a>"
        f"<br/><strong>زمان انتشار منبع:</strong> {clean_text(nice_date)}</p>"
    )

    parts = []
    if header_media.strip():
        parts.append(header_media.strip())
    parts.append((final_body_html or "").strip())
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

                categories = pick_categories(title_en, snippet_en)
                print("Picked categories:", categories)

                # Build page_text
                page_text = ""
                if USE_SOURCE_PAGE_TEXT:
                    page_text = fetch_source_text_excerpt(url)
                    print("page_text chars:", len(page_text))

                # Generate Persian article
                gen = openai_generate_fa_article(title_en, snippet_en, source_name, url, page_text=page_text)

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
                            fn = f"news-{item_id[:12]}.{ext}"
                            media = wp_upload_media(img_bytes, fn, mime_type=mime, alt_text=gen["title_fa"])
                            featured_media_id = int(media["id"])
                            wp_src = (media.get("source_url") or "").strip()
                            if wp_src:
                                image_html = f'<p><img src="{wp_src}" alt="{gen["title_fa"]}"/></p>'
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
                )

                # Create WP post
                post_id = create_wp_post(
                    title=gen["title_fa"],
                    content_html=content_html,
                    categories=categories,
                    featured_media_id=featured_media_id,
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

            except Exception as e:
                print("FAILED item:", url)
                print("ERROR:", repr(e))
                traceback.print_exc()
                mark_failed(item_id)
                break  # برو سراغ سورس بعدی

        if processed_posts >= MAX_POSTS_PER_RUN:
            break

    print(f"\nDone. Added from feeds: {total_added}, posted now: {processed_posts}")


if __name__ == "__main__":
    run()








