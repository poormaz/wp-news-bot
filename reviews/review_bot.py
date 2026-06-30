import os
import re
import sys
import json
import yaml
import requests
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_FILE = os.path.join(BASE_DIR, "reviews_queue.yaml")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.2") or "0.2")

REVIEW_MIN_SOURCES = int(os.getenv("REVIEW_MIN_SOURCES", "3") or "3")
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30") or "30")
SOURCE_TEXT_LIMIT = int(os.getenv("REVIEW_SOURCE_TEXT_LIMIT", "18000") or "18000")

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (PoormazReviewBot/1.0; +https://poormaz.com)"
).strip()

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})


def fail(message: str):
    print(f"ERROR: {message}")
    sys.exit(1)


def clean_text(value: str) -> str:
    value = unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def fallback_site_name(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    host = re.sub(r"^www\.", "", host)
    host = re.sub(r"\.(com|net|org|io|co|gg|tv|uk)$", "", host)
    return host or "Unknown Source"


class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title_parts = []
        self.in_title = False
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs_dict = {str(k).lower(): (v or "") for k, v in attrs}

        if tag == "title":
            self.in_title = True

        if tag == "meta":
            key = (
                attrs_dict.get("property", "")
                or attrs_dict.get("name", "")
                or attrs_dict.get("itemprop", "")
            ).lower().strip()

            content = attrs_dict.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            text = clean_text(data)
            if text:
                self.title_parts.append(text)


class VisibleTextParser(HTMLParser):
    SKIP_TAGS = {
        "script", "style", "noscript", "svg", "iframe",
        "canvas", "template", "form"
    }

    def __init__(self):
        super().__init__()
        self.skip_depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.SKIP_TAGS:
            self.skip_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in self.SKIP_TAGS and self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_data(self, data):
        if self.skip_depth:
            return

        text = clean_text(data)
        if text:
            self.parts.append(text)


def select_main_html(html: str) -> str:
    html = html or ""

    article_matches = re.findall(
        r"(?is)<article\b[^>]*>(.*?)</article>",
        html
    )

    if article_matches:
        return max(article_matches, key=len)

    main_matches = re.findall(
        r"(?is)<main\b[^>]*>(.*?)</main>",
        html
    )

    if main_matches:
        return max(main_matches, key=len)

    return html


def html_to_text(html: str) -> str:
    parser = VisibleTextParser()

    try:
        parser.feed(html or "")
    except Exception:
        pass

    text = clean_text(" ".join(parser.parts))
    return text


def extract_page_info(url: str) -> dict:
    try:
        response = SESSION.get(
            url,
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        return {
            "ok": False,
            "url": url,
            "status": 0,
            "error": repr(exc),
        }

    if response.status_code >= 400:
        return {
            "ok": False,
            "url": response.url,
            "status": response.status_code,
            "error": f"HTTP {response.status_code}",
        }

    html = response.text or ""

    meta_parser = MetaParser()
    try:
        meta_parser.feed(html)
    except Exception:
        pass

    main_html = select_main_html(html)
    page_text = html_to_text(main_html)

    if len(page_text) < 300:
        page_text = html_to_text(html)

    title = clean_text(
        meta_parser.meta.get("og:title")
        or meta_parser.meta.get("twitter:title")
        or " ".join(meta_parser.title_parts)
    )

    site_name = clean_text(
        meta_parser.meta.get("og:site_name")
        or meta_parser.meta.get("application-name")
        or fallback_site_name(response.url)
    )

    description = clean_text(
        meta_parser.meta.get("description")
        or meta_parser.meta.get("twitter:description")
        or meta_parser.meta.get("og:description")
    )

    return {
        "ok": True,
        "url": response.url,
        "status": response.status_code,
        "site_name": site_name,
        "title": title,
        "description": description,
        "text": page_text[:SOURCE_TEXT_LIMIT],
        "text_chars": len(page_text),
    }


def load_review_queue() -> list[dict]:
    if not os.path.exists(QUEUE_FILE):
        fail(f"فایل صف پیدا نشد: {QUEUE_FILE}")

    with open(QUEUE_FILE, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}

    reviews = data.get("reviews", [])

    if not isinstance(reviews, list):
        fail("مقدار reviews در reviews_queue.yaml باید یک لیست باشد.")

    return reviews


def validate_review_item(item: dict, index: int) -> list[str]:
    errors = []

    if not isinstance(item, dict):
        return [f"آیتم شماره {index} باید object باشد."]

    if not str(item.get("game", "")).strip():
        errors.append("game ندارد.")

    if not str(item.get("platform", "")).strip():
        errors.append("platform ندارد.")

    metacritic_url = str(item.get("metacritic_url", "")).strip()
    if not metacritic_url:
        errors.append("metacritic_url ندارد.")
    elif not is_valid_url(metacritic_url):
        errors.append("metacritic_url معتبر نیست.")

    review_urls = item.get("review_urls", [])

    if not isinstance(review_urls, list):
        errors.append("review_urls باید لیست باشد.")
    elif len(review_urls) < REVIEW_MIN_SOURCES:
        errors.append(
            f"حداقل {REVIEW_MIN_SOURCES} لینک نقد لازم است، "
            f"ولی فقط {len(review_urls)} لینک وارد شده."
        )

    return errors


def parse_json_response(text: str) -> dict:
    text = (text or "").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")

        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])

    raise ValueError("OpenAI JSON response could not be parsed.")


