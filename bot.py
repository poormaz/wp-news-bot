import os
import re
import json
import time
import base64
import hashlib
import sqlite3
import traceback
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, quote_plus

import yaml
import feedparser
import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# =======================
# ENV
# =======================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini").strip()

WP_BASE_URL = os.getenv("WP_BASE_URL", "").strip().rstrip("/")
WP_USERNAME = os.getenv("WP_USERNAME", "").strip()
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "").strip()
WP_POST_STATUS = os.getenv("WP_POST_STATUS", "draft").strip()

LANG = os.getenv("LANG", "fa").strip()
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "25"))
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (WPNewsBot/1.0; +https://example.com)").strip()

DB_FILE = os.getenv("DB_FILE", "news_cache.db").strip()
SOURCES_FILE = os.getenv("SOURCES_FILE", "sources.yaml").strip()

# One post per run
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "1"))

# How many top RSS items per site to import each run (3 or 4 suggested)
FEED_ENTRIES_LIMIT = int(os.getenv("FEED_ENTRIES_LIMIT", "4"))

# Category mapping
CAT_ALL = int(os.getenv("CAT_ALL", "0"))
CAT_GAMING = int(os.getenv("CAT_GAMING", "0"))
CAT_HARDWARE = int(os.getenv("CAT_HARDWARE", "0"))
CAT_REVIEWS = int(os.getenv("CAT_REVIEWS", "0"))
WP_CATEGORY_ID_DEFAULT = int(os.getenv("WP_CATEGORY_ID", "0"))

# RankMath updater (optional)
RANKMATH_UPDATER_URL = os.getenv("RANKMATH_UPDATER_URL", "").strip()
RANKMATH_UPDATER_TOKEN = os.getenv("RANKMATH_UPDATER_TOKEN", "").strip()

# Image behavior
SET_FEATURED_IMAGE = os.getenv("SET_FEATURED_IMAGE", "1").strip() == "1"
EMBED_IMAGE_IN_CONTENT = os.getenv("EMBED_IMAGE_IN_CONTENT", "1").strip() == "1"

# Rotation / round-robin order:
# Example: "GameSpot,IGN,DSOGaming,Wccftech"
ROTATION_SOURCES = os.getenv("ROTATION_SOURCES", "").strip()

# Pexels fallback
PEXELS_ENABLED = os.getenv("PEXELS_ENABLED", "1").strip() == "1"
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "").strip()
PEXELS_ORIENTATION = os.getenv("PEXELS_ORIENTATION", "landscape").strip()
PEXELS_PER_PAGE = int(os.getenv("PEXELS_PER_PAGE", "1"))


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
    """
    Convert RSS published string to unix timestamp (seconds).
    Falls back to now if parsing fails.
    """
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


def basic_keywords_from_title(title_en: str, max_words: int = 7) -> str:
    t = (title_en or "").lower()
    t = re.sub(r"[^a-z0-9\s\-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    words = [w for w in t.split(" ") if w]

    stop = {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "from", "by", "at",
        "is", "are", "was", "were", "be", "been", "being",
        "as", "this", "that", "these", "those",
        "new", "latest", "update", "updates",
    }
    keep = [w for w in words if w not in stop and len(w) >= 3]
    keep = (keep or words)[:max_words]
    return " ".join(keep).strip() or "technology"


# =======================
# DB
# =======================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
      CREATE TABLE IF NOT EXISTS items (
        id TEXT PRIMARY KEY,
        source_name TEXT,
        title_en TEXT,
        snippet_en TEXT,
        url TEXT,
        published_at TEXT,
        published_ts INTEGER,
        created_at TEXT,
        status TEXT,
        wp_post_id INTEGER
      )
    """)

    c.execute("""
      CREATE TABLE IF NOT EXISTS state (
        key TEXT PRIMARY KEY,
        value TEXT
      )
    """)

    # Migration: add published_ts if missing (older DB)
    c.execute("PRAGMA table_info(items)")
    cols = {row[1] for row in c.fetchall()}
    if "published_ts" not in cols:
        c.execute("ALTER TABLE items ADD COLUMN published_ts INTEGER")
        conn.commit()

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

        published_ts = parse_published_ts(published)

        try:
            c.execute("""
              INSERT INTO items (
                id, source_name, title_en, snippet_en, url,
                published_at, published_ts, created_at, status, wp_post_id
              )
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (hid, source_name, title, snippet, link, published, published_ts, now_iso, "pending", None))
            added += 1
        except sqlite3.IntegrityError:
            # already exists (same URL hash)
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


