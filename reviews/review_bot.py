import os
import re
import sys
import yaml
import requests
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_FILE = os.path.join(BASE_DIR, "reviews_queue.yaml")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
WP_BASE_URL = os.getenv("WP_BASE_URL", "").strip().rstrip("/")
WP_USERNAME = os.getenv("WP_USERNAME", "").strip()
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "").strip()

REVIEW_POST_STATUS = os.getenv("REVIEW_POST_STATUS", "draft").strip()
REVIEW_MIN_SOURCES = int(os.getenv("REVIEW_MIN_SOURCES", "3") or "3")
REVIEW_SCORE_MAX_DELTA = float(os.getenv("REVIEW_SCORE_MAX_DELTA", "0.4") or "0.4")

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "25") or "25")
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (PoormazReviewBot/1.0; +https://poormaz.com)"
).strip()


def fail(message: str):
    print(f"ERROR: {message}")
    sys.exit(1)


def clean_text(value: str) -> str:
    value = unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.og_title = ""
        self.site_name = ""
        self.description = ""
        self.text_parts = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs_dict = {str(k).lower(): (v or "") for k, v in attrs}

        if tag == "title":
            self.in_title = True

        if tag == "meta":
            prop = attrs_dict.get("property", "").lower()
            name = attrs_dict.get("name", "").lower()
            content = attrs_dict.get("content", "")

            if prop == "og:title" and content:
                self.og_title = content
            elif prop == "og:site_name" and content:
                self.site_name = content
            elif name in {"description", "twitter:description"} and content:
                self.description = content

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data):
        text = clean_text(data)
        if not text:
            return

        if self.in_title:
            self.title += " " + text

        self.text_parts.append(text)


def load_review_queue() -> list[dict]:
    if not os.path.exists(QUEUE_FILE):
        fail(f"فایل صف پیدا نشد: {QUEUE_FILE}")

    with open(QUEUE_FILE, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}

    reviews = data.get("reviews", [])
    if not isinstance(reviews, list):
        fail("مقدار reviews در reviews_queue.yaml باید یک لیست باشد.")

    return reviews


def is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def validate_review_item(item: dict, index: int) -> list[str]:
    errors = []

    if not isinstance(item, dict):
        return [f"آیتم شماره {index} باید به شکل YAML object باشد."]

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
    else:
        for url in review_urls:
            if not is_valid_url(str(url)):
                errors.append(f"لینک نقد نامعتبر است: {url}")

    return errors


def fallback_site_name(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    host = re.sub(r"^www\.", "", host)
    host = re.sub(r"\.(com|net|org|io|co|gg|tv|uk)$", "", host)
    return host or "Unknown Source"


def fetch_page_info(url: str) -> dict:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    }

    try:
        response = requests.get(
            url,
            headers=headers,
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

    parser = PageParser()
    try:
        parser.feed(response.text or "")
    except Exception:
        pass

    page_title = clean_text(parser.og_title or parser.title)
    site_name = clean_text(parser.site_name) or fallback_site_name(response.url)
    description = clean_text(parser.description)
    page_text = clean_text(" ".join(parser.text_parts))

    return {
        "ok": True,
        "url": response.url,
        "status": response.status_code,
        "site_name": site_name,
        "title": page_title,
        "description": description,
        "text_chars": len(page_text),
        "text_excerpt": page_text[:600],
    }


def print_page_result(label: str, result: dict):
    print(f"\n[{label}]")

    if not result["ok"]:
        print(f"FAILED | status={result['status']} | {result['error']}")
        return

    print(f"OK | HTTP {result['status']}")
    print(f"Site: {result['site_name']}")
    print(f"Title: {result['title'] or '(no title found)'}")
    print(f"Text chars: {result['text_chars']}")
    print(f"URL: {result['url']}")


def main():
    print("=== Poormaz Review Bot: Source Inspector ===")
    print(f"Post status: {REVIEW_POST_STATUS}")
    print(f"Minimum review sources: {REVIEW_MIN_SOURCES}")
    print(f"Allowed score delta: {REVIEW_SCORE_MAX_DELTA}")

    missing_secrets = [
        name for name, value in {
            "OPENAI_API_KEY": OPENAI_API_KEY,
            "WP_BASE_URL": WP_BASE_URL,
            "WP_USERNAME": WP_USERNAME,
            "WP_APP_PASSWORD": WP_APP_PASSWORD,
        }.items()
        if not value
    ]

    if missing_secrets:
        fail("Secrets/ENV ناقص هستند: " + ", ".join(missing_secrets))

    reviews = load_review_queue()

    if not reviews:
        print("\nصف نقدها خالی است. فعلاً چیزی برای بررسی وجود ندارد.")
        return

    for index, item in enumerate(reviews, start=1):
        errors = validate_review_item(item, index)
        game = str(item.get("game", "")).strip() or f"آیتم {index}"

        print("\n" + "=" * 65)
        print(f"GAME: {game}")

        if errors:
            print("INVALID:")
            for error in errors:
                print(f" - {error}")
            continue

        print(f"Platform: {item['platform']}")
        print(f"Release date: {item.get('release_date', 'Unknown')}")

        metacritic_result = fetch_page_info(item["metacritic_url"])
        print_page_result("METACRITIC", metacritic_result)

        for source_index, review_url in enumerate(item["review_urls"], start=1):
            source_result = fetch_page_info(review_url)
            print_page_result(f"REVIEW SOURCE {source_index}", source_result)

    print("\nInspection finished. No WordPress post was created.")


if __name__ == "__main__":
    main()