def ask_openai_json(client: OpenAI, prompt: str) -> dict:
    last_error = None

    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                temperature=OPENAI_TEMPERATURE,
                max_tokens=1200,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Return exactly one valid JSON object. "
                            "Never invent facts or scores."
                        ),
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
            )

            raw = response.choices[0].message.content or ""
            return parse_json_response(raw)

        except Exception as exc:
            last_error = exc
            print(f"OpenAI attempt {attempt + 1} failed: {repr(exc)}")

    raise RuntimeError(f"OpenAI failed twice: {repr(last_error)}")


def as_score_10(value) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None

    if 0 <= score <= 10:
        return round(score, 1)

    return None


def as_score_100(value) -> int | None:
    try:
        score = int(float(value))
    except (TypeError, ValueError):
        return None

    if 0 <= score <= 100:
        return score

    return None


def short_list(value, max_items=4) -> list[str]:
    if not isinstance(value, list):
        return []

    result = []

    for item in value:
        text = clean_text(str(item))
        if text and text not in result:
            result.append(text)

        if len(result) >= max_items:
            break

    return result


def analyze_metacritic(client: OpenAI, game: str, platform: str, page: dict) -> dict:
    prompt = f"""
You are extracting factual Metacritic data for a game review dossier.

Game: {game}
Target platform: {platform}
Page title: {page["title"]}
Page URL: {page["url"]}

Extract only information visibly supported by the provided page text.

Return JSON with exactly:
- metascore_100: integer from 0 to 100, or null
- critic_review_count: integer, or null
- platform_found: string, or null
- confidence_note_fa: very short Persian note about any uncertainty

Page text:
{page["text"]}
""".strip()

    raw = ask_openai_json(client, prompt)

    return {
        "metascore_100": as_score_100(raw.get("metascore_100")),
        "critic_review_count": (
            int(raw["critic_review_count"])
            if str(raw.get("critic_review_count", "")).isdigit()
            else None
        ),
        "platform_found": clean_text(str(raw.get("platform_found") or "")) or None,
        "confidence_note_fa": clean_text(
            str(raw.get("confidence_note_fa") or "")
        ),
        "url": page["url"],
    }