def get_next_pending_round_robin(source_order: list[str]):
    """
    Pick exactly one pending item, rotating between sources.
    If the current source has no pending items, it will scan forward to find one.
    If none exist, returns [].
    """
    if not source_order:
        return []

    # rr_index points to the "next source to try"
    try:
        rr_index = int(state_get("rr_index", "0") or "0")
    except ValueError:
        rr_index = 0

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    chosen_row = None
    chosen_idx = None

    for i in range(len(source_order)):
        idx = (rr_index + i) % len(source_order)
        src = source_order[idx]

        c.execute("""
          SELECT id, source_name, title_en, snippet_en, url, published_at
          FROM items
          WHERE status='pending' AND source_name=?
          ORDER BY COALESCE(published_ts, 0) DESC, created_at DESC
          LIMIT 1
        """, (src,))
        row = c.fetchone()
        if row:
            chosen_row = row
            chosen_idx = idx
            break

    conn.close()

    if chosen_row is None:
        return []

    # next run starts after the chosen source
    next_idx = (chosen_idx + 1) % len(source_order)
    state_set("rr_index", str(next_idx))
    return [chosen_row]


# =======================
# Sources
# =======================
def load_sources():
    if not os.path.exists(SOURCES_FILE):
        die(f"Missing {SOURCES_FILE}")

    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    sources = cfg.get("sources", [])
    if not isinstance(sources, list) or not sources:
        die("sources.yaml is empty. Expected:\nsources:\n  - name: ...\n    feed: ...")

    for s in sources:
        if "name" not in s or "feed" not in s:
            die("Each source must have 'name' and 'feed'")
    return sources


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
# Categories
# =======================
def pick_categories(title_en: str, snippet_en: str) -> list[int]:
    text = (title_en + " " + snippet_en).lower()

    if CAT_REVIEWS and any(k in text for k in ["review", "hands-on", "preview", "impressions", "benchmark"]):
        return [CAT_REVIEWS]

    if CAT_HARDWARE and any(k in text for k in [
        "gpu", "rtx", "radeon", "cpu", "intel", "amd", "nvidia",
        "laptop", "ssd", "ram", "motherboard", "dlss", "fsr"
    ]):
        return [CAT_ALL, CAT_HARDWARE] if CAT_ALL else [CAT_HARDWARE]

    if CAT_GAMING and any(k in text for k in ["game", "gaming", "steam", "ps5", "xbox", "nintendo", "dlc", "trailer"]):
        return [CAT_ALL, CAT_GAMING] if CAT_ALL else [CAT_GAMING]

    if CAT_ALL:
        return [CAT_ALL]
    if WP_CATEGORY_ID_DEFAULT > 0:
        return [WP_CATEGORY_ID_DEFAULT]
    return []


# =======================
# OpenAI content generation
# =======================
def openai_generate_fa_article(title_en: str, snippet_en: str, source_name: str, source_url: str) -> dict:
    if not OPENAI_API_KEY:
        die("OPENAI_API_KEY is missing")

    client = OpenAI(api_key=OPENAI_API_KEY)

    prompt = f"""
You are a Persian (Farsi) tech & gaming news editor writing for a WordPress site.

Input:
- English title: {title_en}
- English snippet: {snippet_en}
- Source name: {source_name}
- Source URL: {source_url}

Rules:
- Write ORIGINAL Persian content (no copying).
- Do NOT invent facts/specs/numbers. If unknown, say: "جزئیات کامل در منبع".
- Target length: 600–700 Persian words.
- Use HTML with 3 to 5 <h2> headings.
- Avoid bullet-heavy writing (max 3 bullet points total).
- Do NOT write the phrase "چرا مهم است".
- End with one short takeaway sentence (without that phrase).
- Add FAQ at the end:
  <h2>سوالات متداول</h2>
  <p>سوال: ...</p>
  <p>پاسخ: ...</p> (3 pairs)

Return valid JSON ONLY with keys:
title_fa, meta_title_fa, meta_description_fa, focus_keyword_fa, content_html_fa
""".strip()

    # Important: don't pass temperature (avoid unsupported temperature error)
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "فقط JSON برگردان. بدون مارک‌داون و بدون متن اضافه."},
            {"role": "user", "content": prompt},
        ],
    )

    text = (resp.choices[0].message.content or "").strip()
    if not text:
        raise ValueError("OpenAI returned empty content")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Recover from extra text
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            data = json.loads(text[start:end + 1])
        else:
            raise

    required = ["title_fa", "meta_title_fa", "meta_description_fa", "focus_keyword_fa", "content_html_fa"]
    for k in required:
        if k not in data:
            raise ValueError(f"Missing key in OpenAI JSON: {k}")

    data["title_fa"] = clean_text(data["title_fa"])
    data["meta_title_fa"] = clean_text(data["meta_title_fa"])[:70]
    data["meta_description_fa"] = clean_text(data["meta_description_fa"])[:160]
    data["focus_keyword_fa"] = clean_text(data["focus_keyword_fa"])
    data["content_html_fa"] = (data["content_html_fa"] or "").strip()
    return data


