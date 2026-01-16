import os
import re
import json
import time
import base64
import hashlib
import sqlite3
from datetime import datetime, timezone

import yaml
import feedparser
import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
WP_BASE_URL = os.getenv("WP_BASE_URL", "").rstrip("/")
WP_USERNAME = os.getenv("WP_USERNAME", "").strip()
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "").strip()

WP_POST_STATUS = os.getenv("WP_POST_STATUS", "draft").strip()
WP_CATEGORY_ID = int(os.getenv("WP_CATEGORY_ID", "0"))
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "3"))
LANG = os.getenv("LANG", "fa").strip()

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))
USER_AGENT = os.getenv("USER_AGENT", "WPNewsBot/1.0").strip()

DB_FILE = "news_cache.db"
SOURCES_FILE = "sources.yaml"

MODEL = "gpt-4o-mini"  # می‌توانید تغییر دهید

def die(msg: str):
    raise SystemExit(msg)

def clean_text(s: str) -> str:
    s = s or ""
    s = re.sub(r"<[^>]+>", " ", s)  # strip html
    s = re.sub(r"\s+", " ", s).strip()
    return s

def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()

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

def load_sources():
    if not os.path.exists(SOURCES_FILE):
        die(f"Missing {SOURCES_FILE}")
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    sources = cfg.get("sources", [])
    if not sources:
        die("sources.yaml has no sources")
    return sources

def fetch_feed_entries(feed_url: str):
    fp = feedparser.parse(feed_url)
    entries = fp.entries or []
    return entries

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
            published = datetime.now(timezone.utc).isoformat()

        now = datetime.now(timezone.utc).isoformat()

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

def openai_generate_fa(title_en: str, snippet_en: str, source_name: str, source_url: str) -> dict:
    if not OPENAI_API_KEY:
        die("OPENAI_API_KEY is missing")

    client = OpenAI(api_key=OPENAI_API_KEY)

    prompt = f"""
You are a Persian (Farsi) tech & gaming news editor.

Input:
- English title: {title_en}
- English snippet: {snippet_en}
- Source name: {source_name}
- Source URL: {source_url}

Task (IMPORTANT):
- Write ORIGINAL Persian content (do not copy source text).
- Produce: Persian title + Persian summary (90 to 160 words) + 3 bullet key points + 1 short "Why it matters" sentence.
- Keep it neutral, factual, and readable for Persian gamers/PC builders.
- Do NOT invent numbers/specs. If not in snippet/title, say "جزئیات کامل در منبع".
- Return valid JSON only with keys:
  title_fa, summary_fa, bullets_fa (array of 3 strings), why_it_matters_fa
"""

    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "Return only JSON. No markdown."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3,
    )

    text = resp.choices[0].message.content.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            data = json.loads(text[start:end+1])
        else:
            raise

    for k in ["title_fa", "summary_fa", "bullets_fa", "why_it_matters_fa"]:
        if k not in data:
            raise ValueError(f"Missing key in OpenAI JSON: {k}")

    if not isinstance(data["bullets_fa"], list) or len(data["bullets_fa"]) < 3:
        raise ValueError("bullets_fa must be an array of at least 3 strings")

    data["title_fa"] = clean_text(data["title_fa"])
    data["summary_fa"] = clean_text(data["summary_fa"])
    data["why_it_matters_fa"] = clean_text(data["why_it_matters_fa"])
    data["bullets_fa"] = [clean_text(x) for x in data["bullets_fa"][:3]]

    return data

def wp_auth_header(username: str, app_password: str) -> str:
    token = base64.b64encode(f"{username}:{app_password}".encode("utf-8")).decode("utf-8")
    return f"Basic {token}"

def create_wp_post(title: str, content_html: str) -> int:
    if not (WP_BASE_URL and WP_USERNAME and WP_APP_PASSWORD):
        die("WP_BASE_URL / WP_USERNAME / WP_APP_PASSWORD is missing")

    endpoint = f"{WP_BASE_URL}/wp-json/wp/v2/posts"
    headers = {
        "Authorization": wp_auth_header(WP_USERNAME, WP_APP_PASSWORD),
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }

    payload = {
        "title": title,
        "content": content_html,
        "status": WP_POST_STATUS,
    }
    if WP_CATEGORY_ID > 0:
        payload["categories"] = [WP_CATEGORY_ID]

    r = requests.post(endpoint, headers=headers, json=payload, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return int(r.json()["id"])

def build_wp_content(gen: dict, source_name: str, source_url: str, published_at: str) -> str:
    bullets = "".join([f"<li>{b}</li>" for b in gen["bullets_fa"]])

    html = f"""
<p>{gen["summary_fa"]}</p>

<ul>
{bullets}
</ul>

<p><strong>چرا مهم است:</strong> {gen["why_it_matters_fa"]}</p>

<hr>
<p><strong>منبع:</strong> <a href="{source_url}" target="_blank" rel="nofollow noopener">{source_name}</a></p>
<p><small>زمان انتشار منبع (RSS): {clean_text(published_at)}</small></p>
""".strip()

    return html

def run():
    if LANG.lower() != "fa":
        die("This bot is configured for Persian (fa). Set LANG=fa")

    init_db()
    sources = load_sources()

    total_added = 0
    for s in sources:
        name = s["name"]
        feed = s["feed"]
        entries = fetch_feed_entries(feed)
        total_added += upsert_new_items(name, entries[:10])

    pending = get_pending_items(MAX_POSTS_PER_RUN)

    for (item_id, source_name, title_en, snippet_en, url, published_at) in pending:
        try:
            gen = openai_generate_fa(
                title_en=title_en,
                snippet_en=snippet_en,
                source_name=source_name,
                source_url=url
            )
            wp_title = gen["title_fa"]
            wp_content = build_wp_content(gen, source_name, url, published_at)
            wp_post_id = create_wp_post(wp_title, wp_content)
            mark_posted(item_id, wp_post_id)
            time.sleep(1.2)  # نرخ‌دهی ساده
        except Exception:
            mark_failed(item_id)

if __name__ == "__main__":
    run()