def analyze_review_source(client: OpenAI, game: str, platform: str, page: dict) -> dict:
    prompt = f"""
You are extracting structured information from one professional game review.

Game: {game}
Requested platform: {platform}
Website: {page["site_name"]}
Review title: {page["title"]}
Review URL: {page["url"]}

Rules:
- Use only the supplied page text.
- Never quote the review directly.
- Do not infer a score from tone.
- Set review_score_10 to null unless the review explicitly gives a score.
- If an explicit score is out of 10, normalize it to 0-10.
- Write all analysis fields in fluent Persian.
- Keep every list item short and factual.

Return JSON with exactly:
- original_score: string or null
- review_score_10: number from 0 to 10, or null
- positives_fa: array of 2 to 4 short points
- negatives_fa: array of 2 to 4 short points
- technical_notes_fa: array of 0 to 3 short points about performance, bugs, controls, UI or optimization
- verdict_fa: one concise Persian paragraph
- platform_mentioned: string or null

Page text:
{page["text"]}
""".strip()

    raw = ask_openai_json(client, prompt)

    return {
        "site_name": page["site_name"],
        "title": page["title"],
        "url": page["url"],
        "original_score": clean_text(str(raw.get("original_score") or "")) or None,
        "review_score_10": as_score_10(raw.get("review_score_10")),
        "positives_fa": short_list(raw.get("positives_fa")),
        "negatives_fa": short_list(raw.get("negatives_fa")),
        "technical_notes_fa": short_list(raw.get("technical_notes_fa"), 3),
        "verdict_fa": clean_text(str(raw.get("verdict_fa") or "")),
        "platform_mentioned": clean_text(
            str(raw.get("platform_mentioned") or "")
        ) or None,
    }


def safe_filename(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", value or "")
    value = re.sub(r"-+", "-", value).strip("-")
    return value.lower() or "review-dossier"


def save_dossier(game: str, dossier: dict) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    filename = f"{safe_filename(game)}-dossier.json"
    output_path = os.path.join(OUTPUT_DIR, filename)

    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(dossier, file, ensure_ascii=False, indent=2)

    return output_path


def process_review_job(client: OpenAI, item: dict):
    game = str(item["game"]).strip()
    platform = str(item["platform"]).strip()

    print("\n" + "=" * 72)
    print(f"ANALYZING: {game} | {platform}")

    metacritic_page = extract_page_info(item["metacritic_url"])

    if not metacritic_page["ok"]:
        print(
            f"SKIP: Metacritic unavailable "
            f"({metacritic_page['status']})"
        )
        return

    source_pages = []

    for url in item["review_urls"]:
        page = extract_page_info(url)

        if page["ok"]:
            source_pages.append(page)
            print(
                f"Loaded: {page['site_name']} "
                f"| {page['title'][:80]}"
            )
        else:
            print(f"Skipped source: {url} | HTTP {page['status']}")

    if len(source_pages) < REVIEW_MIN_SOURCES:
        print(
            f"SKIP: only {len(source_pages)} usable review sources. "
            f"Need at least {REVIEW_MIN_SOURCES}."
        )
        return

    metacritic_data = analyze_metacritic(
        client,
        game,
        platform,
        metacritic_page,
    )

    source_analyses = []

    for page in source_pages:
        print(f"Analyzing with OpenAI: {page['site_name']}")

        source_analyses.append(
            analyze_review_source(
                client,
                game,
                platform,
                page,
            )
        )

    dossier = {
        "game": game,
        "platform": platform,
        "release_date": str(item.get("release_date") or ""),
        "metacritic": metacritic_data,
        "review_sources": source_analyses,
        "status": "analysis_only",
        "wordpress_post_created": False,
    }

    output_path = save_dossier(game, dossier)

    print("\n--- REVIEW DOSSIER SUMMARY ---")
    print(f"Game: {game}")
    print(f"Metascore: {metacritic_data['metascore_100']}")
    print(f"Review count: {metacritic_data['critic_review_count']}")

    for source in source_analyses:
        print(
            f"- {source['site_name']}: "
            f"{source['original_score'] or 'No visible score'}"
        )

    print(f"\nSaved: {output_path}")
    print("\n--- REVIEW DOSSIER JSON ---")
    print(json.dumps(dossier, ensure_ascii=False, indent=2))
    print("\nNo WordPress post was created.")


def main():
    print("=== Poormaz Review Bot: Dossier Builder ===")

    if not OPENAI_API_KEY:
        fail("OPENAI_API_KEY is missing.")

    reviews = load_review_queue()

    if not reviews:
        print("صف نقدها خالی است.")
        return

    client = OpenAI(api_key=OPENAI_API_KEY)

    for index, item in enumerate(reviews, start=1):
        errors = validate_review_item(item, index)

        if errors:
            print(f"\nINVALID ITEM #{index}")
            for error in errors:
                print(f"- {error}")
            continue

        process_review_job(client, item)


if __name__ == "__main__":
    main()