# =======================
# Images: source first, then Pexels
# =======================
def extract_image_url_from_html(html: str, base_url: str) -> str | None:
    html = html or ""

    # 1) OpenGraph image
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if m:
        return m.group(1).strip()

    # 2) Twitter image
    m = re.search(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if m:
        return m.group(1).strip()

    # 3) First img src
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.I)
    if m:
        src = m.group(1).strip()
        return urljoin(base_url, src)

    return None


def fetch_source_image_url(source_url: str) -> str | None:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*"}
    r = requests.get(source_url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
    print("SOURCE HTML:", r.status_code, "bytes=", len(r.content))
    if r.status_code >= 400:
        print("SOURCE HTML fetch failed:", r.status_code)
        return None
    return extract_image_url_from_html(r.text, source_url)


def download_image_bytes(img_url: str) -> tuple[bytes, str, str] | tuple[None, None, None]:
    headers = {"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/*,*/*"}
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
    print("PEXELS SEARCH:", r.status_code, "| query=", q)
    if r.status_code >= 400:
        print("PEXELS ERROR (first 300):", r.text[:300])
        return None

    data = r.json() or {}
    photos = data.get("photos") or []
    if not photos:
        return None
    return photos[0]


def pexels_pick_image_url(photo: dict) -> str | None:
    src = (photo or {}).get("src") or {}
    return (src.get("large2x") or src.get("large") or src.get("original") or src.get("medium"))


def pexels_attribution_html(photo: dict) -> str:
    if not photo:
        return ""
    photographer = clean_text(photo.get("photographer", ""))
    photo_page = (photo.get("url") or "").strip()

    # short attribution line
    if photo_page and photographer:
        return f'<p><small>اعتبار عکس: <a href="{photo_page}" target="_blank" rel="nofollow noopener">Pexels / {photographer}</a></small></p>'
    if photo_page:
        return f'<p><small>اعتبار عکس: <a href="{photo_page}" target="_blank" rel="nofollow noopener">Pexels</a></small></p>'
    return "<p><small>اعتبار عکس: Pexels</small></p>"


# =======================
# WordPress
# =======================
def wp_check_me():
    endpoint = f"{WP_BASE_URL}/wp-json/wp/v2/users/me"
    headers = {"Authorization": wp_auth_header(WP_USERNAME, WP_APP_PASSWORD), "User-Agent": USER_AGENT}
    r = requests.get(endpoint, headers=headers, timeout=HTTP_TIMEOUT)
    print("WP ME:", r.status_code)
    if r.status_code >= 400:
        print("WP ME error body (first 300):", r.text[:300])
    r.raise_for_status()
    return r.json()


def wp_upload_media(image_bytes: bytes, filename: str, mime_type: str, alt_text: str = "") -> dict:
    endpoint = f"{WP_BASE_URL}/wp-json/wp/v2/media"
    headers = {
        "Authorization": wp_auth_header(WP_USERNAME, WP_APP_PASSWORD),
        "User-Agent": USER_AGENT,
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": mime_type,
    }

    r = requests.post(endpoint, headers=headers, data=image_bytes, timeout=HTTP_TIMEOUT)
    print("WP MEDIA UPLOAD:", r.status_code)
    if r.status_code >= 400:
        print("WP MEDIA ERROR (first 400):", r.text[:400])
    r.raise_for_status()

    media = r.json()

    if alt_text:
        patch = requests.post(
            f"{WP_BASE_URL}/wp-json/wp/v2/media/{media['id']}",
            headers={
                "Authorization": wp_auth_header(WP_USERNAME, WP_APP_PASSWORD),
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
            },
            json={"alt_text": alt_text},
            timeout=HTTP_TIMEOUT,
        )
        print("WP MEDIA ALT PATCH:", patch.status_code)

    return media


def create_wp_post(title: str, content_html: str, categories: list[int], featured_media_id: int | None) -> int:
    endpoint = f"{WP_BASE_URL}/wp-json/wp/v2/posts"
    headers = {
        "Authorization": wp_auth_header(WP_USERNAME, WP_APP_PASSWORD),
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }

    payload = {"title": title, "content": content_html, "status": WP_POST_STATUS}
    if categories:
        payload["categories"] = categories
    if featured_media_id:
        payload["featured_media"] = featured_media_id

    r = requests.post(endpoint, headers=headers, json=payload, timeout=HTTP_TIMEOUT)
    print("WP POST:", r.status_code)
    if r.status_code >= 400:
        print("WP POST ERROR (first 500):", r.text[:500])
    r.raise_for_status()
    return int(r.json()["id"])


def push_rankmath_meta(post_id: int, meta_title: str, meta_desc: str, focus_kw: str):
    if not (RANKMATH_UPDATER_URL and RANKMATH_UPDATER_TOKEN):
        print("RankMath updater disabled (missing env).")
        return

    payload = {
        "token": RANKMATH_UPDATER_TOKEN,
        "post_id": int(post_id),
        "meta_title": meta_title or "",
        "meta_description": meta_desc or "",
        "focus_keyword": focus_kw or "",
    }

    r = requests.post(RANKMATH_UPDATER_URL, json=payload, timeout=HTTP_TIMEOUT)
    print("RANKMATH UPDATE:", r.status_code, r.text[:200])
    if r.status_code >= 400:
        print("RANKMATH ERROR (first 300):", r.text[:300])
    r.raise_for_status()


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
        header_media = (image_html.strip() + "\n" + (image_credit_html or "").strip()).strip()

    footer = f"""
<hr>
<p><strong>منبع:</strong> <a href="{source_url}" target="_blank" rel="nofollow noopener">{source_name}</a></p>
<p><small>زمان انتشار منبع: {nice_date}</small></p>
""".strip()

    parts = []
    if header_media:
        parts.append(header_media)
    parts.append(final_body_html.strip())
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

    # Determine rotation list
    if ROTATION_SOURCES:
        rotation = [x.strip() for x in ROTATION_SOURCES.split(",") if x.strip()]
    else:
        rotation = [s["name"] for s in sources]

    print("Rotation order:", rotation)

    # Fetch & upsert newest items for every source (top N)
    total_added = 0
    for s in sources:
        entries = fetch_feed_entries(s["feed"])
        newest = entries[:max(1, FEED_ENTRIES_LIMIT)]
        added = upsert_new_items(s["name"], newest)
        print(f"ADDED from {s['name']}: {added}")
        total_added += added

    # Pick exactly one pending item by round-robin
    pending = get_next_pending_round_robin(rotation)
    print("Pending selected:", len(pending), "| MAX_POSTS_PER_RUN:", MAX_POSTS_PER_RUN)

    # Hard safety: do not process more than 1
    pending = pending[:1]

    for (item_id, source_name, title_en, snippet_en, url, published_at) in pending:
        print("\n--- ITEM ---")
        print("Source:", source_name)
        print("URL:", url)
        print("Title EN:", title_en)

        try:
            categories = pick_categories(title_en, snippet_en)
            print("Picked categories:", categories)

            gen = openai_generate_fa_article(title_en, snippet_en, source_name, url)

            featured_media_id = None
            image_html = ""
            image_credit_html = ""
            used_image_kind = "none"

            if SET_FEATURED_IMAGE:
                img_url = fetch_source_image_url(url)
                print("Source Image URL:", img_url)

                if not img_url:
                    q = basic_keywords_from_title(title_en)
                    photo = pexels_search_photo(q)
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
                        media = wp_upload_media(
                            img_bytes,
                            fn,
                            mime_type=mime,
                            alt_text=gen["title_fa"]
                        )
                        featured_media_id = int(media["id"])
                        wp_src = (media.get("source_url") or "").strip()
                        if wp_src:
                            image_html = f'<p><img src="{wp_src}" alt="{gen["title_fa"]}"></p>'
                        print("Featured media id:", featured_media_id, "| kind:", used_image_kind)
                    else:
                        print("No image bytes downloaded.")
                else:
                    print("No image found (source + pexels).")

            content_html = build_wp_content(
                final_body_html=gen["content_html_fa"],
                source_name=source_name,
                source_url=url,
                published_at=published_at,
                image_html=image_html,
                image_credit_html=image_credit_html,
            )

            post_id = create_wp_post(
                title=gen["title_fa"],
                content_html=content_html,
                categories=categories,
                featured_media_id=featured_media_id,
            )

            push_rankmath_meta(
                post_id=post_id,
                meta_title=gen["meta_title_fa"],
                meta_desc=gen["meta_description_fa"],
                focus_kw=gen["focus_keyword_fa"],
            )

            print("POSTED:", post_id)
            mark_posted(item_id, post_id)
            time.sleep(1.2)

        except Exception as e:
            print("FAILED item:", url)
            print("ERROR:", repr(e))
            traceback.print_exc()
            mark_failed(item_id)
            raise

    print(f"\nDone. Added from feeds: {total_added}, processed: {len(pending)}")


if __name__ == "__main__":
    run()
