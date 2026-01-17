import os
import re
import json
import time
import base64
import hashlib
import sqlite3
import traceback
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import yaml
import feedparser
import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ========= ENV =========
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

WP_BASE_URL = os.getenv("WP_BASE_URL", "").strip().rstrip("/")
WP_USERNAME = os.getenv("WP_USERNAME", "").strip()
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "").strip()

WP_POST_STATUS = os.getenv("WP_POST_STATUS", "draft").strip()
WP_CATEGORY_ID_DEFAULT = int(os.getenv("WP_CATEGORY_ID", "0"))  # fallback
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "3"))
LANG = os.getenv("LANG", "fa").strip()

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "25"))
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (WPNewsBot/1.0; +https://poormaz.com)").strip()

DB_FILE = "news_cache.db"
SOURCES_FILE = "sources.yaml"

MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini").strip()
...
resp = client.chat.completions.create(
    model=MODEL,
    messages=[...],
)

# Optional: RankMath updater endpoint (plugin below)
RANKMATH_UPDATER_URL = os.getenv("RANKMATH_UPDATER_URL", "").strip()  # e.g. https://poormaz.com/wp-json/pmz-seo/v1/update
RANKMATH_UPDATER_TOKEN = os.getenv("RANKMATH_UPDATER_TOKEN", "").strip()  # random token, not WP password

# Category mapping via env (optional)
CAT_ALL = int(os.getenv("CAT_ALL", "0"))
CAT_GAMING = int(os.getenv("CAT_GAMING", "0"))
CAT_HARDWARE = int(os.getenv("CAT_HARDWARE", "0"))
CAT_REVIEWS = int(os.getenv("CAT_REVIEWS", "0"))

# Image behavior
SET_FEATURED_IMAGE = os.getenv("SET_FEATURED_IMAGE", "1").strip() == "1"
EMBED_IMAGE_IN_CONTENT = os.getenv("EMBED_IMAGE_IN_CONTENT", "0").strip() == "1"


# ========= helpers =========
def die(msg: str):
    raise SystemExit(msg)


def safe_env_report():
    keys = [
        "OPENAI_API_KEY", "WP_BASE_URL", "WP_USERNAME", "WP_APP_PASSWORD",
        "RANKMATH_UPDATER_URL", "RANKMATH_UPDATER_TOKEN"
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


def format_rss_date(published_at: str) -> str:
    """
    Input like: Fri, 09 Jan 2026 14:03:56 +0000
    Output:     Fri, 09 Jan 2026
    """
    try:
        dt = parsedate_to_datetime(published_at)
        return dt.strftime("%a, %d %b %Y")
    except Exception:
        # fallback: cut timezone/time if parse fails
        return (published_at or "").split("+")[0].strip()


# ========= DB =========
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
        created_at TEXT,
        status TEXT,
        wp_post_id INTEGER
      )
    """)
    conn.commit()
    conn.close()


def upsert_new_items(source_name: str, entries: list) -> int:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    added = 0
    for e in entries:
        link = (e.get("link") or "").strip()
        if not link:
            continue

        hid = url_hash(link)
        title = clean_text(e.get("title", ""))
        snippet = clean_text(e.get("summary", "") or e.get("description", ""))

        published = e.get("published", "") or e.get("updated", "")
        if not published:
            published = datetime.utcnow().isoformat()

        now = datetime.utcnow().isoformat()

        try:
            c.execute("""
              INSERT INTO items (id, source_name, title_en, snippet_en, url, published_at, created_at, status, wp_post_id)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (hid, source_name, title, snippet, link, published, now, "pending", None))
            added += 1
        except sqlite3.IntegrityError:
            pass

    conn.commit()
    conn.close()
    return added


