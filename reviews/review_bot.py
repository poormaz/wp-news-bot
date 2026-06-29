import os
import sys
import yaml
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


def fail(message: str):
    print(f"ERROR: {message}")
    sys.exit(1)


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
        return [f"آیتم شماره {index} باید به شکل YAML object باشد."]

    if not str(item.get("game", "")).strip():
        errors.append("game ندارد.")

    if not str(item.get("platform", "")).strip():
        errors.append("platform ندارد.")

    if not str(item.get("metacritic_url", "")).strip():
        errors.append("metacritic_url ندارد.")

    review_urls = item.get("review_urls", [])
    if not isinstance(review_urls, list):
        errors.append("review_urls باید لیست باشد.")
    elif len(review_urls) < REVIEW_MIN_SOURCES:
        errors.append(
            f"حداقل {REVIEW_MIN_SOURCES} لینک نقد لازم است، "
            f"ولی فقط {len(review_urls)} لینک وارد شده."
        )

    return errors


def main():
    print("=== Poormaz Review Bot: Queue Check ===")
    print(f"Post status: {REVIEW_POST_STATUS}")
    print(f"Minimum review sources: {REVIEW_MIN_SOURCES}")
    print(f"Allowed score delta: {REVIEW_SCORE_MAX_DELTA}")
    print()

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
        print("صف نقدها خالی است. فعلاً چیزی برای پردازش وجود ندارد.")
        return

    valid_count = 0

    for index, item in enumerate(reviews, start=1):
        errors = validate_review_item(item, index)
        game = str(item.get("game", "")).strip() or f"آیتم {index}"

        if errors:
            print(f"INVALID: {game}")
            for error in errors:
                print(f" - {error}")
            continue

        valid_count += 1
        print(f"READY: {game} | {item['platform']} | {len(item['review_urls'])} sources")

    print()
    print(f"Valid review jobs: {valid_count}/{len(reviews)}")


if __name__ == "__main__":
    main()