def get_pending_items(limit: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
      SELECT id, source_name, title_en, snippet_en, url, published_at
      FROM items
      WHERE status = 'pending'
      ORDER BY created_at DESC
      LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return rows


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


# ========= Sources =========
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
        print("FEED ERROR BODY (first 200):", r.text[:200])
        return []

    parsed = feedparser.parse(r.text)
    entries = parsed.entries or []
    print(f"FEED PARSED: entries={len(entries)}")
    return entries


# ========= Category selection =========
def pick_categories(source_name: str, title_en: str, snippet_en: str) -> list[int]:
    text = (title_en + " " + snippet_en).lower()

    # Reviews
    if CAT_REVIEWS and any(k in text for k in ["review", "benchmark", "hands-on", "impressions"]):
        return [CAT_REVIEWS]

    # Hardware
    if CAT_HARDWARE and any(k in text for k in ["gpu", "rtx", "radeon", "cpu", "intel", "amd", "nvidia", "laptop", "ssd", "ram", "motherboard"]):
        return [CAT_HARDWARE]

    # Gaming
    if CAT_GAMING and any(k in text for k in ["game", "gaming", "steam", "ps5", "xbox", "nintendo", "dlc", "trailer"]):
        return [CAT_GAMING]

    # Fallback
    if CAT_ALL:
        return [CAT_ALL]
    if WP_CATEGORY_ID_DEFAULT > 0:
        return [WP_CATEGORY_ID_DEFAULT]
    return []


# ========= OpenAI generation =========
def openai_generate_fa_long(title_en: str, snippet_en: str, source_name: str, source_url: str) -> dict:
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
- Do NOT invent facts/specs/numbers. If unknown, say "جزئیات کامل در منبع".
- Target length: 500–900 Persian words.
- Use HTML with <h2> headings (2 to 4 headings).
- Include a short FAQ section with 3 Q/A pairs at the end (use <h2>سوالات متداول</h2> and <p>Q: ...</p><p>A: ...</p>).
- Return valid JSON ONLY with keys:
  title_fa, meta_title_fa, meta_description_fa, focus_keyword_fa, content_html_fa
"""

    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "Return only JSON. No markdown."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.35,
    )

    text = (resp.choices[0].message.content or "").strip()
    if not text:
        raise ValueError("OpenAI returned empty content")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            data = json.loads(text[start:end + 1])
        else:
            raise

    for k in ["title_fa", "meta_title_fa", "meta_description_fa", "focus_keyword_fa", "content_html_fa"]:
        if k not in data:
            raise ValueError(f"Missing key in OpenAI JSON: {k}")

    data["title_fa"] = clean_text(data["title_fa"])
    data["meta_title_fa"] = clean_text(data["meta_title_fa"])
    data["meta_description_fa"] = clean_text(data["meta_description_fa"])
    data["focus_keyword_fa"] = clean_text(data["focus_keyword_fa"])
    data["content_html_fa"] = (data["content_html_fa"] or "").strip()

    # Safety trims
    data["meta_title_fa"] = data["meta_title_fa"][:70]
    data["meta_description_fa"] = data["meta_description_fa"][:160]
    return data


# ========= Image extraction =========
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
    if r.status_code >= 400:
        print("SOURCE HTML fetch failed:", r.status_code)
        return None
    return extract_image_url_from_html(r.text, source_url)


def download_image_bytes(img_url: str) -> tuple[bytes, str] | tuple[None, None]:
    headers = {"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/*,*/*"}
    r = requests.get(img_url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
    if r.status_code >= 400 or not r.content:
        print("IMG download failed:", r.status_code, img_url)
        return None, None

    # Try to guess extension
    ctype = (r.headers.get("Content-Type") or "").lower()
    ext = "jpg"
    if "png" in ctype:
        ext = "png"
    elif "webp" in ctype:
        ext = "webp"
    elif "gif" in ctype:
        ext = "gif"

    return r.content, ext


def wp_upload_media(image_bytes: bytes, filename: str, alt_text: str = "") -> int:
    endpoint = f"{WP_BASE_URL}/wp-json/wp/v2/media"
    headers = {
        "Authorization": wp_auth_header(WP_USERNAME, WP_APP_PASSWORD),
        "User-Agent": USER_AGENT,
        "Content-Disposition": f'attachment; filename="{filename}"',
    }

    r = requests.post(endpoint, headers=headers, data=image_bytes, timeout=HTTP_TIMEOUT)
    print("WP MEDIA UPLOAD:", r.status_code)
    if r.status_code >= 400:
        print("WP MEDIA ERROR (first 300):", r.text[:300])
    r.raise_for_status()

    media_id = int(r.json()["id"])

    # Optional: set alt_text (requires another request)
    if alt_text:
        patch = requests.post(
            f"{WP_BASE_URL}/wp-json/wp/v2/media/{media_id}",
            headers={
                "Authorization": wp_auth_header(WP_USERNAME, WP_APP_PASSWORD),
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
            },
            json={"alt_text": alt_text},
            timeout=HTTP_TIMEOUT
        )
        print("WP MEDIA ALT PATCH:", patch.status_code)
    return media_id


# ========= RankMath updater call =========
def push_rankmath_meta(post_id: int, meta_title: str, meta_desc: str, focus_kw: str):
    if not (RANKMATH_UPDATER_URL and RANKMATH_UPDATER_TOKEN):
        return

    payload = {
        "token": RANKMATH_UPDATER_TOKEN,
        "post_id": post_id,
        "meta_title": meta_title,
        "meta_description": meta_desc,
        "focus_keyword": focus_kw,
    }

    r = requests.post(RANKMATH_UPDATER_URL, json=payload, timeout=HTTP_TIMEOUT)
    print("RANKMATH UPDATE:", r.status_code)
    if r.status_code >= 400:
        print("RANKMATH ERROR (first 300):", r.text[:300])
    r.raise_for_status()


# ========= WordPress post =========
def wp_check_me():
    endpoint = f"{WP_BASE_URL}/wp-json/wp/v2/users/me"
    headers = {
        "Authorization": wp_auth_header(WP_USERNAME, WP_APP_PASSWORD),
        "User-Agent": USER_AGENT,
    }
    r = requests.get(endpoint, headers=headers, timeout=HTTP_TIMEOUT)
    print("WP ME:", endpoint, "| status=", r.status_code)
    if r.status_code >= 400:
        print("WP ME error body (first 300):", r.text[:300])
    r.raise_for_status()
    return r.json()


def create_wp_post(title: str, content_html: str, categories: list[int], featured_media: int | None) -> int:
    endpoint = f"{WP_BASE_URL}/wp-json/wp/v2/posts"
    headers = {
        "Authorization": wp_auth_header(WP_USERNAME, WP_APP_PASSWORD),
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }

    payload = {"title": title, "content": content_html, "status": WP_POST_STATUS}
    if categories:
        payload["categories"] = categories
    if featured_media:
        payload["featured_media"] = featured_media

    r = requests.post(endpoint, headers=headers, json=payload, timeout=HTTP_TIMEOUT)
    print("WP POST:", r.status_code)
    if r.status_code >= 400:
        print("WP POST ERROR (first 500):", r.text[:500])
    r.raise_for_status()
    return int(r.json()["id"])


def build_wp_content(final_body_html: str, source_name: str, source_url: str, published_at: str, image_tag_html: str = "") -> str:
    nice_date = format_rss_date(published_at)
    footer = f"""
<hr>
<p><strong>منبع:</strong> <a href="{source_url}" target="_blank" rel="nofollow noopener">{source_name}</a></p>
<p><small>زمان انتشار منبع: {nice_date}</small></p>
""".strip()

    if image_tag_html and EMBED_IMAGE_IN_CONTENT:
        return (image_tag_html + "\n\n" + final_body_html.strip() + "\n\n" + footer).strip()

    return (final_body_html.strip() + "\n\n" + footer).strip()


def run():
    print("=== WP News Bot starting ===")
    safe_env_report()

    if LANG.lower() != "fa":
        die("Set LANG=fa")

    if not (WP_BASE_URL and WP_USERNAME and WP_APP_PASSWORD):
        die("WP_BASE_URL / WP_USERNAME / WP_APP_PASSWORD is missing")

    wp_check_me()

    init_db()
    sources = load_sources()

    print("Sources loaded:", len(sources))
    for s in sources:
        print("-", s["name"], s["feed"])

    total_added = 0
    for s in sources:
        entries = fetch_feed_entries(s["feed"])
        added = upsert_new_items(s["name"], entries[:10])
        print(f"ADDED from {s['name']}: {added}")
        total_added += added

    pending = get_pending_items(MAX_POSTS_PER_RUN)
    print("Pending to publish:", len(pending), "| MAX_POSTS_PER_RUN:", MAX_POSTS_PER_RUN)

    for (item_id, source_name, title_en, snippet_en, url, published_at) in pending:
        print("\n--- ITEM ---")
        print("Source:", source_name)
        print("URL:", url)
        print("Title EN:", title_en)

        try:
            categories = pick_categories(source_name, title_en, snippet_en)
            print("Picked categories:", categories)

            gen = openai_generate_fa_long(title_en, snippet_en, source_name, url)

            featured_media_id = None
            image_tag_html = ""

            if SET_FEATURED_IMAGE:
                img_url = fetch_source_image_url(url)
                print("Image URL:", img_url)
                if img_url:
                    img_bytes, ext = download_image_bytes(img_url)
                    if img_bytes:
                        fn = f"news-{item_id[:12]}.{ext}"
                        featured_media_id = wp_upload_media(img_bytes, fn, alt_text=gen["title_fa"])
                        image_tag_html = f'<p><img src="{WP_BASE_URL}/?attachment_id={featured_media_id}" alt="{gen["title_fa"]}"></p>'

            content_html = build_wp_content(
                final_body_html=gen["content_html_fa"],
                source_name=source_name,
                source_url=url,
                published_at=published_at,
                image_tag_html=image_tag_html
            )

            post_id = create_wp_post(
                title=gen["title_fa"],
                content_html=content_html,
                categories=categories,
                featured_media=featured_media_id
            )

            push_rankmath_meta(
                post_id=post_id,
                meta_title=gen["meta_title_fa"],
                meta_desc=gen["meta_description_fa"],
                focus_kw=gen["focus_keyword_fa"]
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

