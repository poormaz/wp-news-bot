import os
import re
import sys
import json
import yaml
import hashlib
from datetime import datetime, timezone
import requests
from html import unescape, escape
from html.parser import HTMLParser
from urllib.parse import urlparse
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_FILE = os.path.join(BASE_DIR, "reviews_queue.yaml")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
EVIDENCE_CACHE_DIR = os.path.join(BASE_DIR, "cache", "evidence")
EVIDENCE_CACHE_VERSION = "source_evidence_cache_v1"

# شواهدی که یک انسان واقعاً در منبع بررسی کرده، بیرون از cache نگه داشته می‌شوند.
# این لایه نه OpenAI را دوباره اجرا می‌کند و نه cache خودکار را تغییر می‌دهد.
MANUAL_EVIDENCE_FILE = os.path.join(BASE_DIR, "manual_evidence.yaml")


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "1" if default else "0").strip().casefold()
    return value in {"1", "true", "yes", "on"}


# شواهد تأییدشده‌ی هر منبع بعد از اولین تحلیل ذخیره می‌شوند.
# Refresh فقط با تنظیم صریح انجام می‌شود، نه به‌خاطر نوسان مدل.
REVIEW_EVIDENCE_CACHE_ENABLED = env_flag("REVIEW_EVIDENCE_CACHE", True)
REVIEW_REFRESH_EVIDENCE = env_flag("REVIEW_REFRESH_EVIDENCE", False)
REVIEW_REFRESH_INCOMPLETE_EVIDENCE = env_flag(
    "REVIEW_REFRESH_INCOMPLETE_EVIDENCE", False
)
EVIDENCE_CACHE_MIN_VERIFIED_POINTS = int(
    os.getenv("EVIDENCE_CACHE_MIN_VERIFIED_POINTS", "2") or "2"
)
EVIDENCE_CACHE_MIN_VERIFIED_POINTS = min(
    max(EVIDENCE_CACHE_MIN_VERIFIED_POINTS, 1),
    6,
)

# بازخوانی انتخابی نباید یک منبع کم‌محتوا را تا ابد به مدل برگرداند.
# بعد از چند تلاش ناموفق، منبع برای بازبینی دستی علامت می‌خورد.
EVIDENCE_CACHE_MAX_FAILED_SELECTIVE_REFRESHES = int(
    os.getenv("EVIDENCE_CACHE_MAX_FAILED_SELECTIVE_REFRESHES", "2") or "2"
)
EVIDENCE_CACHE_MAX_FAILED_SELECTIVE_REFRESHES = min(
    max(EVIDENCE_CACHE_MAX_FAILED_SELECTIVE_REFRESHES, 1),
    5,
)

# ادغام cache قبلی و استخراج تازه نباید به رشد بی‌نهایت فهرست شواهد منجر شود.
EVIDENCE_CACHE_MAX_MERGED_POINTS_PER_SECTION = int(
    os.getenv("EVIDENCE_CACHE_MAX_MERGED_POINTS_PER_SECTION", "6") or "6"
)
EVIDENCE_CACHE_MAX_MERGED_POINTS_PER_SECTION = min(
    max(EVIDENCE_CACHE_MAX_MERGED_POINTS_PER_SECTION, 2),
    10,
)

# سقفِ جدا برای ورود دستی تا یک فایل YAML نتواند مقاله را با فهرست بی‌پایان پر کند.
MANUAL_EVIDENCE_MAX_POINTS_PER_SECTION = int(
    os.getenv("MANUAL_EVIDENCE_MAX_POINTS_PER_SECTION", "4") or "4"
)
MANUAL_EVIDENCE_MAX_POINTS_PER_SECTION = min(
    max(MANUAL_EVIDENCE_MAX_POINTS_PER_SECTION, 1),
    6,
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0") or "0")

# هدف تحریریه ۵ تا ۱۰ نقد برای هر پرونده است، نه فقط حداقل مطلق. حداقل واقعی
# را روی ۵ می‌گذاریم تا «اجماع منتقدان» و «تفاوت دیدگاه سایت‌ها» معنای واقعی
# داشته باشند؛ سقف ۱۰ فقط یک یادآوریِ نرم است، نه محدودیت سخت‌گیرانه.
REVIEW_MIN_SOURCES = int(os.getenv("REVIEW_MIN_SOURCES", "5") or "5")
REVIEW_RECOMMENDED_MAX_SOURCES = int(
    os.getenv("REVIEW_RECOMMENDED_MAX_SOURCES", "10") or "10"
)
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30") or "30")
SOURCE_TEXT_LIMIT = int(os.getenv("REVIEW_SOURCE_TEXT_LIMIT", "18000") or "18000")
REVIEW_SCORE_MAX_DELTA = float(
    os.getenv("REVIEW_SCORE_MAX_DELTA", "0.4") or "0.4"
)
REVIEW_METACRITIC_WEIGHT = float(
    os.getenv("REVIEW_METACRITIC_WEIGHT", "0.70") or "0.70"
)
REVIEW_METACRITIC_WEIGHT = min(max(REVIEW_METACRITIC_WEIGHT, 0.0), 1.0)

# کارت‌های دسته‌ای عمداً فقط کمی از امتیاز کلی فاصله می‌گیرند.
# با شواهد کم، دقتِ ظاهری ساختن از جعل عدد بهتر نیست.
REVIEW_CATEGORY_MAX_DELTA = float(
    os.getenv("REVIEW_CATEGORY_MAX_DELTA", "1.0") or "1.0"
)
REVIEW_CATEGORY_MAX_DELTA = min(
    max(REVIEW_CATEGORY_MAX_DELTA, 0.2),
    2.0,
)


# پیش‌نمایش مقاله فقط فایل محلی تولید می‌کند و هنوز هیچ پستی در وردپرس نمی‌سازد.
ARTICLE_MAX_POINTS_PER_SECTION = int(
    os.getenv("ARTICLE_MAX_POINTS_PER_SECTION", "6") or "6"
)
ARTICLE_MAX_POINTS_PER_SECTION = min(
    max(ARTICLE_MAX_POINTS_PER_SECTION, 2),
    10,
)


# وردپرس در این بات به‌صورت پیش‌فرض کاملاً خاموش است. فقط ورودی صریح Workflow
# اجازه‌ی ساخت Draft می‌دهد و حتی در آن حالت هم انتشار عمومی ممنوع است.
REVIEW_CREATE_WORDPRESS_DRAFT = env_flag("REVIEW_CREATE_WORDPRESS_DRAFT", False)
REVIEW_WP_CATEGORY_ID = int(
    os.getenv("REVIEW_WP_CATEGORY_ID", os.getenv("CAT_REVIEWS", "0")) or "0"
)
REVIEW_WP_CATEGORY_ID = max(REVIEW_WP_CATEGORY_ID, 0)
WP_BASE_URL = os.getenv("WP_BASE_URL", "").strip().rstrip("/")
WP_USERNAME = os.getenv("WP_USERNAME", "").strip()
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "").strip()
WP_POST_STATUS = os.getenv("REVIEW_POST_STATUS", "draft").strip().casefold()
WP_DRAFT_REQUEST_TIMEOUT = int(os.getenv("WP_DRAFT_REQUEST_TIMEOUT", "30") or "30")
WP_DRAFT_REQUEST_TIMEOUT = min(max(WP_DRAFT_REQUEST_TIMEOUT, 10), 90)

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (PoormazReviewBot/1.0; +https://poormaz.com)",
).strip()

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
)



def canonical_cache_url(url: str) -> str:
    """URLهای یکسان با slash یا پارامترهای رهگیری متفاوت را یکی می‌کند."""
    parsed = urlparse((url or "").strip())
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.netloc or "").lower()
    path = re.sub(r"/+", "/", parsed.path or "/")

    if path != "/":
        path = path.rstrip("/")

    # لینک‌های نقد در صف query کاربردی ندارند؛ UTM و مشابه آن نباید cache جدا بسازند.
    return f"{scheme}://{host}{path}"


def _cache_key(game: str, platform: str, review_url: str) -> str:
    payload = "|".join(
        [
            EVIDENCE_CACHE_VERSION,
            clean_text(game).casefold(),
            clean_text(platform).casefold(),
            canonical_cache_url(review_url),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evidence_cache_path(game: str, platform: str, review_url: str) -> str:
    return os.path.join(EVIDENCE_CACHE_DIR, f"{_cache_key(game, platform, review_url)}.json")


def _json_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _evidence_key(point: dict) -> str:
    """کلید پایدار برای حذف نقل‌قول تکراری، حتی با ترجمه‌ی فارسی متفاوت."""
    quote = clean_text(str((point or {}).get("evidence_en") or "")).casefold()
    if quote:
        return quote
    return clean_text(str((point or {}).get("point_fa") or "")).casefold()


def _dedupe_evidence_points(points: list, limit: int) -> list[dict]:
    output = []
    seen = set()

    for point in points or []:
        if not isinstance(point, dict):
            continue
        point_fa = clean_text(str(point.get("point_fa") or ""))
        evidence_en = clean_text(str(point.get("evidence_en") or ""))
        if not point_fa or not evidence_en:
            continue

        key = _evidence_key({"point_fa": point_fa, "evidence_en": evidence_en})
        if not key or key in seen:
            continue

        seen.add(key)
        output.append({"point_fa": point_fa, "evidence_en": evidence_en})
        if len(output) >= limit:
            break

    return output



def _manual_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _manual_point_is_valid(point: dict) -> bool:
    """اعتبارسنجی ساختاری برای شاهدی که سردبیر شخصاً با منبع تطبیق داده است."""
    if not isinstance(point, dict):
        return False

    point_fa = clean_text(str(point.get("point_fa") or ""))
    evidence_en = clean_text(str(point.get("evidence_en") or ""))

    if len(point_fa) < 3 or len(evidence_en) < 8:
        return False

    word_count = len(re.findall(r"\b[\w'-]+\b", evidence_en))
    return 8 <= word_count <= 22


def load_manual_evidence_entries() -> tuple[list[dict], dict]:
    """
    فایل دستی عمداً یک لایه‌ی جدا از cache است. خراب بودن آن نباید کل bot را
    زمین بزند، اما در لاگ و dossier قابل‌تشخیص باقی می‌ماند.
    """
    info = {
        "file": os.path.basename(MANUAL_EVIDENCE_FILE),
        "status": "missing",
        "error": "",
    }

    if not os.path.exists(MANUAL_EVIDENCE_FILE):
        return [], info

    try:
        with open(MANUAL_EVIDENCE_FILE, "r", encoding="utf-8") as file:
            payload = yaml.safe_load(file) or {}
    except (OSError, yaml.YAMLError) as exc:
        info["status"] = "invalid"
        info["error"] = clean_text(str(exc))
        return [], info

    if isinstance(payload, list):
        raw_entries = payload
    elif isinstance(payload, dict):
        raw_entries = payload.get("entries", [])
    else:
        raw_entries = []

    if not isinstance(raw_entries, list):
        info["status"] = "invalid"
        info["error"] = "کلید entries باید یک فهرست باشد"
        return [], info

    entries = [entry for entry in raw_entries if isinstance(entry, dict)]
    info["status"] = "loaded"
    info["entry_count"] = len(entries)
    return entries, info


def _manual_entry_matches_source(
    entry: dict,
    game: str,
    platform: str,
    requested_url: str,
) -> bool:
    if not _manual_bool(entry.get("reviewed", False)):
        return False

    if clean_text(str(entry.get("game") or "")).casefold() != clean_text(game).casefold():
        return False

    entry_platform = clean_text(str(entry.get("platform") or ""))
    if entry_platform and entry_platform.casefold() != clean_text(platform).casefold():
        return False

    source_url = str(entry.get("source_url") or entry.get("url") or "").strip()
    if not is_valid_url(source_url):
        return False

    return canonical_cache_url(source_url) == canonical_cache_url(requested_url)


def _apply_manual_evidence_entries(
    analysis: dict,
    entries: list[dict],
) -> tuple[dict, dict]:
    """نقاط دستیِ معتبر را با شواهد موجود ادغام می‌کند، بدون دست‌زدن به cache."""
    output = _json_copy(analysis or {})
    added_by_section = {"positives": 0, "negatives": 0, "technical_notes": 0}
    matched_notes = []
    matched_count = 0

    for entry in entries or []:
        if not isinstance(entry, dict):
            continue

        matched_count += 1
        note_fa = clean_text(str(entry.get("note_fa") or ""))
        if note_fa:
            matched_notes.append(note_fa)

        for section in ("positives", "negatives", "technical_notes"):
            current = output.get(section, []) or []
            candidate = entry.get(section, []) or []
            if not isinstance(candidate, list):
                continue

            valid_candidate = [
                {
                    "point_fa": clean_text(str(point.get("point_fa") or "")),
                    "evidence_en": clean_text(str(point.get("evidence_en") or "")),
                }
                for point in candidate
                if _manual_point_is_valid(point)
            ]

            before_keys = {
                _evidence_key(point)
                for point in current
                if isinstance(point, dict)
            }
            merged = _dedupe_evidence_points(
                list(current) + valid_candidate,
                EVIDENCE_CACHE_MAX_MERGED_POINTS_PER_SECTION,
            )
            after_keys = {
                _evidence_key(point)
                for point in merged
                if isinstance(point, dict)
            }
            added_by_section[section] += len(after_keys - before_keys)
            output[section] = merged

    added_total = sum(added_by_section.values())
    status = "applied" if added_total else (
        "matched_no_new_points" if matched_count else "not_applied"
    )

    metadata = {
        "status": status,
        "file": os.path.basename(MANUAL_EVIDENCE_FILE),
        "matched_entries": matched_count,
        "points_added": added_total,
        "points_added_by_section": added_by_section,
        "notes_fa": matched_notes[:3],
        "policy_fa": (
            "این شواهد به‌صورت دستی با منبع تطبیق داده شده‌اند و فقط در خروجی "
            "فعلی اعمال می‌شوند؛ cache خودکار و درخواست OpenAI تغییر نمی‌کنند."
        ),
    }
    return output, metadata


def apply_manual_evidence_overlay(
    analysis: dict,
    game: str,
    platform: str,
    requested_url: str,
) -> tuple[dict, dict]:
    entries, file_info = load_manual_evidence_entries()

    if file_info.get("status") != "loaded":
        metadata = {
            "status": file_info.get("status", "missing"),
            "file": file_info.get("file", os.path.basename(MANUAL_EVIDENCE_FILE)),
            "matched_entries": 0,
            "points_added": 0,
            "points_added_by_section": {
                "positives": 0,
                "negatives": 0,
                "technical_notes": 0,
            },
            "notes_fa": [],
            "error": file_info.get("error", ""),
        }
        return _json_copy(analysis or {}), metadata

    matches = [
        entry
        for entry in entries
        if _manual_entry_matches_source(entry, game, platform, requested_url)
    ]

    output, metadata = _apply_manual_evidence_entries(analysis, matches)
    metadata["file_status"] = file_info.get("status")
    metadata["entry_count"] = file_info.get("entry_count", 0)

    if metadata.get("status") == "applied":
        output["manual_evidence"] = metadata

    return output, metadata

def merge_cached_and_candidate_analysis(cached_analysis: dict, candidate_analysis: dict) -> dict:
    """شواهد معتبر قدیمی را نگه می‌دارد و فقط یافته‌های تازه را به آن اضافه می‌کند."""
    merged = _json_copy(candidate_analysis or {})

    # برای متادیتای پایدار، داده‌ی تازه در اولویت است اما هیچ فیلد ضروری گم نمی‌شود.
    for key in (
        "site_name", "title", "url", "original_score", "review_score_10",
        "score_method", "score_confidence", "score_evidence", "platform_mentioned",
    ):
        if not clean_text(str(merged.get(key) or "")):
            merged[key] = _json_copy((cached_analysis or {}).get(key))

    for section in ("positives", "negatives", "technical_notes"):
        old_points = (cached_analysis or {}).get(section, []) or []
        new_points = (candidate_analysis or {}).get(section, []) or []
        # ترتیب عمدی است: cache تأییدشده اول می‌ماند، استخراج تازه فقط پوشش را کامل می‌کند.
        merged[section] = _dedupe_evidence_points(
            list(old_points) + list(new_points),
            EVIDENCE_CACHE_MAX_MERGED_POINTS_PER_SECTION,
        )

    # verdict یک نتیجه‌گیریِ کلی و تک‌آیتمی است، نه فهرستی که باید انباشته شود؛
    # استخراج تازه در اولویت است و فقط اگر خالی بود، verdict قدیمی نگه داشته می‌شود.
    merged["verdict"] = (
        (candidate_analysis or {}).get("verdict")
        or (cached_analysis or {}).get("verdict")
        or []
    )

    return merged


def _refresh_meta(
    quality_audit: dict,
    stored_meta: dict | None = None,
    *,
    legacy_low_quality_cache: bool = False,
) -> dict:
    """وضعیت تلاش‌های ناموفق را مستقل از خود شواهد نگه می‌دارد."""
    meta = _json_copy(stored_meta) if isinstance(stored_meta, dict) else {}

    try:
        failed_attempts = int(meta.get("failed_selective_refreshes", 0) or 0)
    except (TypeError, ValueError):
        failed_attempts = 0

    # Cacheهای V10 که low-quality هستند، یک کوشش ناموفق داشته‌اند ولی هنوز متادیتا نداشتند.
    if legacy_low_quality_cache and quality_audit.get("status") == "needs_review":
        failed_attempts = max(failed_attempts, 1)

    failed_attempts = min(max(failed_attempts, 0), 99)
    manual_review = bool(meta.get("manual_review", False))

    if quality_audit.get("status") != "needs_review":
        manual_review = False
        failed_attempts = 0

    if (
        quality_audit.get("status") == "needs_review"
        and failed_attempts >= EVIDENCE_CACHE_MAX_FAILED_SELECTIVE_REFRESHES
    ):
        manual_review = True

    return {
        "failed_selective_refreshes": failed_attempts,
        "max_failed_selective_refreshes": EVIDENCE_CACHE_MAX_FAILED_SELECTIVE_REFRESHES,
        "manual_review": manual_review,
        "manual_review_reason_fa": (
            "پس از چند بازخوانی ناموفق، این منبع برای بازبینی دستی نگه داشته شده است"
            if manual_review
            else ""
        ),
        "last_selective_refresh_at_utc": clean_text(
            str(meta.get("last_selective_refresh_at_utc") or "")
        ),
        "last_selective_refresh_outcome": clean_text(
            str(meta.get("last_selective_refresh_outcome") or "")
        ),
    }


def _after_selective_refresh(
    previous_meta: dict | None,
    quality_audit: dict,
    *,
    improved: bool,
) -> dict:
    meta = _refresh_meta(quality_audit, previous_meta)
    meta["last_selective_refresh_at_utc"] = _utc_now_iso()

    if improved:
        meta["failed_selective_refreshes"] = 0
        meta["manual_review"] = False
        meta["manual_review_reason_fa"] = ""
        meta["last_selective_refresh_outcome"] = "merged_improved"
        return meta

    meta["failed_selective_refreshes"] = int(
        meta.get("failed_selective_refreshes", 0) or 0
    ) + 1
    meta["last_selective_refresh_outcome"] = "retained_no_new_coverage"

    if (
        quality_audit.get("status") == "needs_review"
        and meta["failed_selective_refreshes"] >= EVIDENCE_CACHE_MAX_FAILED_SELECTIVE_REFRESHES
    ):
        meta["manual_review"] = True
        meta["manual_review_reason_fa"] = (
            "بازخوانی تازه پوشش شواهد را بهتر نکرد؛ ادامه‌ی تلاش خودکار متوقف شد"
        )

    return meta


def load_evidence_cache(game: str, platform: str, review_url: str) -> tuple[dict | None, dict]:
    """فقط cache هم‌نسخه و هم‌URL را برمی‌گرداند؛ داده‌ی قدیمی یا بی‌ربط رد می‌شود."""
    path = evidence_cache_path(game, platform, review_url)
    info = {"path": path, "status": "miss", "created_at_utc": ""}

    if not REVIEW_EVIDENCE_CACHE_ENABLED:
        info["status"] = "disabled"
        return None, info

    if not os.path.exists(path):
        return None, info

    try:
        with open(path, "r", encoding="utf-8") as file:
            cached = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        info["status"] = "invalid"
        info["reason"] = repr(exc)
        return None, info

    expected_url = canonical_cache_url(review_url)

    if (
        cached.get("cache_version") != EVIDENCE_CACHE_VERSION
        or clean_text(str(cached.get("game") or "")).casefold()
        != clean_text(game).casefold()
        or clean_text(str(cached.get("platform") or "")).casefold()
        != clean_text(platform).casefold()
        or canonical_cache_url(str(cached.get("requested_url") or "")) != expected_url
        or not isinstance(cached.get("analysis"), dict)
    ):
        info["status"] = "invalid"
        return None, info

    analysis = _json_copy(cached["analysis"])
    required = {"site_name", "title", "url", "positives", "negatives", "technical_notes"}

    if not required.issubset(analysis):
        info["status"] = "invalid"
        return None, info

    for key in ("positives", "negatives", "technical_notes"):
        if not isinstance(analysis.get(key), list):
            info["status"] = "invalid"
            return None, info

    # قواعد دسته‌بندی و پوشش ممکن است در نسخه‌های بعدی بهتر شوند.
    # بنابراین audit را از خودِ شواهد بازسازی می‌کنیم، نه از برچسب قدیمی cache.
    quality_audit = evidence_quality_audit(analysis)
    refresh_meta = _refresh_meta(
        quality_audit,
        cached.get("refresh_meta"),
        legacy_low_quality_cache=("refresh_meta" not in cached),
    )

    info.update(
        {
            "status": "hit",
            "created_at_utc": clean_text(str(cached.get("created_at_utc") or "")),
            "source_text_sha256": clean_text(str(cached.get("source_text_sha256") or "")),
            "quality_audit": quality_audit,
            "refresh_meta": refresh_meta,
        }
    )
    return analysis, info

def save_evidence_cache(
    game: str,
    platform: str,
    requested_url: str,
    page: dict,
    analysis: dict,
    refresh_meta: dict | None = None,
) -> dict:
    """فقط نتیجه‌ی نهاییِ تأییدشده را ذخیره می‌کند، نه HTML کامل نقد را."""
    path = evidence_cache_path(game, platform, requested_url)
    os.makedirs(EVIDENCE_CACHE_DIR, exist_ok=True)

    quality_audit = evidence_quality_audit(analysis)
    refresh_meta = _refresh_meta(quality_audit, refresh_meta)

    payload = {
        "cache_version": EVIDENCE_CACHE_VERSION,
        "created_at_utc": _utc_now_iso(),
        "game": clean_text(game),
        "platform": clean_text(platform),
        "requested_url": canonical_cache_url(requested_url),
        "final_url": canonical_cache_url(str(page.get("url") or requested_url)),
        "source_text_sha256": hashlib.sha256(
            clean_text(str(page.get("text") or "")).encode("utf-8")
        ).hexdigest(),
        "analysis": _json_copy(analysis),
        "quality_audit": quality_audit,
        "refresh_meta": refresh_meta,
    }

    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)

    return {
        "status": "saved",
        "path": path,
        "created_at_utc": payload["created_at_utc"],
        "source_text_sha256": payload["source_text_sha256"],
        "quality_audit": quality_audit,
        "refresh_meta": refresh_meta,
    }

def item_refresh_requested(item: dict) -> bool:
    """Refresh سراسری با ENV یا فقط برای یک بازی با YAML فعال می‌شود."""
    if REVIEW_REFRESH_EVIDENCE:
        return True

    value = item.get("refresh_evidence", item.get("force_refresh_evidence", False))

    if isinstance(value, bool):
        return value

    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def item_incomplete_refresh_requested(item: dict) -> bool:
    """فقط منابعی را بازخوانی می‌کند که cache آن‌ها از کنترل کیفیت عبور نکرده است."""
    if REVIEW_REFRESH_INCOMPLETE_EVIDENCE:
        return True

    value = item.get(
        "refresh_incomplete_evidence",
        item.get("refresh_low_quality_evidence", False),
    )

    if isinstance(value, bool):
        return value

    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _quality_reason_fa(total_points: int, categorized_points: int) -> list[str]:
    reasons = []

    if total_points == 0:
        reasons.append("هیچ شاهد تأییدشده‌ای ثبت نشده است")
    elif total_points < EVIDENCE_CACHE_MIN_VERIFIED_POINTS:
        reasons.append(
            f"فقط {total_points} شاهد تأییدشده دارد؛ حداقل "
            f"{EVIDENCE_CACHE_MIN_VERIFIED_POINTS} شاهد لازم است"
        )

    if total_points and categorized_points == 0:
        reasons.append("هیچ شاهدی برای کارت‌های ارزیابی قابل‌دسته‌بندی نیست")

    return reasons


def _coverage_label_fa(category_count: int) -> str:
    if category_count <= 0:
        return "بدون پوشش دسته‌ای"
    if category_count == 1:
        return "پوشش محدود"
    if category_count == 2:
        return "پوشش چندبخشی"
    return "پوشش گسترده"


def evidence_quality_audit(analysis: dict) -> dict:
    """کیفیت cache را با تعداد، قابل‌دسته‌بندی بودن و پوشش دسته‌های شواهد می‌سنجد."""
    section_counts = {}
    categorized_points = 0
    categories = set()

    for section in ("positives", "negatives", "technical_notes"):
        points = analysis.get(section, []) or []
        valid_points = [point for point in points if isinstance(point, dict)]
        section_counts[section] = len(valid_points)

        for point in valid_points:
            category, _ = classify_evidence_rule_based(
                {
                    "evidence_en": point.get("evidence_en", ""),
                    "section": section,
                }
            )
            if category:
                categorized_points += 1
                categories.add(category)

    total_points = sum(section_counts.values())
    category_keys = sorted(categories)
    reasons = _quality_reason_fa(total_points, categorized_points)
    status = "acceptable" if not reasons else "needs_review"

    # این عدد داخلی فقط برای مقایسه‌ی نسخه‌های همان منبع است.
    # پوشش دسته‌ای وزن مستقل دارد تا «چند جمله‌ی هم‌موضوع» کیفیت تلقی نشود.
    quality_rank = total_points * 100 + categorized_points * 20 + len(categories) * 25

    return {
        "status": status,
        "verified_point_count": total_points,
        "categorized_point_count": categorized_points,
        "category_count": len(categories),
        "category_keys": category_keys,
        "coverage_fa": _coverage_label_fa(len(categories)),
        "section_counts": section_counts,
        "reasons_fa": reasons,
        "quality_rank": quality_rank,
        "action_fa": (
            "بازخوانی انتخابی توصیه می‌شود"
            if status == "needs_review"
            else "برای استفاده‌ی پایدار قابل‌قبول است"
        ),
    }


def should_replace_cached_analysis(cached_analysis: dict, candidate_analysis: dict) -> bool:
    """استخراج تازه فقط وقتی کاربرد دارد که پس از ادغام، پوشش واقعاً بهتر شود."""
    old_audit = evidence_quality_audit(cached_analysis)
    merged_audit = evidence_quality_audit(
        merge_cached_and_candidate_analysis(cached_analysis, candidate_analysis)
    )
    return merged_audit["quality_rank"] > old_audit["quality_rank"]

def attach_cache_metadata(analysis: dict, info: dict) -> dict:
    output = _json_copy(analysis)
    quality = info.get("quality_audit")
    if not isinstance(quality, dict):
        quality = evidence_quality_audit(output)
    refresh_meta = _refresh_meta(
        quality,
        info.get("refresh_meta"),
    )

    output["evidence_cache"] = {
        "status": info.get("status", "miss"),
        "created_at_utc": info.get("created_at_utc", ""),
        "source_text_sha256": info.get("source_text_sha256", ""),
        "quality_audit": quality,
        "refresh_meta": refresh_meta,
    }
    return output


def attach_runtime_evidence(
    analysis: dict,
    cache_info: dict,
    game: str,
    platform: str,
    requested_url: str,
) -> dict:
    """
    cache را دست‌نخورده نگه می‌دارد و فقط برای dossier فعلی، شواهد دستی
    تأییدشده را روی آن می‌نشاند.
    """
    output = attach_cache_metadata(analysis, cache_info)
    output, manual_meta = apply_manual_evidence_overlay(
        output,
        game,
        platform,
        requested_url,
    )

    # audit خروجی می‌تواند با overlay دستی از audit خود cache متفاوت باشد.
    output["runtime_quality_audit"] = evidence_quality_audit(output)

    if manual_meta.get("status") == "applied":
        print(
            f"Manual evidence: APPLIED | {output.get('site_name', 'Unknown Source')} "
            f"| +{manual_meta.get('points_added', 0)} verified editor point(s)"
        )
    elif manual_meta.get("status") == "invalid":
        print(
            "Manual evidence: INVALID YAML | "
            f"{manual_meta.get('error') or 'file ignored'}"
        )

    return output

def run_evidence_cache_regression_checks():
    assert canonical_cache_url(
        "HTTPS://WWW.Example.COM/review/game/?utm_source=test"
    ) == "https://www.example.com/review/game"
    assert canonical_cache_url(
        "https://www.example.com/review/game/"
    ) == "https://www.example.com/review/game"
    assert item_refresh_requested({"refresh_evidence": True}) or REVIEW_REFRESH_EVIDENCE
    assert not (
        item_refresh_requested({"refresh_evidence": "false"})
        and not REVIEW_REFRESH_EVIDENCE
    )

    empty = {"positives": [], "negatives": [], "technical_notes": []}
    thin = {
        "positives": [{"point_fa": "پازل‌ها دشوارند.", "evidence_en": "Many puzzles are almost impossible to solve without help."}],
        "negatives": [],
        "technical_notes": [],
    }
    acceptable = {
        "positives": [
            {"point_fa": "مکانیک‌ها متنوع‌اند.", "evidence_en": "The game is stuffed with a huge variety of mechanics and systems."},
            {"point_fa": "کنترل اسب خوب است.", "evidence_en": "The horse controls very well across the open world."},
        ],
        "negatives": [],
        "technical_notes": [],
    }
    supplemental = {
        "positives": [
            {"point_fa": "مکانیک‌ها متنوع‌اند.", "evidence_en": "The game is stuffed with a huge variety of mechanics and systems."},
        ],
        "negatives": [
            {"point_fa": "باس‌فایت‌ها سخت‌اند.", "evidence_en": "Boss battles can be brutal and occasionally feel a little unfair."},
        ],
        "technical_notes": [],
    }

    assert evidence_quality_audit(empty)["status"] == "needs_review"
    assert evidence_quality_audit(thin)["status"] == "needs_review"
    assert evidence_quality_audit(acceptable)["status"] == "acceptable"
    merged = merge_cached_and_candidate_analysis(acceptable, supplemental)
    assert len(merged["positives"]) == 2, "نقل‌قول تکراری نباید دوباره اضافه شود"
    assert len(merged["negatives"]) == 1, "شاهد تازه باید به cache افزوده شود"
    assert should_replace_cached_analysis(thin, acceptable)
    assert not should_replace_cached_analysis(acceptable, {"positives": [], "negatives": [], "technical_notes": []})

    legacy_meta = _refresh_meta(
        evidence_quality_audit(thin),
        None,
        legacy_low_quality_cache=True,
    )
    assert legacy_meta["failed_selective_refreshes"] == 1
    capped_meta = _after_selective_refresh(
        legacy_meta,
        evidence_quality_audit(thin),
        improved=False,
    )
    assert capped_meta["manual_review"], "منبع کم‌کیفیت نباید بی‌نهایت refresh شود"

    multi_category = {
        "positives": [
            {
                "point_fa": "کنترل‌ها روان‌اند.",
                "evidence_en": "The horse controls very well across the open world and makes traversal a pleasure.",
            }
        ],
        "negatives": [],
        "technical_notes": [
            {
                "point_fa": "افت عملکرد دیده شده است.",
                "evidence_en": "I did notice a significant dip in performance in the last few days of the review window.",
            }
        ],
    }
    assert evidence_quality_audit(multi_category)["coverage_fa"] == "پوشش چندبخشی"

    manual_entry = {
        "reviewed": True,
        "positives": [
            {
                "point_fa": "گیم‌پلی توانایی حمل کل تجربه را دارد.",
                "evidence_en": "The gameplay is more than enough to carry the entire experience from start to finish.",
            }
        ],
        "negatives": [],
        "technical_notes": [],
    }
    manually_merged, manual_meta = _apply_manual_evidence_entries(
        {"positives": [], "negatives": [], "technical_notes": []},
        [manual_entry],
    )
    assert manual_meta["status"] == "applied"
    assert len(manually_merged["positives"]) == 1

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


# دامنه‌های معتبری که Poormaz برای نقد بازی ترجیح می‌دهد. این نگاشت فقط برای
# یکدست‌سازی نام سایت (به‌جای og:site_name نامنظم) و برای گزارش پوشش منابع
# معتبر استفاده می‌شود؛ هرگز برای فیلتر یا رد کردن یک URL به کار نمی‌رود،
# چون صف نقدها همچنان دستی و توسط سردبیر پر می‌شود.
REPUTABLE_SITE_DOMAINS = {
    "ign.com": "IGN",
    "gamespot.com": "GameSpot",
    "pcgamer.com": "PC Gamer",
    "wccftech.com": "Wccftech",
    "eurogamer.net": "Eurogamer",
    "gameinformer.com": "Game Informer",
    "vg247.com": "VG247",
    "gamesradar.com": "GamesRadar+",
    "destructoid.com": "Destructoid",
    "rockpapershotgun.com": "Rock Paper Shotgun",
    "polygon.com": "Polygon",
    "kotaku.com": "Kotaku",
    "pcgamesn.com": "PCGamesN",
    "thegamer.com": "TheGamer",
    "videogamer.com": "VideoGamer",
    "gamingbolt.com": "GamingBolt",
    "pushsquare.com": "Push Square",
    "nintendolife.com": "Nintendo Life",
    "purexbox.com": "Pure Xbox",
    "gamerant.com": "Game Rant",
    "dualshockers.com": "DualShockers",
    "gamesindustry.biz": "GamesIndustry.biz",
    "gameskinny.com": "GameSkinny",
    "digitaltrends.com": "Digital Trends",
    "pcinvasion.com": "PC Invasion",
    "godisageek.com": "GodisaGeek",
    "screenrant.com": "ScreenRant",
    "shacknews.com": "Shacknews",
    "gamewatcher.com": "GameWatcher",
    "rpgsite.net": "RPG Site",
    "hardcoregamer.com": "Hardcore Gamer",
    "gamespew.com": "GameSpew",
    "gamerevolution.com": "GameRevolution",
    "trueachievements.com": "TrueAchievements",
    "purenintendo.com": "Pure Nintendo",
    "metro.co.uk": "Metro GameCentral",
}

# فهرست هسته‌ای که کاربر صراحتاً برای پوشش نقد خواسته است؛ فقط برای گزارش
# «چه منابعی هنوز کم است» استفاده می‌شود، نه یک شرط سخت‌گیرانه برای رد کردن پرونده.
RECOMMENDED_CORE_SITES = [
    "IGN",
    "GameSpot",
    "PC Gamer",
    "Wccftech",
    "Eurogamer",
    "Game Informer",
    "VG247",
    "GamesRadar+",
    "Destructoid",
]


def canonical_reputable_site_name(url: str) -> str | None:
    """اگر دامنه‌ی URL یکی از منابع شناخته‌شده باشد، نام یکدست انتشاراتی را برمی‌گرداند."""
    host = (urlparse(url).hostname or "").lower()
    host = re.sub(r"^www\.", "", host)

    for domain, name in REPUTABLE_SITE_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return name

    return None


def reputable_coverage_report(review_sources: list[dict]) -> dict:
    """
    گزارشی صرفاً راهنما درباره‌ی پوشش سایت‌های معتبر هسته‌ای. این گزارش هرگز
    پرونده را رد نمی‌کند و وارد متن عمومی مقاله نمی‌شود؛ فقط برای تصمیم
    تحریریه‌ی سردبیر در کنسول و JSON پرونده چاپ می‌شود.
    """
    present_names = {
        clean_text(str(source.get("site_name") or ""))
        for source in review_sources
    }

    covered = [name for name in RECOMMENDED_CORE_SITES if name in present_names]
    missing = [name for name in RECOMMENDED_CORE_SITES if name not in present_names]
    other_sources = sorted(
        name for name in present_names
        if name and name not in RECOMMENDED_CORE_SITES
    )

    if missing:
        note_fa = (
            "برای پوشش قوی‌تر، افزودن نقد از " + "، ".join(missing) +
            " پیشنهاد می‌شود (در صورت وجود نقد منتشرشده از آن‌ها)."
        )
    else:
        note_fa = "پوشش منابع معتبر هسته‌ای در این پرونده کامل است."

    return {
        "covered_core_sites": covered,
        "missing_core_sites": missing,
        "other_sources": other_sources,
        "note_fa": note_fa,
    }


class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title_parts = []
        self.in_title = False
        self.meta = {}
        self.meta_items = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs_dict = {str(k).lower(): (v or "") for k, v in attrs}

        if tag == "title":
            self.in_title = True

        if tag == "meta":
            self.meta_items.append(attrs_dict)

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
        "script",
        "style",
        "noscript",
        "svg",
        "iframe",
        "canvas",
        "template",
        "form",
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

    article_matches = re.findall(r"(?is)<article\b[^>]*>(.*?)</article>", html)
    if article_matches:
        return max(article_matches, key=len)

    main_matches = re.findall(r"(?is)<main\b[^>]*>(.*?)</main>", html)
    if main_matches:
        return max(main_matches, key=len)

    return html


def html_to_text(html: str) -> str:
    parser = VisibleTextParser()

    try:
        parser.feed(html or "")
    except Exception:
        pass

    return clean_text(" ".join(parser.parts))


def extract_page_info(url: str) -> dict:
    try:
        response = SESSION.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
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
        canonical_reputable_site_name(response.url)
        or meta_parser.meta.get("og:site_name")
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
        "score_text": page_text,
        "text_chars": len(page_text),
        "html": html,
        "meta_items": meta_parser.meta_items,
    }


# وضعیت عرضه‌ی بازی (منتشرشده، دسترسی زودهنگام، بازسازی، DLC و غیره) یک فیلد
# اختیاری در صف است. اگر کاربر آن را ننویسد، پیش‌فرض «منتشرشده» در نظر گرفته
# می‌شود؛ اگر مقدار ناشناخته‌ای بنویسد، همان متن به‌عنوان برچسب نگه داشته
# می‌شود تا هیچ پرونده‌ای فقط به‌خاطر یک مقدار غیرمنتظره رد نشود.
GAME_STATUS_LABELS = {
    "released": "منتشرشده",
    "early_access": "دسترسی زودهنگام (Early Access)",
    "remaster": "بازسازی (Remaster)",
    "remake": "ریمیک (Remake)",
    "definitive_edition": "نسخه‌ی نهایی (Definitive Edition)",
    "goty_edition": "نسخه‌ی ویژه‌ی بازی سال (GOTY Edition)",
    "dlc": "بسته الحاقی (DLC)",
    "expansion": "بسته گسترش‌دهنده (Expansion)",
    "port": "پورت روی پلتفرم جدید",
}

GAME_STATUS_ALIASES = {
    "released": "released",
    "release": "released",
    "full release": "released",
    "1.0": "released",
    "early access": "early_access",
    "early-access": "early_access",
    "earlyaccess": "early_access",
    "ea": "early_access",
    "remaster": "remaster",
    "remastered": "remaster",
    "remake": "remake",
    "definitive edition": "definitive_edition",
    "definitive": "definitive_edition",
    "goty": "goty_edition",
    "goty edition": "goty_edition",
    "game of the year edition": "goty_edition",
    "dlc": "dlc",
    "downloadable content": "dlc",
    "expansion": "expansion",
    "expansion pack": "expansion",
    "port": "port",
    "console port": "port",
}


def normalize_game_status(raw) -> dict:
    """
    مقدار خام game_status را به یک کلید و برچسب فارسی استاندارد تبدیل می‌کند.
    خالی بودن یا ناشناخته بودن مقدار هرگز باعث رد شدن آیتم نمی‌شود.
    """
    text = clean_text(str(raw or "")).strip()

    if not text:
        return {
            "key": "released",
            "label_fa": GAME_STATUS_LABELS["released"],
            "raw": "",
            "recognized": True,
        }

    key = GAME_STATUS_ALIASES.get(text.casefold())

    if key:
        return {
            "key": key,
            "label_fa": GAME_STATUS_LABELS[key],
            "raw": text,
            "recognized": True,
        }

    return {
        "key": "custom",
        "label_fa": text,
        "raw": text,
        "recognized": False,
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
            f"حداقل {REVIEW_MIN_SOURCES} لینک نقد لازم است (هدف تحریریه ۵ تا "
            f"{REVIEW_RECOMMENDED_MAX_SOURCES} نقد است)، ولی فقط "
            f"{len(review_urls)} لینک وارد شده."
        )
    elif len(review_urls) > REVIEW_RECOMMENDED_MAX_SOURCES:
        print(
            f"Note: item #{index} has {len(review_urls)} review_urls, above the "
            f"recommended {REVIEW_RECOMMENDED_MAX_SOURCES}. This is not an error, "
            "just a heads-up in case some links were added by mistake."
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


def ask_openai_json(
    client: OpenAI,
    prompt: str,
    max_tokens: int = 1200,
) -> dict:
    last_error = None

    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                temperature=OPENAI_TEMPERATURE,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Return exactly one valid JSON object. "
                            "Never invent facts, scores, performance claims, or quotations."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            )

            return parse_json_response(
                response.choices[0].message.content or ""
            )

        except Exception as exc:
            last_error = exc
            print(f"OpenAI attempt {attempt + 1} failed: {repr(exc)}")

    raise RuntimeError(f"OpenAI failed twice: {repr(last_error)}")


def as_score_100(value) -> int | None:
    try:
        score = int(float(value))
    except (TypeError, ValueError):
        return None

    return score if 0 <= score <= 100 else None


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


def _score_to_10(raw_value, raw_best=None, raw_worst=None) -> float | None:
    try:
        value = float(str(raw_value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None

    try:
        best = (
            float(str(raw_best).replace(",", "").strip())
            if raw_best is not None
            else None
        )
    except (TypeError, ValueError):
        best = None

    try:
        worst = (
            float(str(raw_worst).replace(",", "").strip())
            if raw_worst is not None
            else 0.0
        )
    except (TypeError, ValueError):
        worst = 0.0

    if best is not None and best > worst:
        score = ((value - worst) / (best - worst)) * 10
    elif 0 <= value <= 10:
        score = value
    elif 0 <= value <= 100:
        score = value / 10
    else:
        return None

    return round(score, 1) if 0 <= score <= 10 else None


def _display_score(raw_value, raw_best=None) -> str:
    raw = clean_text(str(raw_value))
    best = clean_text(str(raw_best)) if raw_best is not None else ""

    if not raw:
        return ""

    if best and best not in {"0", "0.0"}:
        return f"{raw}/{best}"

    try:
        value = float(raw)
    except ValueError:
        return raw

    return f"{raw}/10" if value <= 10 else f"{raw}/100"


def _compact_evidence(value: str, max_len: int = 240) -> str:
    return clean_text(value)[:max_len].rstrip()


def _walk_json(value):
    if isinstance(value, dict):
        yield value

        for child in value.values():
            yield from _walk_json(child)

    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _extract_json_ld_objects(html: str) -> list[object]:
    objects = []

    for match in re.finditer(
        r'(?is)<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html or "",
    ):
        raw = unescape(match.group(1) or "").strip()

        if not raw:
            continue

        try:
            objects.append(json.loads(raw))
        except json.JSONDecodeError:
            continue

    return objects


def extract_review_score(page: dict) -> dict:
    """
    فقط نمره‌هایی را قبول می‌کند که از JSON-LD، meta معتبر،
    یا عبارت صریح x/10، x/5 یا x/100 کنار برچسب نمره آمده باشند.
    عددهای تنها مثل «30 hours» عمداً نادیده گرفته می‌شوند.
    """
    candidates = []
    html = page.get("html", "") or ""
    visible_text = clean_text(page.get("score_text", "") or "")

    def add_candidate(
        raw_value,
        raw_best=None,
        raw_worst=None,
        method="unknown",
        confidence=0,
        evidence="",
    ):
        raw_value = str(raw_value or "").strip()
        raw_best = str(raw_best or "").strip() or None

        fraction = re.fullmatch(
            r"(\d{1,3}(?:\.\d+)?)\s*/\s*(\d{1,3}(?:\.\d+)?)",
            raw_value,
        )

        if fraction and raw_best is None:
            raw_value = fraction.group(1)
            raw_best = fraction.group(2)

        normalized = _score_to_10(raw_value, raw_best, raw_worst)

        if normalized is None:
            return

        evidence_clean = _compact_evidence(evidence).lower()

        blocked_words = (
            "user score",
            "user rating",
            "community rating",
            "reader rating",
            "audience score",
            "metascore",
            "hours",
            "hour",
        )

        if any(word in evidence_clean for word in blocked_words):
            return

        candidates.append(
            {
                "original_score": _display_score(raw_value, raw_best),
                "review_score_10": normalized,
                "score_method": method,
                "score_confidence": confidence,
                "score_evidence": _compact_evidence(evidence),
            }
        )

    # 0) قالب اختصاصی Wccftech، مانند «Gaming 9.0».
    # این سایت معمولاً نمره را در هدر متن می‌گذارد و JSON-LD ندارد.
    host = (urlparse(page.get("url", "")).hostname or "").lower()

    if host.endswith("wccftech.com"):
        header_text = visible_text[:2500]

        wccftech_patterns = [
            r"(?is)\bGaming\s+(?:Score\s*)?(\d(?:\.\d+)?)\b",
            r"(?is)\bScore\s*:?\s*(\d(?:\.\d+)?)\b.{0,120}?\bGaming\b",
        ]

        for pattern in wccftech_patterns:
            header_match = re.search(pattern, header_text)

            if not header_match:
                continue

            evidence = header_text[
                max(0, header_match.start() - 80): header_match.end() + 120
            ]

            add_candidate(
                header_match.group(1),
                "10",
                method="wccftech_header_score",
                confidence=98,
                evidence=evidence,
            )
            break

    # 1) JSON-LD: معتبرترین منبع برای نمره‌ی همان نقد
    for obj in _extract_json_ld_objects(html):
        for node in _walk_json(obj):
            node_type = node.get("@type", "")
            node_types = node_type if isinstance(node_type, list) else [node_type]
            node_types = [str(item).lower() for item in node_types]

            if "review" not in node_types:
                continue

            review_rating = node.get("reviewRating")

            if isinstance(review_rating, dict):
                add_candidate(
                    review_rating.get("ratingValue"),
                    review_rating.get("bestRating"),
                    review_rating.get("worstRating"),
                    method="jsonld_reviewRating",
                    confidence=100,
                    evidence=json.dumps(
                        {
                            "@type": node.get("@type"),
                            "reviewRating": review_rating,
                        },
                        ensure_ascii=False,
                    ),
                )

            elif node.get("ratingValue") is not None:
                add_candidate(
                    node.get("ratingValue"),
                    node.get("bestRating"),
                    node.get("worstRating"),
                    method="jsonld_review_node",
                    confidence=95,
                    evidence=json.dumps(node, ensure_ascii=False)[:400],
                )

    # 2) Meta tagهای مشخصاً مربوط به امتیاز نقد
    for attrs in page.get("meta_items", []) or []:
        key = (
            attrs.get("property", "")
            or attrs.get("name", "")
            or attrs.get("itemprop", "")
        ).lower().strip()

        value = attrs.get("content", "").strip()

        if not key or not value:
            continue

        if "user" in key or "community" in key or "audience" in key:
            continue

        allowed_keys = (
            "review:rating",
            "review_rating",
            "reviewrating",
            "ratingvalue",
        )

        if not any(token in key for token in allowed_keys):
            continue

        best = (
            attrs.get("best-rating")
            or attrs.get("bestrating")
            or attrs.get("rating-scale")
            or attrs.get("scale")
        )

        add_candidate(
            value,
            best,
            method="meta_review_rating",
            confidence=90,
            evidence=f"{key}: {value}" + (f" / {best}" if best else ""),
        )

    # 3) فقط نمره‌های دارای کسر صریح، مثل 8/10
    visible_patterns = [
        r"(?is)\b(?:review\s*score|final\s*score|rating|verdict)\b"
        r"[^0-9]{0,50}(\d{1,3}(?:\.\d+)?)\s*(?:/|out\s+of)\s*(10|5|100)\b",

        r'(?is)<(?:span|div|p)[^>]+(?:class|data-testid)=["\'][^"\']*'
        r'(?:review[-_ ]?score|rating|score)[^"\']*["\'][^>]*>'
        r"\s*(\d{1,3}(?:\.\d+)?)\s*(?:/|out\s+of)\s*(10|5|100)\b",
    ]

    for pattern_index, pattern in enumerate(visible_patterns):
        haystack = visible_text if pattern_index == 0 else html

        for match in re.finditer(pattern, haystack):
            evidence = haystack[max(0, match.start() - 90): match.end() + 90]

            add_candidate(
                match.group(1),
                match.group(2),
                method=(
                    "visible_labelled_fraction"
                    if pattern_index == 0
                    else "html_score_element"
                ),
                confidence=82 if pattern_index == 0 else 86,
                evidence=evidence,
            )

    if not candidates:
        return {
            "original_score": None,
            "review_score_10": None,
            "score_method": "not_found",
            "score_confidence": 0,
            "score_evidence": "",
        }

    candidates.sort(
        key=lambda item: (
            item["score_confidence"],
            item["review_score_10"],
        ),
        reverse=True,
    )

    return candidates[0]

def extract_metacritic_data(page: dict, requested_platform: str) -> dict:
    """
    نمره‌ی متاکریتیک را فقط از ساختارهای اختصاصی Critic Score Summary
    یا عبارت مستقیم Metascore می‌گیرد، نه از نمره‌ی منتقدهای داخل صفحه.
    """
    html = page.get("html", "") or ""
    visible_text = clean_text(page.get("score_text", "") or "")
    description = clean_text(page.get("description", "") or "")

    score_candidates = []
    count_candidates = []

    def add_score(score, method, confidence, evidence):
        score = as_score_100(score)

        if score is None:
            return

        score_candidates.append(
            {
                "score": score,
                "method": method,
                "confidence": confidence,
                "evidence": _compact_evidence(evidence),
            }
        )

    def add_count(value, method, confidence, evidence):
        try:
            count = int(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return

        if count < 1 or count > 10000:
            return

        count_candidates.append(
            {
                "count": count,
                "method": method,
                "confidence": confidence,
                "evidence": _compact_evidence(evidence),
            }
        )
        
    # 0) ساختار متنی رایج صفحه Metacritic:
    # Metascore -> Based on N Critic Reviews -> score
    for match in re.finditer(
        r"(?is)\bmetascore\b.{0,450}?"
        r"\bbased\s+on\s+(\d{1,5})\s+critic\s+reviews?\b"
        r".{0,180}?\b(\d{1,3})\b",
        visible_text,
    ):
        add_count(
            match.group(1),
            "metacritic_visible_summary",
            99,
            match.group(0),
        )
        add_score(
            match.group(2),
            "metacritic_visible_summary",
            99,
            match.group(0),
        )
        
    # 1) JSON / Next.js data مخصوص خلاصه‌ی امتیاز منتقدها
    summary_pattern = re.compile(
        r'(?is)"(?:criticScoreSummary|metascoreSummary)"\s*:\s*\{'
    )

    for match in summary_pattern.finditer(html):
        window = html[match.end(): match.end() + 1200]
        end = window.find("}")

        if end != -1:
            window = window[:end + 1]

        score_match = re.search(
            r'(?is)"(?:score|metascore)"\s*:\s*"?(\d{1,3})"?',
            window,
        )

        count_match = re.search(
            r'(?is)"(?:count|reviewCount|criticReviewCount)"\s*:\s*"?(\d{1,5})"?',
            window,
        )

        if score_match:
            add_score(
                score_match.group(1),
                "critic_score_summary",
                100,
                window,
            )

        if count_match:
            add_count(
                count_match.group(1),
                "critic_score_summary",
                100,
                window,
            )

    # 2) توضیح متا، اگر خود سایت صریحاً Metascore را نوشته باشد
    for source_name, text in (
        ("meta_description", description),
        ("visible_heading", visible_text[:30000]),
    ):
        score_match = re.search(
            r"(?is)\bmetascore\b\s*(?:of|:|-)?\s*(\d{1,3})\b",
            text,
        )

        if score_match:
            add_score(
                score_match.group(1),
                source_name,
                88 if source_name == "meta_description" else 80,
                text[
                    max(0, score_match.start() - 80):
                    score_match.end() + 100
                ],
            )

        count_match = re.search(
            r"(?is)\b(?:based\s+on\s+)?(\d{1,5})\s+critic\s+reviews?\b",
            text,
        )

        if count_match:
            add_count(
                count_match.group(1),
                source_name,
                85 if source_name == "meta_description" else 76,
                text[
                    max(0, count_match.start() - 80):
                    count_match.end() + 100
                ],
            )

    score_candidates.sort(
        key=lambda item: item["confidence"],
        reverse=True,
    )

    count_candidates.sort(
        key=lambda item: item["confidence"],
        reverse=True,
    )

    best_score = score_candidates[0] if score_candidates else None
    best_count = count_candidates[0] if count_candidates else None

    if best_score:
        note = (
            "نمره متاکریتیک از ساختار اختصاصی صفحه یا عبارت مستقیم Metascore استخراج شده است."
        )
    else:
        note = "نمره متاکریتیک به‌صورت قطعی در HTML صفحه پیدا نشد."

    return {
        "metascore_100": best_score["score"] if best_score else None,
        "critic_review_count": best_count["count"] if best_count else None,
        "platform_found": requested_platform,
        "confidence_note_fa": note,
        "metascore_method": best_score["method"] if best_score else "not_found",
        "metascore_evidence": best_score["evidence"] if best_score else "",
        "url": page.get("url", ""),
    }


def verified_points(items, source_text: str, max_items: int = 4) -> list[dict]:
    """
    فقط نکاتی را نگه می‌دارد که شاهد انگلیسی‌شان واقعاً در متن نقد باشد
    و طول شاهد هم برای تأیید یک ادعا کافی باشد.
    """
    if not isinstance(items, list):
        return []

    normalized_source = clean_text(source_text).casefold()
    output = []

    for item in items:
        if not isinstance(item, dict):
            continue

        point_fa = clean_text(str(item.get("point_fa") or ""))
        evidence_en = clean_text(str(item.get("evidence_en") or ""))

        if len(point_fa) < 3 or len(evidence_en) < 8:
            continue

        word_count = len(re.findall(r"\b[\w'-]+\b", evidence_en))

        if word_count < 8 or word_count > 22:
            continue

        if evidence_en.casefold() not in normalized_source:
            continue

        output.append(
            {
                "point_fa": point_fa,
                "evidence_en": evidence_en,
            }
        )

        if len(output) >= max_items:
            break

    return output


def analyze_review_source(
    client: OpenAI,
    game: str,
    platform: str,
    page: dict,
    recovery_mode: bool = False,
) -> dict:
    score = extract_review_score(page)

    prompt = f"""
You are extracting only evidence-based editorial themes from one professional game review.

Game: {game}
Requested platform: {platform}
Website: {page["site_name"]}
Review title: {page["title"]}
Review URL: {page["url"]}

The score was extracted deterministically before this request:
- original_score: {score["original_score"]}
- review_score_10: {score["review_score_10"]}
- score_method: {score["score_method"]}

Rules:
- Do NOT change, infer, or discuss the score.
- Use only the supplied page text.
- Do not invent details, including performance, bugs, hardware results,
  story specifics, localization issues, or technical results.
- Every returned point MUST include:
  1) point_fa: a concise Persian paraphrase
  2) evidence_en: an exact short English quote from the supplied page text
- point_fa must preserve the precise meaning and scope of evidence_en.
  Do not broaden the claim or add a cause, feature, or conclusion not present in the quote.
- evidence_en must be between 8 and 22 English words.
- Do not use quotes longer than 22 words.
- If there is no direct evidence for a claim, omit it.
- technical_notes must be empty unless the review explicitly discusses
  performance, bugs, optimization, controls, UI, or technical problems.
- Never use information from your own knowledge.
- Aim to capture up to 4 distinct, high-value evidence points when the review contains them.
- Do not repeat the same theme in different wording.
- positives: array of objects with point_fa and evidence_en
- negatives: array of objects with point_fa and evidence_en
- technical_notes: array of objects with point_fa and evidence_en
- verdict: a single object with point_fa and evidence_en representing the
  reviewer's own bottom-line conclusion/verdict sentence about the game as a
  whole (not a specific pro or con), or null if the review has no clear
  concluding verdict sentence. Same evidence_en length rule (8-22 words) applies.
- platform_mentioned: string or null

Extraction mode: {"coverage recovery: inspect the whole supplied review carefully because the prior cache was too thin" if recovery_mode else "normal"}

Page text:
{page["text"]}
""".strip()

    raw = ask_openai_json(client, prompt)

    raw_verdict = raw.get("verdict")
    verdict_candidates = [raw_verdict] if isinstance(raw_verdict, dict) else []

    return {
        "site_name": page["site_name"],
        "title": page["title"],
        "url": page["url"],
        **score,
        "positives": verified_points(raw.get("positives"), page["text"], 4),
        "negatives": verified_points(raw.get("negatives"), page["text"], 4),
        "technical_notes": verified_points(
            raw.get("technical_notes"),
            page["text"],
            3,
        ),
        "verdict": verified_points(verdict_candidates, page["text"], 1),
        "platform_mentioned": clean_text(
            str(raw.get("platform_mentioned") or "")
        ) or None,
    }


def as_score_10(value) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None

    if 0 <= score <= 10:
        return round(score, 1)

    return None


def build_evidence_index(review_sources: list[dict]) -> dict[str, dict]:
    """
    شناسه‌های یکتا برای نکات تأییدشده می‌سازد تا امتیازهای دسته‌ای
    تنها به شواهد واقعی همین پرونده وصل شوند.
    """
    evidence_index = {}

    for source_index, source in enumerate(review_sources, start=1):
        for section in ("positives", "negatives", "technical_notes"):
            for point_index, point in enumerate(
                source.get(section, []) or [],
                start=1,
            ):
                if not isinstance(point, dict):
                    continue

                point_fa = clean_text(str(point.get("point_fa") or ""))
                evidence_en = clean_text(str(point.get("evidence_en") or ""))

                if not point_fa or not evidence_en:
                    continue

                ref_id = f"S{source_index}:{section}:{point_index}"

                evidence_index[ref_id] = {
                    "site_name": source.get("site_name", "Unknown Source"),
                    "section": section,
                    "point_fa": point_fa,
                    "evidence_en": evidence_en,
                }

    return evidence_index


def calculate_overall_score(dossier: dict) -> dict:
    """
    امتیاز کلی را شفاف و قطعی می‌سازد:
    70٪ متاکریتیک + 30٪ میانگین نمره‌ی نقدهای انتخاب‌شده.
    سپس به بازه‌ی مجازِ نزدیک به متاکریتیک محدود می‌شود.
    """
    metacritic_raw = (
        dossier.get("metacritic", {}) or {}
    ).get("metascore_100")

    metacritic_score = None

    try:
        if metacritic_raw is not None:
            metacritic_score = round(float(metacritic_raw) / 10, 1)
    except (TypeError, ValueError):
        metacritic_score = None

    source_scores = []

    for source in dossier.get("review_sources", []) or []:
        score = as_score_10(source.get("review_score_10"))

        if score is not None:
            source_scores.append(score)

    source_average = (
        round(sum(source_scores) / len(source_scores), 1)
        if source_scores
        else None
    )

    if metacritic_score is not None and source_average is not None:
        raw_score = (
            REVIEW_METACRITIC_WEIGHT * metacritic_score
            + (1 - REVIEW_METACRITIC_WEIGHT) * source_average
        )

        lower = max(0.0, metacritic_score - REVIEW_SCORE_MAX_DELTA)
        upper = min(10.0, metacritic_score + REVIEW_SCORE_MAX_DELTA)
        overall_score = round(min(max(raw_score, lower), upper), 1)

        formula_fa = (
            "امتیاز کلی با فرمول ۷۰٪ نمره متاکریتیک و ۳۰٪ میانگین "
            "نقدهای انتخاب‌شده محاسبه شده و برای فاصله نگرفتن از "
            "اجماع منتقدان محدود شده است."
        )
    elif metacritic_score is not None:
        overall_score = metacritic_score
        formula_fa = (
            "به‌دلیل نبود نمره معتبر از نقدهای انتخاب‌شده، "
            "امتیاز کلی برابر با متاکریتیک است."
        )
    elif source_average is not None:
        overall_score = source_average
        formula_fa = (
            "به‌دلیل نبود متاکریتیک، امتیاز کلی از میانگین "
            "نقدهای انتخاب‌شده به دست آمده است."
        )
    else:
        overall_score = None
        formula_fa = "داده‌ی عددی کافی برای محاسبه امتیاز کلی وجود ندارد."

    return {
        "overall_score_10": overall_score,
        "metacritic_score_10": metacritic_score,
        "selected_review_average_10": source_average,
        "selected_review_scores": source_scores,
        "formula_fa": formula_fa,
    }



def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


CATEGORY_PATTERNS = {
    "technical": (
        r"\bframe\s*rate\b",
        r"\bframerate\b",
        r"\bperformance\b",
        r"\boptimization\b",
        r"\boptimisation\b",
        r"\bstutter(?:ing)?\b",
        r"\bcrash(?:es|ing)?\b",
        r"\bbug(?:s)?\b",
        r"\bglitch(?:es)?\b",
        r"\bstability\b",
        r"\bstable\b",
        r"\bunstable\b",
        r"\btechnical\b",
        r"\bpolish\b",
        r"\bloading\b",
        r"\binput lag\b",
        r"\buser interface\b",
        r"\bUI\b",
    ),
    "visuals": (
        r"\bgraphics?\b",
        r"\bvisuals?\b",
        r"\bart direction\b",
        r"\blighting\b",
        r"\banimation(?:s)?\b",
        r"\btexture(?:s)?\b",
        r"\bimage quality\b",
        r"\bresolution\b",
        r"\bdraw distance\b",
        r"\bvisual presentation\b",
    ),
    "audio": (
        r"\bvoice acting\b",
        r"\bvoice[- ]over(?:s)?\b",
        r"\bvoiceover(?:s)?\b",
        r"\bvocal performance(?:s)?\b",
        r"\bvoice cast\b",
        r"\bvoice direction\b",
        r"\bdubbing\b",
        r"\bdubbed\b",
        r"\bsound design\b",
        r"\bsound effects\b",
        r"\bsoundtrack\b",
        r"\bmusical score\b",
        r"\bcomposer\b",
        r"\baudio (?:design|mix|quality|presentation)\b",
    ),
    "value": (
        # «worth» به‌تنهایی الگوی ارزش خرید نیست. در عبارت‌هایی مثل
        # "for what it's worth" فقط یک اصطلاح محاوره‌ای است.
        r"\bprice\b",
        r"\bcost\b",
        r"\bvalue for money\b",
        r"\bgood value\b",
        r"\bpoor value\b",
        r"\bworth (?:the |its |your )?(?:price|cost|money|purchase|buying)\b",
        r"\bworth buying\b",
        r"\bbang for (?:your|the) buck\b",
        r"\bmonetization\b",
        r"\bmonetisation\b",
        r"\bmicrotransaction(?:s)?\b",
        r"\bseason pass\b",
        r"\breplayability\b",
        r"\breplay value\b",
        r"\bcontent[- ]to[- ]price\b",
    ),
    # شخصیت فقط وقتی نشانه‌ی روایت است که خودش در متن با توسعه، نوشتار یا داستان
    # همراه باشد. عبارت «جهان مثل یک شخصیت است» نباید به‌اشتباه کارت داستان بسازد.
    "story": (
        r"\bstory\b",
        r"\bnarrative\b",
        r"\bdialogue\b",
        r"\bwriting\b",
        r"\bplot\b",
        r"\blore\b",
        r"\bending\b",
        r"\bcutscene(?:s)?\b",
        r"\bcharacter development\b",
        r"\bcharacter(?:s)?\s+(?:arc|writing|development|dialogue)\b",
    ),
    "world_design": (
        r"\bopen[- ]world\b",
        r"\benvironment(?:s)?\b",
        r"\blocation(?:s)?\b",
        r"\bmap\b",
        r"\bbiome(?:s)?\b",
        r"\bpywel\b",
        r"\bworld\b",
        r"\blandscape\b",
        r"\bregion(?:s)?\b",
    ),
    "gameplay": (
        r"\bgameplay\b",
        r"\bcombat\b",
        r"\bbattle(?:s)?\b",
        r"\bboss(?:es)?\b",
        r"\bmechanic(?:s)?\b",
        r"\bsystem(?:s)?\b",
        r"\bcontrol(?:s)?\b",
        r"\bpuzzle(?:s)?\b",
        r"\bencounter(?:s)?\b",
        r"\bweapon(?:s)?\b",
        r"\bbuild(?:s)?\b",
        r"\bprogression\b",
        r"\bmovement\b",
        r"\bstealth\b",
        r"\bskill(?:s)?\b",
        r"\bactivit(?:y|ies)\b",
        r"\bchallenge(?:s)?\b",
        r"\bexplor(?:e|ation|ing)\b",
        r"\btravel\b",
        r"\btraversal\b",
    ),
}


TECHNICAL_NEGATIVE_PATTERNS = (
    r"\bdip in performance\b",
    r"\bperformance issue(?:s)?\b",
    r"\bpoor performance\b",
    r"\bstutter(?:ing)?\b",
    r"\bcrash(?:es|ing)?\b",
    r"\bbug(?:s)?\b",
    r"\bglitch(?:es)?\b",
    r"\bunstable\b",
    r"\black of polish\b",
    r"\binput lag\b",
    r"\bloading (?:issue|problem|time)\b",
)


TECHNICAL_POSITIVE_PATTERNS = (
    r"\bframe rate stability\b",
    r"\bstable frame rate\b",
    r"\bruns? (?:very )?well\b",
    r"\bsmooth(?:ly)?\b",
    r"\bwell optimized\b",
    r"\boptimization is (?:good|excellent)\b",
    r"\bbiggest triumph\b",
    r"\btechnically impressive\b",
)


CATEGORY_LABELS = {
    "gameplay": "گیم‌پلی",
    "world_design": "طراحی جهان",
    "story": "داستان و شخصیت‌پردازی",
    "visuals": "تصویرسازی و طراحی هنری",
    "audio": "صداگذاری و موسیقی",
    "technical": "فنی و عملکرد",
    "value": "ارزش خرید و محتوا",
}


def classify_evidence_rule_based(item: dict) -> tuple[str | None, str | None]:
    """
    دسته‌بندی قانون‌محور با اولویتِ معنایی.
    «world ... character» کارت طراحی جهان است، نه داستان.
    """
    evidence = clean_text(str(item.get("evidence_en") or ""))
    section = clean_text(str(item.get("section") or "")).lower()

    if not evidence:
        return None, None

    # ترتیب مهم است: مقوله‌های دقیق‌تر پیش از مقوله‌های گسترده‌تر.
    if _matches_any(evidence, CATEGORY_PATTERNS["technical"]):
        category = "technical"
    elif _matches_any(evidence, CATEGORY_PATTERNS["audio"]):
        category = "audio"
    elif _matches_any(evidence, CATEGORY_PATTERNS["visuals"]):
        category = "visuals"
    elif _matches_any(evidence, CATEGORY_PATTERNS["value"]):
        category = "value"
    # وقتی یک جمله درباره‌ی open world، نقشه یا منطقه‌هاست، اول طراحی جهان
    # محسوب می‌شود؛ وجود واژه‌ی story در جمله نباید آن را به کارت روایت ببرد.
    elif _matches_any(evidence, CATEGORY_PATTERNS["world_design"]):
        category = "world_design"
    elif _matches_any(evidence, CATEGORY_PATTERNS["story"]):
        category = "story"
    elif _matches_any(evidence, CATEGORY_PATTERNS["gameplay"]):
        category = "gameplay"
    else:
        return None, None

    if section == "positives":
        sentiment = "positive"
    elif section == "negatives":
        sentiment = "negative"
    elif section == "technical_notes":
        if _matches_any(evidence, TECHNICAL_NEGATIVE_PATTERNS):
            sentiment = "negative"
        elif _matches_any(evidence, TECHNICAL_POSITIVE_PATTERNS):
            sentiment = "positive"
        else:
            sentiment = "neutral"
    else:
        return None, None

    return category, sentiment


def run_rule_based_regression_checks() -> None:
    """تست‌های کوتاه برای جلوگیری از بازگشت خطاهای طبقه‌بندی شناخته‌شده."""
    cases = [
        (
            "Pywel is, for what it's worth, one of the best open worlds ever created.",
            "positives",
            "world_design",
            "positive",
        ),
        (
            "At this price, the game is worth the money for fans of the genre.",
            "positives",
            "value",
            "positive",
        ),
        (
            "I did notice a significant dip in performance in the last few days.",
            "technical_notes",
            "technical",
            "negative",
        ),
        (
            "Frame rate stability feels like one of the biggest triumphs.",
            "technical_notes",
            "technical",
            "positive",
        ),
        (
            "The voice acting is superb and the orchestral soundtrack elevates every scene.",
            "positives",
            "audio",
            "positive",
        ),
        (
            "The English dubbing feels flat and the sound design lacks any punch.",
            "negatives",
            "audio",
            "negative",
        ),
    ]

    for evidence_en, section, expected_category, expected_sentiment in cases:
        actual_category, actual_sentiment = classify_evidence_rule_based(
            {"evidence_en": evidence_en, "section": section}
        )
        if (actual_category, actual_sentiment) != (
            expected_category,
            expected_sentiment,
        ):
            fail(
                "Rule-based regression check failed: "
                f"expected {(expected_category, expected_sentiment)}, "
                f"got {(actual_category, actual_sentiment)} for: {evidence_en}"
            )



def run_metadata_regression_checks() -> None:
    """جلوگیری از بازگشت خطا در نگاشت وضعیت عرضه و شناسایی سایت‌های معتبر."""
    status_cases = [
        ("", "released", True),
        ("Early Access", "early_access", True),
        ("early-access", "early_access", True),
        ("Remastered", "remaster", True),
        ("DLC", "dlc", True),
        ("A Totally Unknown Status", "custom", False),
    ]

    for raw, expected_key, expected_recognized in status_cases:
        result = normalize_game_status(raw)
        if result["key"] != expected_key or result["recognized"] != expected_recognized:
            fail(
                "Game status regression check failed: "
                f"input={raw!r} expected key={expected_key!r} "
                f"got {result}"
            )

    site_cases = [
        ("https://www.ign.com/reviews/crimson-desert-review", "IGN"),
        ("https://www.gamesradar.com/crimson-desert-review/", "GamesRadar+"),
        ("https://example-not-a-real-outlet.com/review", None),
    ]

    for url, expected_name in site_cases:
        actual_name = canonical_reputable_site_name(url)
        if actual_name != expected_name:
            fail(
                "Reputable site regression check failed: "
                f"url={url!r} expected={expected_name!r} got={actual_name!r}"
            )


def category_score_from_evidence(
    overall_score: float | None,
    evidence_signals: list[dict],
) -> tuple[float | None, str, str]:
    """
    امتیاز دسته‌ای را با قانون ثابت می‌سازد. یک شاهد تنها اجازه ندارد
    دسته را بیش از حد از امتیاز کلی دور کند.
    """
    if overall_score is None or not evidence_signals:
        return None, "نامشخص", "برداشت اولیه"

    signal_values = {
        "positive": 1.0,
        "negative": -1.0,
        "mixed": 0.0,
        "neutral": 0.0,
    }
    values = [
        signal_values.get(item.get("sentiment"), 0.0)
        for item in evidence_signals
    ]
    direction_value = sum(values) / len(values)
    evidence_count = len(evidence_signals)
    source_count = len({
        item.get("site_name", "")
        for item in evidence_signals
        if item.get("site_name")
    })

    if evidence_count >= 3 and source_count >= 2:
        coverage_factor = 0.75
    elif evidence_count >= 2 or source_count >= 2:
        coverage_factor = 0.50
    else:
        coverage_factor = 0.25

    delta = REVIEW_CATEGORY_MAX_DELTA * direction_value * coverage_factor
    raw_score = overall_score + delta

    lower = max(0.0, overall_score - REVIEW_CATEGORY_MAX_DELTA)
    upper = min(10.0, overall_score + REVIEW_CATEGORY_MAX_DELTA)
    score_10 = round(min(max(raw_score, lower), upper), 1)

    if direction_value >= 0.50:
        trend_fa = "مثبت"
    elif direction_value <= -0.50:
        trend_fa = "منفی"
    else:
        trend_fa = "ترکیبی"

    if evidence_count >= 3 and source_count >= 2:
        confidence_fa = "متوسط"
    elif evidence_count >= 2 or source_count >= 2:
        confidence_fa = "محدود"
    else:
        confidence_fa = "برداشت اولیه"

    return score_10, trend_fa, confidence_fa


def build_game_intro(
    client: OpenAI,
    game: str,
    platform: str,
    release_date: str,
    game_status_label: str,
    description_en: str,
) -> dict:
    """
    معرفی کوتاه بازی را می‌سازد. اگر توضیح رسمی از صفحه‌ی Metacritic در دسترس
    باشد، فقط همان بازنویسی می‌شود (نه ترجمه‌ی کلمه‌به‌کلمه)؛ در غیر این صورت
    فقط از واقعیت‌های ساختاریافته (نام، پلتفرم، تاریخ، وضعیت عرضه) یک جمله‌ی
    خیلی کوتاه ساخته می‌شود. هیچ‌جا جزئیات گیم‌پلی از خود مدل ساخته نمی‌شود.
    """
    description_en = clean_text(description_en)[:900]

    def template_fallback() -> str:
        bits = [f"{game} برای {platform}"]
        if release_date:
            bits.append(f"در تاریخ {release_date}")
        if game_status_label:
            bits.append(f"با وضعیت {game_status_label}")
        return " ".join(bits) + " منتشر شده و در ادامه بر پایه‌ی نقدهای منتخب بررسی می‌شود."

    if not description_en:
        return {"intro_fa": template_fallback(), "method": "template_no_description"}

    facts_lines = [f"Game: {game}", f"Platform: {platform}"]
    if release_date:
        facts_lines.append(f"Release date: {release_date}")
    if game_status_label:
        facts_lines.append(f"Release status: {game_status_label}")

    prompt = f"""
Write a short, neutral, factual Persian introduction (2 to 3 sentences maximum)
for the opening of a game review article.

Facts you may use:
{chr(10).join(facts_lines)}

Official page description (English, may be from the publisher or Metacritic):
{description_en}

Rules:
- Paraphrase the description in your own words; do not translate it word for word
  and do not copy full phrases from it verbatim.
- Do not invent gameplay features, story details, or claims that are not present
  in the description or the facts above.
- Do not mention scores, review verdicts, or critic opinions; this is only a
  neutral introduction to the game itself.
- Return strict JSON: {{"intro_fa": "..."}}
""".strip()

    try:
        raw = ask_openai_json(client, prompt, max_tokens=300)
        intro_fa = _clean_article_text(raw.get("intro_fa"), min_words=6, max_words=110)
    except Exception as exc:
        intro_fa = ""
        print(f"Game intro generation failed, falling back to template: {repr(exc)}")

    if not intro_fa:
        return {"intro_fa": template_fallback(), "method": "template_fallback_after_api_error"}

    return {"intro_fa": intro_fa, "method": "openai_paraphrase_from_official_description"}


def build_poormaz_assessment(client: OpenAI, dossier: dict) -> dict:
    """
    کارت امتیاز را فقط از شواهد تأییدشده و قواعد قابل‌بررسی می‌سازد.
    مدل در این مرحله هیچ نقشی در دسته‌بندی ندارد.
    """
    del client  # API در این مرحله عمداً استفاده نمی‌شود.

    score_info = calculate_overall_score(dossier)
    evidence_index = build_evidence_index(
        dossier.get("review_sources", []) or []
    )

    if not evidence_index or score_info.get("overall_score_10") is None:
        return {
            **score_info,
            "scorecard": [],
            "evidence_index": evidence_index,
            "classification_method": "rule_based",
            "uncategorized_refs": [],
        }

    signals_by_category: dict[str, list[dict]] = {
        key: [] for key in CATEGORY_LABELS
    }
    uncategorized_refs = []

    for ref_id, item in evidence_index.items():
        category, sentiment = classify_evidence_rule_based(item)

        if category is None or sentiment is None:
            uncategorized_refs.append(ref_id)
            continue

        signals_by_category[category].append(
            {
                "ref_id": ref_id,
                "sentiment": sentiment,
                "site_name": item["site_name"],
            }
        )

    scorecard = []

    for key in CATEGORY_LABELS:
        signals = signals_by_category[key]

        if not signals:
            continue

        score_10, trend_fa, confidence_fa = category_score_from_evidence(
            score_info["overall_score_10"],
            signals,
        )

        if score_10 is None:
            continue

        scorecard.append(
            {
                "key": key,
                "label_fa": CATEGORY_LABELS[key],
                "score_10": score_10,
                "trend_fa": trend_fa,
                "confidence_fa": confidence_fa,
                "evidence_count": len(signals),
                "source_count": len({
                    signal["site_name"]
                    for signal in signals
                    if signal["site_name"]
                }),
                "supported_refs": [
                    signal["ref_id"]
                    for signal in signals
                ],
                "evidence_signals": [
                    {
                        "ref_id": signal["ref_id"],
                        "sentiment": signal["sentiment"],
                    }
                    for signal in signals
                ],
            }
        )

    return {
        **score_info,
        "scorecard": scorecard,
        "evidence_index": evidence_index,
        "classification_method": "rule_based",
        "uncategorized_refs": uncategorized_refs,
    }

def _format_score_10(value) -> str:
    try:
        return f"{float(value):.1f}/10"
    except (TypeError, ValueError):
        return "نامشخص"


def _source_score_label(source: dict) -> str:
    score = source.get("original_score")
    return clean_text(str(score)) if score is not None else "بدون نمره‌ی قابل‌تشخیص"


def _dedupe_points(points: list[dict], max_items: int) -> list[dict]:
    unique = []
    seen = set()

    for point in points:
        point_fa = clean_text(str(point.get("point_fa") or ""))
        site_name = clean_text(str(point.get("site_name") or ""))
        url = str(point.get("url") or "").strip()

        if not point_fa:
            continue

        key = point_fa.casefold()
        if key in seen:
            continue

        seen.add(key)
        unique.append(
            {
                "point_fa": point_fa,
                "site_name": site_name or "منبع نامشخص",
                "url": url,
            }
        )

        if len(unique) >= max_items:
            break

    return unique


def _collect_article_points(
    review_sources: list[dict],
    section: str,
    max_items: int,
) -> list[dict]:
    points = []

    for source in review_sources:
        site_name = clean_text(str(source.get("site_name") or ""))
        url = str(source.get("url") or "").strip()

        for point in source.get(section, []) or []:
            if not isinstance(point, dict):
                continue

            points.append(
                {
                    "point_fa": point.get("point_fa"),
                    "site_name": site_name,
                    "url": url,
                }
            )

    return _dedupe_points(points, max_items)


def _render_article_points(points: list[dict]) -> list[str]:
    if not points:
        return ["- در منابع بررسی‌شده، نکته‌ی مستقیم و قابل‌اتکای کافی برای این بخش ثبت نشده است."]

    lines = []

    for point in points:
        site_name = point["site_name"]
        url = point["url"]

        if url:
            lines.append(f"- {point['point_fa']} [{site_name}]({url})")
        else:
            lines.append(f"- {point['point_fa']} ({site_name})")

    return lines


def _scorecard_summary_sentences(scorecard: list[dict]) -> list[str]:
    """
    جمله‌های اجماع منتقدان را فقط از رویِ برچسبِ بخش و جهت‌گیری می‌سازد.
    عمداً «سطح پوشش شواهد»/«برداشت اولیه» را اینجا نمی‌آورد؛ آن‌ها برچسبِ
    کیفیتِ داخلیِ خودِ پرونده‌اند، نه واقعیتی درباره‌ی بازی که باید به خواننده
    نشان داده شود (در dossier و کارت امتیاز همچنان در دسترس‌اند).
    """
    positive = []
    negative = []
    mixed = []

    for category in scorecard:
        label = clean_text(str(category.get("label_fa") or ""))
        trend = clean_text(str(category.get("trend_fa") or ""))

        if not label:
            continue

        if trend == "مثبت":
            positive.append(label)
        elif trend == "منفی":
            negative.append(label)
        else:
            mixed.append(label)

    sentences = []

    if positive:
        sentences.append(
            "در پرونده‌ی فعلی، ارزیابی منابع نسبت به "
            + "، ".join(positive)
            + " مثبت‌تر است."
        )

    if negative:
        sentences.append(
            "نکات احتیاطی ثبت‌شده بیشتر به "
            + "، ".join(negative)
            + " مربوط می‌شوند."
        )

    if mixed:
        sentences.append(
            "برای "
            + "، ".join(mixed)
            + " شواهد موجود تصویر ترکیبی یا خنثی نشان می‌دهند."
        )

    return sentences


def _audience_fit_sentence(scorecard: list[dict], game: str) -> str:
    """
    جمله‌ی «این بازی برای چه کسانی مناسب است» را فقط از جهت‌گیریِ از قبل
    محاسبه‌شده‌ی کارت امتیاز می‌سازد؛ هیچ ادعای تازه‌ای درباره‌ی مخاطب اضافه نمی‌شود.
    """
    if not scorecard:
        return (
            "برای قضاوت درباره‌ی مناسب بودن این بازی برای سلیقه‌های مختلف، "
            "شواهد کافی در این پرونده ثبت نشده است."
        )

    positive_labels = [
        clean_text(str(c.get("label_fa") or ""))
        for c in scorecard
        if clean_text(str(c.get("trend_fa") or "")) == "مثبت"
    ]
    negative_labels = [
        clean_text(str(c.get("label_fa") or ""))
        for c in scorecard
        if clean_text(str(c.get("trend_fa") or "")) == "منفی"
    ]
    positive_labels = [label for label in positive_labels if label]
    negative_labels = [label for label in negative_labels if label]

    sentences = []

    if positive_labels:
        sentences.append(
            f"{game} بیشتر به بازیکنانی پیشنهاد می‌شود که برای "
            + "، ".join(positive_labels)
            + " در یک بازی اهمیت زیادی قائل‌اند، چون ارزیابی منابع بررسی‌شده در "
            "این زمینه‌ها مثبت‌تر بوده است."
        )

    if negative_labels:
        sentences.append(
            "در مقابل، اگر "
            + "، ".join(negative_labels)
            + " اولویت اصلی شما برای خرید است، بهتر است پیش از خرید نقدهای "
            "کامل منابع را هم بررسی کنید، چون شواهد این پرونده در این زمینه‌ها "
            "نکات احتیاطی ثبت کرده‌اند."
        )

    if not sentences:
        sentences.append(
            f"شواهد ثبت‌شده درباره‌ی {game} در این پرونده ترکیبی است و در حال "
            "حاضر برای هیچ‌کدام از بخش‌ها جهت‌گیری قاطع مثبت یا منفی گزارش نشده است."
        )

    return " ".join(sentences)


def _render_site_differences(review_sources: list[dict]) -> list[str]:
    """
    تفاوت دیدگاه سایت‌ها را فقط با نمره‌ی هر منبع و یک نکته‌ی متمایزکننده‌ی
    برگرفته از همان شاهد تأییدشده نشان می‌دهد؛ هیچ تضاد یا اختلاف‌نظری
    ساخته یا فرض نمی‌شود، فقط داده‌ی موجود کنار هم گذاشته می‌شود.
    """
    if not review_sources:
        return ["در حال حاضر منبعی برای مقایسه‌ی دیدگاه‌ها ثبت نشده است."]

    lines: list[str] = []
    numeric_scores = [
        score for score in (
            as_score_10(source.get("review_score_10")) for source in review_sources
        )
        if score is not None
    ]

    if len(numeric_scores) >= 2:
        spread = round(max(numeric_scores) - min(numeric_scores), 1)
        if spread >= 1.5:
            lines.append(
                f"فاصله‌ی نمره‌ی منابع بررسی‌شده در این پرونده حدود {spread} از ۱۰ "
                "است؛ یعنی دیدگاه منتقدان درباره‌ی این بازی یکدست نیست."
            )
        else:
            lines.append(
                "نمره‌ی منابع بررسی‌شده در این پرونده به هم نزدیک است و اختلاف "
                "چشمگیری در امتیازدهی دیده نمی‌شود."
            )
        lines.append("")

    lines.append("| منبع | نمره | نکته‌ی متمایزکننده |")
    lines.append("|---|---:|---|")

    for source in review_sources:
        site_name = clean_text(str(source.get("site_name") or "منبع نامشخص"))
        url = str(source.get("url") or "").strip()
        score_label = _source_score_label(source)

        verdict_points = source.get("verdict") or []
        if verdict_points and isinstance(verdict_points[0], dict):
            highlight = clean_text(str(verdict_points[0].get("point_fa") or ""))
        else:
            highlight = ""

        if not highlight:
            fallback_points = (source.get("negatives") or []) + (source.get("positives") or [])
            if fallback_points and isinstance(fallback_points[0], dict):
                highlight = clean_text(str(fallback_points[0].get("point_fa") or ""))

        if not highlight:
            highlight = "نکته‌ی متمایزکننده‌ی قابل‌استناد در این منبع ثبت نشده است."

        site_label = f"[{site_name}]({url})" if url else site_name
        # Markdown Table کاراکتر | داخل سلول را می‌شکند.
        highlight = highlight.replace("|", "/")
        lines.append(f"| {site_label} | {score_label} | {highlight} |")

    return lines


def _extract_numbers(text: str) -> set[str]:
    """همه‌ی اعداد (فارسی یا انگلیسی) داخل متن را برای بررسی رانش عددی برمی‌گرداند."""
    persian_digits = "۰۱۲۳۴۵۶۷۸۹"
    translation = str.maketrans(persian_digits, "0123456789")
    normalized = clean_text(text).translate(translation)
    return set(re.findall(r"\d+(?:\.\d+)?", normalized))


# عبارت‌هایی که فقط برای گزارش داخلیِ کیفیت شواهد ساخته شده‌اند، نه برای خواننده.
# اگر بازنویسیِ مدل هرکدام از این‌ها را (که اصلاً نباید در ورودی هم باشند) تولید
# کند، نشانه‌ی درز کردن زبان داخلی به متن عمومی است.
_FORBIDDEN_META_PHRASES = (
    "پوشش شواهد",
    "برداشت اولیه",
    "شواهد تأییدشده",
    "شواهد بیشتری نیاز دارد",
    "این ارزیابی فعلاً",
    "evidence coverage",
    "confidence level",
)


def _rewritten_text_is_safe(
    original_fa: str,
    rewritten_fa: str,
    allowed_site_names: set[str],
) -> bool:
    """
    بازنویسیِ مدل را فقط اگر هیچ عدد، نام‌سایت یا اصطلاح داخلیِ تازه‌ای نسبت به
    متنِ اصلیِ تأییدشده اضافه نکرده باشد، امن می‌داند. این فقط یک محافظ
    سخت‌گیرانه است، نه تضمین کامل صحت؛ در صورت شک، رد می‌کند تا نسخه‌ی
    قالب‌محورِ از قبل تأییدشده جایگزین شود.
    """
    if not clean_text(rewritten_fa):
        return False

    if not _extract_numbers(rewritten_fa).issubset(_extract_numbers(original_fa)):
        return False

    for phrase in _FORBIDDEN_META_PHRASES:
        if phrase in rewritten_fa:
            return False

    for site_name in REPUTABLE_SITE_DOMAINS.values():
        if site_name in allowed_site_names:
            continue
        if site_name and site_name in rewritten_fa:
            return False

    original_word_count = len(original_fa.split()) or 1
    rewritten_word_count = len(rewritten_fa.split())
    # بازنویسی نباید آنقدر کوتاه شود که محتوا حذف شده باشد، یا آنقدر بلند شود
    # که احتمال افزوده شدن ادعای تازه بالا برود.
    if rewritten_word_count < original_word_count * 0.45:
        return False
    if rewritten_word_count > original_word_count * 2.5 + 15:
        return False

    return True


def rewrite_section_as_narrative(
    client: OpenAI,
    section_title_fa: str,
    original_fa: str,
    allowed_site_names: set[str],
    game: str,
) -> dict:
    """
    یک بخشِ از قبل تأییدشده و قالب‌محور را فقط بازنویسی می‌کند تا مثل بخشی از
    یک نقد واقعی خوانده شود، نه یک گزارش کنترل کیفیت. مدل هیچ حق افزودن
    واقعیت، عدد، نام یا ادعای تازه‌ای ندارد؛ فقط اجازه‌ی روان‌نویسی دارد.
    اگر ورودی خالی باشد یا OpenAI در دسترس نباشد یا خروجی از آزمون رانش رد
    شود، همان متنِ قالب‌محورِ اصلی بدون تغییر برگردانده می‌شود.
    """
    original_fa = clean_text(original_fa)

    if not original_fa or client is None:
        return {"text_fa": original_fa, "method": "template_unchanged"}

    prompt = f"""
You will rewrite an already fact-checked Persian paragraph for a game review
website called Poormaz, about the game "{game}". Section: {section_title_fa}

Original verified text (Persian) — every fact, name, and number in it has
already been checked against the source reviews:
{original_fa}

Rules:
- Rewrite ONLY the phrasing so it reads as natural, flowing Persian editorial
  prose, like part of a real published game review — not a bullet list and
  not an internal QA report.
- Do NOT add any new fact, name, number, score, or claim that is not already
  present in the original text above.
- Do NOT remove any specific fact, score, or named source from the original text.
- Do NOT mention internal review-process concepts such as "evidence coverage",
  "confidence level", "برداشت اولیه" or "پوشش شواهد"; a normal reader should
  never see these — write as a normal published review would.
- Do not use bullet points, dashes, or markdown; write connected sentences.
- Keep roughly the same length as the original (not much shorter, not much longer).
- Return strict JSON: {{"text_fa": "..."}}
""".strip()

    try:
        raw = ask_openai_json(client, prompt, max_tokens=500)
        rewritten_fa = _clean_article_text(raw.get("text_fa"), min_words=0, max_words=320)
    except Exception as exc:
        print(f"Narrative rewrite failed for '{section_title_fa}', keeping template text: {repr(exc)}")
        return {"text_fa": original_fa, "method": "template_fallback_after_api_error"}

    if not _rewritten_text_is_safe(original_fa, rewritten_fa, allowed_site_names):
        print(
            f"Narrative rewrite for '{section_title_fa}' failed the drift check; "
            "keeping template text."
        )
        return {"text_fa": original_fa, "method": "template_fallback_after_validation"}

    return {"text_fa": rewritten_fa, "method": "openai_narrative_rewrite"}


def _source_url_map(review_sources: list[dict]) -> dict[str, str]:
    urls = {}

    for source in review_sources:
        site_name = clean_text(str(source.get("site_name") or ""))
        url = str(source.get("url") or "").strip()

        if site_name and url:
            urls[site_name] = url

    return urls


def _article_fact_pack(dossier: dict) -> dict:
    """
    بسته‌ی واقعیتِ محدود برای نگارش مقاله. مدل فقط همین بسته را می‌بیند؛
    بنابراین راهی برای اضافه‌کردن ادعاهای خارج از پرونده ندارد.
    """
    game = clean_text(str(dossier.get("game") or "")) or "بازی"
    platform = clean_text(str(dossier.get("platform") or ""))
    release_date = clean_text(str(dossier.get("release_date") or ""))
    game_status = dossier.get("game_status", {}) or {}
    game_intro = dossier.get("game_intro", {}) or {}
    metacritic = dossier.get("metacritic", {}) or {}
    assessment = dossier.get("poormaz_assessment", {}) or {}
    review_sources = dossier.get("review_sources", []) or []
    evidence_index = assessment.get("evidence_index", {}) or {}
    source_urls = _source_url_map(review_sources)

    evidence = []
    for ref_id, item in evidence_index.items():
        site_name = clean_text(str(item.get("site_name") or ""))
        point_fa = clean_text(str(item.get("point_fa") or ""))
        section = clean_text(str(item.get("section") or ""))

        if not ref_id or not point_fa:
            continue

        evidence.append(
            {
                "id": ref_id,
                "site_name": site_name,
                "section": section,
                "point_fa": point_fa,
                "url": source_urls.get(site_name, ""),
            }
        )

    scorecards = []
    for category in assessment.get("scorecard", []) or []:
        key = clean_text(str(category.get("key") or ""))
        label_fa = clean_text(str(category.get("label_fa") or ""))

        if not key or not label_fa:
            continue

        scorecards.append(
            {
                "id": f"C:{key}",
                "key": key,
                "label_fa": label_fa,
                "score_10": category.get("score_10"),
                "trend_fa": clean_text(str(category.get("trend_fa") or "")),
                "confidence_fa": clean_text(str(category.get("confidence_fa") or "")),
                "supported_refs": list(category.get("supported_refs", []) or []),
                "evidence_signals": [
                    {
                        "ref_id": clean_text(str(signal.get("ref_id") or "")),
                        "sentiment": clean_text(str(signal.get("sentiment") or "")),
                    }
                    for signal in category.get("evidence_signals", []) or []
                    if isinstance(signal, dict) and signal.get("ref_id")
                ],
            }
        )

    sources = []
    for source in review_sources:
        sources.append(
            {
                "site_name": clean_text(str(source.get("site_name") or "")),
                "title": clean_text(str(source.get("title") or "")),
                "url": str(source.get("url") or "").strip(),
                "score": _source_score_label(source),
            }
        )

    return {
        "meta": {
            "game": game,
            "platform": platform,
            "release_date": release_date,
            "game_status_key": clean_text(str(game_status.get("key") or "")),
            "game_status_label": clean_text(str(game_status.get("label_fa") or "")),
            "game_intro_fa": clean_text(str(game_intro.get("intro_fa") or "")),
            "poormaz_score_10": assessment.get("overall_score_10"),
            "metacritic_score_100": metacritic.get("metascore_100"),
            "critic_review_count": metacritic.get("critic_review_count"),
            "metacritic_url": str(metacritic.get("url") or "").strip(),
            "formula_fa": clean_text(str(assessment.get("formula_fa") or "")),
        },
        "scorecards": scorecards,
        "evidence": evidence,
        "sources": sources,
    }


def _allowed_article_supports(fact_pack: dict) -> set[str]:
    allowed = {"META"}
    allowed.update(item.get("id") for item in fact_pack.get("evidence", []) if item.get("id"))
    allowed.update(item.get("id") for item in fact_pack.get("scorecards", []) if item.get("id"))
    return allowed


def _clean_article_text(value, min_words: int = 0, max_words: int = 160) -> str:
    text = clean_text(str(value or ""))
    text = re.sub(r"https?://\S+", "", text).strip()

    # مدل نباید با نقل‌قول مستقیم یا Markdown خودش مقاله را قالب‌بندی کند.
    text = text.replace("`", "").replace("#", "").strip()

    word_count = len(text.split())
    if word_count < min_words or word_count > max_words:
        return ""

    return text


def _normalize_supports(value, allowed: set[str], max_items: int = 5) -> list[str]:
    if not isinstance(value, list):
        return []

    supports = []
    for item in value:
        ref_id = clean_text(str(item or ""))
        if ref_id in allowed and ref_id not in supports:
            supports.append(ref_id)
        if len(supports) >= max_items:
            break

    return supports


def _normalize_article_block(
    value,
    allowed_supports: set[str],
    min_words: int,
    max_words: int,
) -> dict | None:
    if not isinstance(value, dict):
        return None

    text_fa = _clean_article_text(
        value.get("text_fa"),
        min_words=min_words,
        max_words=max_words,
    )
    supports = _normalize_supports(value.get("supports"), allowed_supports)

    if not text_fa or not supports:
        return None

    return {"text_fa": text_fa, "supports": supports}


def _resolve_article_citations(
    supports: list[str],
    fact_pack: dict,
) -> list[dict]:
    evidence_by_id = {
        item.get("id"): item
        for item in fact_pack.get("evidence", [])
        if item.get("id")
    }
    scorecard_by_id = {
        item.get("id"): item
        for item in fact_pack.get("scorecards", [])
        if item.get("id")
    }

    source_links = []
    seen = set()

    def add(site_name: str, url: str):
        site_name = clean_text(str(site_name or ""))
        url = str(url or "").strip()
        key = (site_name, url)
        if site_name and url and key not in seen:
            seen.add(key)
            source_links.append({"site_name": site_name, "url": url})

    for support in supports:
        if support == "META":
            meta = fact_pack.get("meta", {}) or {}
            add("Metacritic", meta.get("metacritic_url", ""))
            continue

        if support in evidence_by_id:
            item = evidence_by_id[support]
            add(item.get("site_name", ""), item.get("url", ""))
            continue

        if support in scorecard_by_id:
            for evidence_id in scorecard_by_id[support].get("supported_refs", []) or []:
                item = evidence_by_id.get(evidence_id)
                if item:
                    add(item.get("site_name", ""), item.get("url", ""))

    return source_links[:3]


def _render_inline_citations(supports: list[str], fact_pack: dict) -> str:
    citations = _resolve_article_citations(supports, fact_pack)

    if not citations:
        return ""

    return " " + " ".join(
        f"[{item['site_name']}]({item['url']})"
        for item in citations
    )


def _fallback_article_preview(dossier: dict) -> dict:
    """نسخه‌ی پایدار و قانون‌محور در صورت خطای API یا خروجی نامعتبر."""
    return build_rule_based_article_preview(dossier)


def build_rule_based_article_preview(dossier: dict) -> dict:
    """
    پیش‌نمایش قانون‌محور نسل قبل. به‌عنوان مسیر امن نگه داشته می‌شود تا
    خطای API هرگز باعث شکست کل Workflow نشود.
    """
    game = clean_text(str(dossier.get("game") or "")) or "بازی"
    platform = clean_text(str(dossier.get("platform") or ""))
    release_date = clean_text(str(dossier.get("release_date") or ""))
    metacritic = dossier.get("metacritic", {}) or {}
    assessment = dossier.get("poormaz_assessment", {}) or {}
    review_sources = dossier.get("review_sources", []) or []
    scorecard = assessment.get("scorecard", []) or []

    overall_score = assessment.get("overall_score_10")
    metascore_100 = metacritic.get("metascore_100")
    critic_count = metacritic.get("critic_review_count")

    positives = _collect_article_points(
        review_sources,
        "positives",
        ARTICLE_MAX_POINTS_PER_SECTION,
    )
    negatives = _collect_article_points(
        review_sources,
        "negatives",
        ARTICLE_MAX_POINTS_PER_SECTION,
    )
    technical_notes = _collect_article_points(
        review_sources,
        "technical_notes",
        ARTICLE_MAX_POINTS_PER_SECTION,
    )

    title_fa = f"نقد و بررسی {game} | جمع‌بندی امتیازها و نظر منتقدان"
    excerpt_fa = f"جمع‌بندی Poormaz از نقدهای منتخب {game}."

    markdown = [
        f"# {title_fa}",
        "",
        "> این متن یک پیش‌نمایش تحریریه‌ای است که فقط از نمره‌ها و نکات تأییدشده‌ی منابع انتخاب‌شده ساخته شده است. هنوز برای وردپرس منتشر نشده است.",
        "",
        "## نتیجه در یک نگاه",
    ]

    if overall_score is not None:
        markdown.append(f"- **امتیاز Poormaz:** {_format_score_10(overall_score)}")
    if metascore_100 is not None:
        critic_part = f" بر پایه‌ی {critic_count} نقد منتقدان" if critic_count is not None else ""
        markdown.append(f"- **متاکریتیک:** {metascore_100}/100{critic_part}")
    if platform:
        markdown.append(f"- **پلتفرم پرونده:** {platform}")
    if release_date:
        markdown.append(f"- **تاریخ عرضه‌ی ثبت‌شده:** {release_date}")

    markdown.extend(["", "## کارت امتیاز Poormaz", ""])
    if scorecard:
        markdown.extend(["| بخش | امتیاز | جهت‌گیری | پوشش شواهد |", "|---|---:|---|---|"])
        for category in scorecard:
            markdown.append(
                f"| {category.get('label_fa', 'نامشخص')} | "
                f"{_format_score_10(category.get('score_10'))} | "
                f"{category.get('trend_fa', 'نامشخص')} | "
                f"{category.get('confidence_fa', 'محدود')} |"
            )
    else:
        markdown.append("برای ساخت کارت امتیاز، شواهد تأییدشده‌ی کافی در دسترس نبود.")

    markdown.extend(["", "## نقاطی که منابع بررسی‌شده تحسین کرده‌اند"])
    markdown.extend(_render_article_points(positives))
    markdown.extend(["", "## نکات احتیاطی منتقدان"])
    markdown.extend(_render_article_points(negatives))
    markdown.extend(["", "## وضعیت فنی و عملکرد"])
    markdown.extend(_render_article_points(technical_notes))
    markdown.extend(["", "## جمع‌بندی Poormaz"])

    if overall_score is not None:
        markdown.append(
            f"امتیاز تجمیعی Poormaz برای {game} {_format_score_10(overall_score)} است. "
            + assessment.get("formula_fa", "")
        )
    markdown.extend(_scorecard_summary_sentences(scorecard))

    markdown.extend(["", "## منابع بررسی‌شده"])
    for source in review_sources:
        site_name = clean_text(str(source.get("site_name") or "منبع نامشخص"))
        title = clean_text(str(source.get("title") or "نقد بازی"))
        url = str(source.get("url") or "").strip()
        score_label = _source_score_label(source)
        if url:
            markdown.append(f"- [{site_name}: {title}]({url}) | نمره: {score_label}")
        else:
            markdown.append(f"- {site_name}: {title} | نمره: {score_label}")

    markdown.extend([
        "",
        "---",
        "یادداشت تحریریه: این پیش‌نمایش به‌صورت خودکار تولید شده و پیش از انتشار، برای لحن، سئو، تصویر شاخص و جزئیات نهایی باید بازبینی شود.",
    ])

    return {
        "status": "preview_only_rule_based_fallback",
        "wordpress_post_created": False,
        "title_fa": title_fa,
        "excerpt_fa": excerpt_fa,
        "markdown": "\n".join(markdown).strip() + "\n",
        "source_links": [
            {
                "site_name": clean_text(str(source.get("site_name") or "")),
                "title": clean_text(str(source.get("title") or "")),
                "url": str(source.get("url") or "").strip(),
                "score": _source_score_label(source),
            }
            for source in review_sources
        ],
        "review_note_fa": "این متن صرفاً پیش‌نمایش است و هیچ پستی در وردپرس ایجاد یا منتشر نشده است.",
    }


def _editorial_points_for_card(
    card: dict,
    evidence_by_id: dict,
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    نکات هر کارت را با همان جهت‌گیریِ ثبت‌شده برای هر شاهد برمی‌گرداند.

    برای technical_notes نباید از جهت‌گیری کلی کارت استفاده شود؛ یک کارت فنی
    می‌تواند هم‌زمان شاهد مثبت و منفی داشته باشد. چنین کارتی «ترکیبی» است،
    نه اینکه هر دو جمله‌اش ناگهان نکته‌ی مثبت شوند.
    """
    positives = []
    negatives = []
    neutral = []

    signal_by_ref = {
        clean_text(str(signal.get("ref_id") or "")): clean_text(
            str(signal.get("sentiment") or "")
        ).lower()
        for signal in card.get("evidence_signals", []) or []
        if isinstance(signal, dict) and signal.get("ref_id")
    }

    for ref_id in card.get("supported_refs", []) or []:
        item = evidence_by_id.get(ref_id)
        if not item:
            continue

        section = clean_text(str(item.get("section") or "")).lower()
        if section == "positives":
            positives.append(item)
        elif section == "negatives":
            negatives.append(item)
        elif section == "technical_notes":
            sentiment = signal_by_ref.get(ref_id, "neutral")
            if sentiment == "positive":
                positives.append(item)
            elif sentiment == "negative":
                negatives.append(item)
            else:
                neutral.append(item)

    return positives[:2], negatives[:2], neutral[:2]


def _editorial_points_text(points: list[dict]) -> str:
    return "؛ ".join(
        clean_text(str(item.get("point_fa") or ""))
        for item in points
        if clean_text(str(item.get("point_fa") or ""))
    )


def _build_grounded_article_blocks(dossier: dict) -> dict:
    """
    بلوک‌های واقعیت‌محورِ کاملاً تأییدشده را می‌سازد؛ این تابع هرگز از OpenAI
    استفاده نمی‌کند. خروجی آن هم پایه‌ی امنِ ورودی برای بازنویسیِ روایی است و
    هم، اگر بازنویسیِ یک بخش رد شود، دقیقاً همان متنی است که در مقاله چاپ
    می‌شود. به همین دلیل، بدترین حالتِ ممکن همیشه همین نسخه‌ی قالب‌محور است.
    """
    fact_pack = _article_fact_pack(dossier)
    meta = fact_pack.get("meta", {}) or {}
    scorecards = fact_pack.get("scorecards", []) or []
    review_sources = dossier.get("review_sources", []) or []

    game = clean_text(str(meta.get("game") or "")) or "بازی"
    overall_score = meta.get("poormaz_score_10")
    metascore = meta.get("metacritic_score_100")
    critic_count = meta.get("critic_review_count")
    platform = clean_text(str(meta.get("platform") or ""))
    release_date = clean_text(str(meta.get("release_date") or ""))
    game_status_label = clean_text(str(meta.get("game_status_label") or ""))
    game_intro_fa = clean_text(str(meta.get("game_intro_fa") or ""))

    evidence_by_id = {
        item.get("id"): item
        for item in fact_pack.get("evidence", [])
        if item.get("id")
    }

    title_fa = f"نقد و بررسی {game} | آیا ارزش خرید دارد؟"
    excerpt_bits = [f"جمع‌بندی Poormaz از نقدهای منتخب {game}"]
    if metascore is not None:
        excerpt_bits.append(f"متاکریتیک {metascore} از ۱۰۰")
    if overall_score is not None:
        excerpt_bits.append(f"امتیاز Poormaz {_format_score_10(overall_score)}")
    excerpt_fa = " | ".join(excerpt_bits) + "."

    glance_lines = []
    if overall_score is not None:
        glance_lines.append(f"- **امتیاز Poormaz:** {_format_score_10(overall_score)}")
    if metascore is not None:
        count_part = f" بر پایه‌ی {critic_count} نقد منتقدان" if critic_count is not None else ""
        glance_lines.append(f"- **متاکریتیک:** {metascore}/100{count_part}")
    if platform:
        glance_lines.append(f"- **پلتفرم پرونده:** {platform}")
    if release_date:
        glance_lines.append(f"- **تاریخ عرضه:** {release_date}")
    if game_status_label:
        glance_lines.append(f"- **وضعیت عرضه:** {game_status_label}")

    # توجه: برخلاف نسخه‌ی قبلی، اینجا هیچ اشاره‌ای به «پوشش شواهد» یا «برداشت
    # اولیه» نمی‌رود — این‌ها برچسب‌های داخلیِ کنترل کیفیت‌اند، نه واقعیتی
    # درباره‌ی خودِ بازی، و نباید در متنِ عمومی به خواننده نشان داده شوند.
    consensus_fa = (
        f"این پرونده از {len(review_sources)} نقد انتخاب‌شده و داده‌ی متاکریتیک ساخته شده است. "
        f"امتیاز تجمیعی Poormaz برای {game} {_format_score_10(overall_score)} است."
    )
    if metascore is not None:
        consensus_fa += f" متاکریتیک ثبت‌شده نیز {metascore}/100 است."
    consensus_sentences = _scorecard_summary_sentences(scorecards)
    if consensus_sentences:
        consensus_fa += " " + " ".join(consensus_sentences)

    scorecard_lines = [
        "| بخش | امتیاز | جهت‌گیری |",
        "|---|---:|---|",
    ]
    for card in scorecards:
        scorecard_lines.append(
            f"| {card.get('label_fa', 'نامشخص')} | "
            f"{_format_score_10(card.get('score_10'))} | "
            f"{card.get('trend_fa', 'نامشخص')} |"
        )

    positives = _collect_article_points(
        review_sources, "positives", ARTICLE_MAX_POINTS_PER_SECTION
    )
    negatives = _collect_article_points(
        review_sources, "negatives", ARTICLE_MAX_POINTS_PER_SECTION
    )
    strengths_fa = _editorial_points_text(positives) or (
        "نکته‌ی مثبتِ به‌اندازه‌ی کافی تأییدشده‌ای برای این بخش در پرونده ثبت نشده است."
    )
    weaknesses_fa = _editorial_points_text(negatives) or (
        "نکته‌ی منفیِ به‌اندازه‌ی کافی تأییدشده‌ای برای این بخش در پرونده ثبت نشده است."
    )

    category_blocks = []
    categorized_refs = set()

    for card in scorecards:
        label = clean_text(str(card.get("label_fa") or "این بخش"))
        card_positives, card_negatives, neutral = _editorial_points_for_card(card, evidence_by_id)
        supports = list(card.get("supported_refs", []) or [])
        categorized_refs.update(supports)

        parts = []
        if card_positives:
            parts.append("نکات مثبت ذکرشده: " + _editorial_points_text(card_positives))
        if card_negatives:
            parts.append("نکات منفی ذکرشده: " + _editorial_points_text(card_negatives))
        if neutral:
            parts.append("نکات فنی ذکرشده: " + _editorial_points_text(neutral))

        text_fa = " ".join(parts).strip()
        if not text_fa:
            continue

        category_blocks.append({
            "key": card.get("key"),
            "label_fa": label,
            "text_fa": text_fa,
        })

    uncategorized_lines = []
    uncategorized = [
        item for ref_id, item in evidence_by_id.items()
        if ref_id not in categorized_refs
    ]
    if uncategorized:
        for item in uncategorized[:ARTICLE_MAX_POINTS_PER_SECTION]:
            point = clean_text(str(item.get("point_fa") or ""))
            if point:
                uncategorized_lines.append("- " + point)

    site_diff_lines = _render_site_differences(review_sources)
    audience_fit_fa = _audience_fit_sentence(scorecards, game)

    # فرمول وزن‌دهی امتیاز شفافیتِ روش‌شناسی است (چیزی که خیلی از سایت‌های نقد
    # هم توضیح می‌دهند)، نه متادیتای داخلیِ کیفیتِ شواهد؛ به همین دلیل در
    # جمع‌بندیِ عمومی می‌ماند، برخلاف برچسب‌های «پوشش شواهد».
    conclusion_fa = (
        f"امتیاز تجمیعی Poormaz برای {game} {_format_score_10(overall_score)} است. "
        + clean_text(str(meta.get("formula_fa") or ""))
    ).strip()

    sources_lines = []
    for source in review_sources:
        site_name = clean_text(str(source.get("site_name") or "منبع نامشخص"))
        title = clean_text(str(source.get("title") or "نقد بازی"))
        url = str(source.get("url") or "").strip()
        score_label = _source_score_label(source)
        if url:
            sources_lines.append(f"- [{site_name}: {title}]({url}) | نمره: {score_label}")
        else:
            sources_lines.append(f"- {site_name}: {title} | نمره: {score_label}")

    allowed_site_names = {
        clean_text(str(source.get("site_name") or ""))
        for source in review_sources
        if clean_text(str(source.get("site_name") or ""))
    }

    return {
        "game": game,
        "title_fa": title_fa,
        "excerpt_fa": excerpt_fa,
        "game_intro_fa": game_intro_fa,
        "glance_lines": glance_lines,
        "consensus_fa": consensus_fa,
        "scorecard_lines": scorecard_lines,
        "strengths_fa": strengths_fa,
        "weaknesses_fa": weaknesses_fa,
        "category_blocks": category_blocks,
        "uncategorized_lines": uncategorized_lines,
        "site_diff_lines": site_diff_lines,
        "audience_fit_fa": audience_fit_fa,
        "conclusion_fa": conclusion_fa,
        "sources_lines": sources_lines,
        "allowed_site_names": allowed_site_names,
        "source_links": fact_pack.get("sources", []),
    }


def build_article_preview(client: OpenAI, dossier: dict) -> dict:
    """
    مقاله در دو لایه ساخته می‌شود:

    لایه‌ی اول (`_build_grounded_article_blocks`) کاملاً قالب‌محور و تأییدشده
    است و هرگز از OpenAI استفاده نمی‌کند.

    لایه‌ی دوم همان متنِ تأییدشده را به نثر روان و شبیه یک نقدِ واقعی
    بازمی‌نویسد (`rewrite_section_as_narrative`). به مدل هیچ اجازه‌ای برای
    افزودن واقعیت، عدد یا نامِ تازه داده نمی‌شود -- فقط اجازه‌ی روان‌نویسی.
    خروجی V5 (نسخه‌ای قدیمی‌تر از این بات) نشان داد که وجود شناسه‌ی منبع
    به‌تنهایی جلوی پیش‌بینی/ادعای تازه‌ی مدل را نمی‌گیرد؛ به همین دلیل اینجا
    به‌جای «تولید آزاد + استناد»، از «بازنویسیِ محدودِ متنِ از قبل درست» استفاده
    می‌شود و هر بازنویسی جداگانه با بررسیِ رانشِ عددی/نام سایت اعتبارسنجی
    می‌شود. اگر رد شود، همان متنِ قالب‌محور بدون تغییر چاپ می‌شود؛ بنابراین
    بدترین حالتِ ممکن دقیقاً همان کیفیتِ نسخه‌ی قبلی است، نه بدتر.
    """
    blocks = _build_grounded_article_blocks(dossier)
    allowed_site_names = blocks["allowed_site_names"]
    game = blocks["game"]
    narrative_methods = {}

    def narrate(section_key: str, section_title_fa: str, text_fa: str) -> str:
        result = rewrite_section_as_narrative(
            client, section_title_fa, text_fa, allowed_site_names, game
        )
        narrative_methods[section_key] = result["method"]
        return result["text_fa"]

    consensus_final = narrate("critic_consensus", "اجماع منتقدان", blocks["consensus_fa"])
    strengths_final = narrate("strengths", "نقاط قوت", blocks["strengths_fa"])
    weaknesses_final = narrate("weaknesses", "نقاط ضعف", blocks["weaknesses_fa"])
    audience_fit_final = narrate(
        "audience_fit", "این بازی برای چه کسانی مناسب است؟", blocks["audience_fit_fa"]
    )

    category_texts = []
    for card in blocks["category_blocks"]:
        section_key = f"category:{card.get('key') or card['label_fa']}"
        final_text = narrate(section_key, card["label_fa"], card["text_fa"])
        category_texts.append({**card, "final_text_fa": final_text})

    markdown = [
        f"# {blocks['title_fa']}",
        "",
        f"> {blocks['excerpt_fa']}",
    ]

    if blocks["game_intro_fa"]:
        markdown.extend(["", "## معرفی بازی", blocks["game_intro_fa"]])

    markdown.extend(["", "## نتیجه در یک نگاه", *blocks["glance_lines"]])
    markdown.extend(["", "## اجماع منتقدان", consensus_final])
    markdown.extend(["", "## کارت امتیاز Poormaz", "", *blocks["scorecard_lines"]])
    markdown.extend(["", "## نقاط قوت", strengths_final])
    markdown.extend(["", "## نقاط ضعف", weaknesses_final])

    markdown.extend(["", "## جزئیات ارزیابی بر اساس بخش‌ها"])
    for card in category_texts:
        markdown.extend(["", f"### {card['label_fa']}", card["final_text_fa"]])

    if blocks["uncategorized_lines"]:
        markdown.extend(["", "## نکات تکمیلی ثبت‌شده", *blocks["uncategorized_lines"]])

    markdown.extend(["", "## تفاوت دیدگاه سایت‌ها", *blocks["site_diff_lines"]])
    markdown.extend(["", "## این بازی برای چه کسانی مناسب است؟", audience_fit_final])
    markdown.extend(["", "## جمع‌بندی Poormaz", blocks["conclusion_fa"]])
    markdown.extend(["", "## منابع بررسی‌شده", *blocks["sources_lines"]])

    markdown.extend([
        "",
        "---",
        "یادداشت تحریریه: این پیش‌نمایش به‌صورت خودکار ساخته شده است. بخش‌های "
        "روایی آن با بازنویسیِ محدود و اعتبارسنجی‌شده‌ی متنِ قالب‌محور نوشته "
        "شده‌اند و هنوز پستی در وردپرس ایجاد یا منتشر نشده است.",
    ])

    rewritten_count = sum(
        1 for method in narrative_methods.values()
        if method == "openai_narrative_rewrite"
    )
    print(
        f"Narrative rewrite: {rewritten_count}/{len(narrative_methods)} sections "
        "written as natural prose; the rest kept the verified template phrasing."
    )

    return {
        "status": "preview_narrative_grounded_v15",
        "wordpress_post_created": False,
        "title_fa": blocks["title_fa"],
        "excerpt_fa": blocks["excerpt_fa"],
        "markdown": "\n".join(markdown).strip() + "\n",
        "source_links": blocks["source_links"],
        "review_note_fa": "این متن صرفاً پیش‌نمایش است و هیچ پستی در وردپرس ایجاد یا منتشر نشده است.",
        "writing_mode": "narrative_rewrite_of_grounded_template",
        "narrative_methods": narrative_methods,
    }


def save_article_preview(game: str, article_preview: dict) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    output_path = os.path.join(
        OUTPUT_DIR,
        f"{safe_filename(game)}-article-preview.md",
    )

    with open(output_path, "w", encoding="utf-8") as file:
        file.write(str(article_preview.get("markdown") or ""))

    return output_path

def safe_filename(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", value or "")
    value = re.sub(r"-+", "-", value).strip("-")

    return value.lower() or "review-dossier"



def _draft_slug(game: str, platform: str) -> str:
    base = safe_filename(f"review-{game}-{platform}")
    return base[:180].strip("-") or "review-draft"


def _review_draft_key(game: str, platform: str) -> str:
    return "|".join([
        safe_filename(game),
        safe_filename(platform),
        "poormaz-review-v1",
    ])


def _markdown_inline_to_html(value: str) -> str:
    """Convert the controlled markdown generated by this bot into safe inline HTML."""
    raw = str(value or "")
    links: list[str] = []

    def stash_link(match: re.Match) -> str:
        label = escape(match.group(1).strip())
        url = match.group(2).strip()
        if not re.match(r"^https?://", url, flags=re.I):
            return match.group(0)
        href = escape(url, quote=True)
        token = f"@@POORMAZ_LINK_{len(links)}@@"
        links.append(
            f'<a href="{href}" target="_blank" rel="nofollow noopener noreferrer">{label}</a>'
        )
        return token

    raw = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", stash_link, raw)
    rendered = escape(raw)
    rendered = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"`([^`]+)`", r"<code>\1</code>", rendered)
    for index, html_link in enumerate(links):
        rendered = rendered.replace(f"@@POORMAZ_LINK_{index}@@", html_link)
    return rendered


def _markdown_to_wp_html(markdown: str) -> str:
    """Minimal, deterministic Markdown-to-HTML converter for the article preview."""
    source = re.sub(
        r"\n---\nیادداشت تحریریه:.*\Z",
        "",
        str(markdown or ""),
        flags=re.S,
    ).strip()
    lines = source.splitlines()
    html_lines: list[str] = []
    paragraph: list[str] = []
    bullets: list[str] = []
    table_rows: list[list[str]] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            body = " ".join(piece.strip() for piece in paragraph if piece.strip())
            if body:
                html_lines.append(f"<p>{_markdown_inline_to_html(body)}</p>")
        paragraph = []

    def flush_bullets() -> None:
        nonlocal bullets
        if bullets:
            html_lines.append("<ul>")
            for item in bullets:
                html_lines.append(f"<li>{_markdown_inline_to_html(item)}</li>")
            html_lines.append("</ul>")
        bullets = []

    def flush_table() -> None:
        nonlocal table_rows
        if len(table_rows) >= 2:
            header = table_rows[0]
            data_rows = table_rows[2:] if len(table_rows) > 2 else []
            html_lines.append('<table class="poormaz-review-scorecard">')
            html_lines.append("<thead><tr>" + "".join(
                f"<th>{_markdown_inline_to_html(cell)}</th>" for cell in header
            ) + "</tr></thead>")
            if data_rows:
                html_lines.append("<tbody>")
                for row in data_rows:
                    html_lines.append("<tr>" + "".join(
                        f"<td>{_markdown_inline_to_html(cell)}</td>" for cell in row
                    ) + "</tr>")
                html_lines.append("</tbody>")
            html_lines.append("</table>")
        table_rows = []

    def flush_all() -> None:
        flush_paragraph()
        flush_bullets()
        flush_table()

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_all()
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            flush_paragraph()
            flush_bullets()
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            table_rows.append(cells)
            continue
        if table_rows:
            flush_table()

        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading:
            flush_all()
            level = len(heading.group(1)) + 1  # h2 for the title; h3/h4 for sections
            level = min(max(level, 2), 4)
            html_lines.append(f"<h{level}>{_markdown_inline_to_html(heading.group(2))}</h{level}>")
            continue

        if stripped == "---":
            flush_all()
            html_lines.append("<hr />")
            continue

        if stripped.startswith("> "):
            flush_all()
            html_lines.append(f"<blockquote><p>{_markdown_inline_to_html(stripped[2:])}</p></blockquote>")
            continue

        if stripped.startswith("- "):
            flush_paragraph()
            bullets.append(stripped[2:].strip())
            continue

        flush_bullets()
        paragraph.append(stripped)

    flush_all()
    return "\n".join(html_lines).strip()


def _wp_headers(json_mode: bool = False) -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if json_mode:
        headers["Content-Type"] = "application/json"
    return headers


def _wp_ready_for_draft() -> tuple[bool, str]:
    if not REVIEW_CREATE_WORDPRESS_DRAFT:
        return False, "draft_not_requested"
    if WP_POST_STATUS != "draft":
        return False, "REVIEW_POST_STATUS must be exactly 'draft'; publishing is blocked"
    missing = [
        name for name, value in {
            "WP_BASE_URL": WP_BASE_URL,
            "WP_USERNAME": WP_USERNAME,
            "WP_APP_PASSWORD": WP_APP_PASSWORD,
        }.items() if not value
    ]
    if missing:
        return False, "missing WordPress configuration: " + ", ".join(missing)
    return True, ""


def _wp_request(method: str, endpoint: str, **kwargs):
    return SESSION.request(
        method.upper(),
        endpoint,
        headers=kwargs.pop("headers", _wp_headers()),
        auth=(WP_USERNAME, WP_APP_PASSWORD),
        timeout=kwargs.pop("timeout", WP_DRAFT_REQUEST_TIMEOUT),
        **kwargs,
    )


def _wp_find_existing_review_draft(slug: str, review_key: str) -> dict:
    endpoint = f"{WP_BASE_URL}/wp-json/wp/v2/posts"
    response = _wp_request(
        "GET",
        endpoint,
        params={"slug": slug, "status": "any", "context": "edit", "per_page": "100"},
    )
    print(f"WP DRAFT DUPLICATE CHECK: {response.status_code} | slug: {slug}")
    if response.status_code >= 400:
        return {
            "ok": False,
            "found": False,
            "reason": f"duplicate check failed with HTTP {response.status_code}",
        }

    for post in response.json() or []:
        content = post.get("content") or {}
        raw_content = str(content.get("raw") or content.get("rendered") or "")
        post_slug = str(post.get("slug") or "")
        if post_slug == slug or f"poormaz-review-key:{review_key}" in raw_content:
            post_id = int(post.get("id"))
            return {
                "ok": True,
                "found": True,
                "post_id": post_id,
                "status": str(post.get("status") or ""),
                "link": str(post.get("link") or ""),
                "edit_url": f"{WP_BASE_URL}/wp-admin/post.php?post={post_id}&action=edit",
            }

    return {"ok": True, "found": False}


def create_wordpress_review_draft(dossier: dict) -> dict:
    allowed, reason = _wp_ready_for_draft()
    if not allowed:
        return {
            "requested": REVIEW_CREATE_WORDPRESS_DRAFT,
            "created": False,
            "status": "not_created",
            "reason": reason,
        }

    article = dossier.get("article_preview") or {}
    title = clean_text(str(article.get("title_fa") or ""))
    markdown = str(article.get("markdown") or "")
    excerpt = clean_text(str(article.get("excerpt_fa") or ""))
    game = clean_text(str(dossier.get("game") or ""))
    platform = clean_text(str(dossier.get("platform") or ""))
    if not title or not markdown or not game or not platform:
        return {
            "requested": True,
            "created": False,
            "status": "not_created",
            "reason": "article preview or review identity is incomplete",
        }

    slug = _draft_slug(game, platform)
    review_key = _review_draft_key(game, platform)
    existing = _wp_find_existing_review_draft(slug, review_key)
    if not existing.get("ok"):
        return {
            "requested": True,
            "created": False,
            "status": "not_created",
            "reason": str(existing.get("reason") or "duplicate check failed"),
        }
    if existing.get("found"):
        return {
            "requested": True,
            "created": False,
            "status": "duplicate_blocked",
            "reason": "an existing review draft/post was found; no overwrite was attempted",
            "post_id": existing.get("post_id"),
            "post_status": existing.get("status"),
            "link": existing.get("link"),
            "edit_url": existing.get("edit_url"),
            "slug": slug,
        }

    content_html = (
        f"<!-- poormaz-review-key:{escape(review_key, quote=True)} -->\n"
        + _markdown_to_wp_html(markdown)
    )
    payload = {
        "title": title,
        "content": content_html,
        "excerpt": excerpt,
        "status": "draft",
        "slug": slug,
    }
    if REVIEW_WP_CATEGORY_ID:
        payload["categories"] = [REVIEW_WP_CATEGORY_ID]

    endpoint = f"{WP_BASE_URL}/wp-json/wp/v2/posts"
    response = _wp_request(
        "POST",
        endpoint,
        headers=_wp_headers(json_mode=True),
        json=payload,
    )
    print(f"WP DRAFT CREATE: {response.status_code}")
    if response.status_code >= 400:
        return {
            "requested": True,
            "created": False,
            "status": "not_created",
            "reason": f"WordPress draft creation failed with HTTP {response.status_code}: {(response.text or '')[:300]}",
            "slug": slug,
        }

    post = response.json() or {}
    post_id = int(post.get("id"))
    return {
        "requested": True,
        "created": True,
        "status": "draft_created",
        "post_id": post_id,
        "post_status": str(post.get("status") or "draft"),
        "link": str(post.get("link") or ""),
        "edit_url": f"{WP_BASE_URL}/wp-admin/post.php?post={post_id}&action=edit",
        "slug": str(post.get("slug") or slug),
        "category_id": REVIEW_WP_CATEGORY_ID or None,
    }


def run_wordpress_draft_regression_checks() -> None:
    assert _draft_slug("Crimson Desert", "PC") == "review-crimson-desert-pc"
    assert "poormaz-review-v1" in _review_draft_key("Crimson Desert", "PC")
    sample = "# Title\n\n- **Bold** [Source](https://example.com/)"
    html = _markdown_to_wp_html(sample)
    assert "<h2>Title</h2>" in html
    assert "<strong>Bold</strong>" in html
    assert 'href="https://example.com/"' in html

def save_dossier(game: str, dossier: dict) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    output_path = os.path.join(
        OUTPUT_DIR,
        f"{safe_filename(game)}-dossier.json",
    )

    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(dossier, file, ensure_ascii=False, indent=2)

    return output_path


def save_incomplete_evidence_report(
    game: str,
    platform: str,
    usable_sources: list[dict],
    thin_sources: list[dict],
    dropped_urls: list[dict],
) -> str:
    """
    وقتی تعداد منابعِ دارای شواهدِ واقعاً قابل‌استفاده کمتر از حد لازم است، هیچ
    مقاله‌ای ساخته نمی‌شود. این گزارش دقیقاً می‌گوید کدام منبع قابل‌استفاده بود،
    کدام‌ها شواهدشان کم بود و چرا، و کدام‌ها اصلاً بارگیری نشدند -- تا سردبیر
    دقیقاً بداند برای تکمیل پرونده باید دنبال چه چیزی بگردد. این گزارش عمداً در
    فایلی جدا از dossier معمولی ذخیره می‌شود تا یک اجرای ناقص، dossierِ سالمِ
    یک اجرای قبلی را پاک نکند.
    """
    def _row(source: dict) -> dict:
        audit = source.get("runtime_quality_audit") or {}
        cache_status = (source.get("evidence_cache") or {}).get("status", "")
        return {
            "site_name": source.get("site_name"),
            "url": source.get("url"),
            "total_evidence_points": audit.get("total_points", 0),
            "categorized_points": audit.get("categorized_points", 0),
            "status": audit.get("status", "unknown"),
            "cache_status": cache_status,
            "reasons_fa": audit.get("reasons_fa", []),
        }

    report = {
        "game": game,
        "platform": platform,
        "status": "incomplete_insufficient_evidence",
        "generated_at_utc": _utc_now_iso(),
        "min_required_usable_sources": REVIEW_MIN_SOURCES,
        "min_verified_points_per_source": EVIDENCE_CACHE_MIN_VERIFIED_POINTS,
        "usable_source_count": len(usable_sources),
        "usable_sources": [_row(source) for source in usable_sources],
        "thin_sources": [_row(source) for source in thin_sources],
        "dropped_urls": dropped_urls,
        "message_fa": (
            f"این پرونده ناقص است: فقط {len(usable_sources)} منبع دارای شواهد "
            f"قابل‌استفاده (حداقل {EVIDENCE_CACHE_MIN_VERIFIED_POINTS} شاهد "
            "دسته‌بندی‌شده) پیدا شد، در حالی که حداقل "
            f"{REVIEW_MIN_SOURCES} منبع لازم است. مقاله‌ای ساخته نشد. لینک‌های "
            "بیشتری از منابع معتبر اضافه کنید یا منابعی که در «thin_sources» یا "
            "«dropped_urls» فهرست شده‌اند را با نسخه‌ی سالم جایگزین کنید."
        ),
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(
        OUTPUT_DIR,
        f"{safe_filename(game)}-INCOMPLETE.json",
    )

    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)

    return output_path


def _source_quality_rows(review_sources: list[dict]) -> list[dict]:
    rows = []

    for source in review_sources:
        # همیشه از شواهد فعلی audit می‌گیریم تا cacheهای قدیمی با برچسب پوشش کهنه
        # گزارش ندهند. این شامل overlay دستیِ همین اجرا هم می‌شود.
        audit = evidence_quality_audit(source)
        cache_meta = source.get("evidence_cache", {}) or {}
        refresh_meta = _refresh_meta(
            audit,
            cache_meta.get("refresh_meta"),
        )
        manual_meta = source.get("manual_evidence", {}) or {}
        manual_applied = manual_meta.get("status") == "applied"

        if manual_applied and audit.get("status") == "acceptable":
            status = "manual_supported"
            action_fa = "پوشش منبع با شاهدهای بررسی‌شده‌ی دستی تکمیل شده است"
        elif refresh_meta.get("manual_review"):
            status = "manual_review"
            action_fa = refresh_meta.get("manual_review_reason_fa") or audit.get(
                "action_fa", ""
            )
        else:
            status = audit.get("status", "needs_review")
            action_fa = audit.get("action_fa", "")

        rows.append(
            {
                "site_name": clean_text(str(source.get("site_name") or "Unknown Source")),
                "status": status,
                "verified_point_count": audit.get("verified_point_count", 0),
                "categorized_point_count": audit.get("categorized_point_count", 0),
                "category_count": audit.get("category_count", 0),
                "coverage_fa": audit.get("coverage_fa", "بدون پوشش دسته‌ای"),
                "reasons_fa": audit.get("reasons_fa", []),
                "action_fa": action_fa,
                "failed_selective_refreshes": refresh_meta.get("failed_selective_refreshes", 0),
                "max_failed_selective_refreshes": refresh_meta.get(
                    "max_failed_selective_refreshes",
                    EVIDENCE_CACHE_MAX_FAILED_SELECTIVE_REFRESHES,
                ),
                "manual_points_added": manual_meta.get("points_added", 0),
                "manual_evidence_applied": manual_applied,
            }
        )

    return rows

def process_review_job(client: OpenAI, item: dict):
    game = str(item["game"]).strip()
    platform = str(item["platform"]).strip()
    refresh_all_evidence = item_refresh_requested(item)
    refresh_incomplete_evidence = (
        item_incomplete_refresh_requested(item) and not refresh_all_evidence
    )

    game_status_info = normalize_game_status(item.get("game_status"))

    print("\n" + "=" * 72)
    print(f"ANALYZING: {game} | {platform}")
    print(f"Game status: {game_status_info['label_fa']}")
    if not game_status_info["recognized"]:
        print(
            "Note: game_status value was not recognized from the known list; "
            "using it verbatim as the label."
        )

    if refresh_all_evidence:
        print("Evidence cache: FULL REFRESH forced for this job")
    elif refresh_incomplete_evidence:
        print("Evidence cache: SELECTIVE QUALITY REFRESH enabled")
    elif REVIEW_EVIDENCE_CACHE_ENABLED:
        print("Evidence cache: enabled")
    else:
        print("Evidence cache: disabled")

    metacritic_page = extract_page_info(item["metacritic_url"])

    if not metacritic_page["ok"]:
        print(
            f"SKIP: Metacritic unavailable "
            f"({metacritic_page['status']})"
        )
        return

    metacritic_data = extract_metacritic_data(
        metacritic_page,
        platform,
    )

    source_analyses = []
    dropped_urls = []

    for url in item["review_urls"]:
        requested_url = str(url).strip()
        cached_analysis = None
        cache_info = {"status": "miss"}

        if not refresh_all_evidence:
            cached_analysis, cache_info = load_evidence_cache(
                game,
                platform,
                requested_url,
            )

        cached_audit = (cache_info.get("quality_audit") or {})
        cached_refresh_meta = _refresh_meta(
            cached_audit,
            cache_info.get("refresh_meta"),
        )
        cache_info["refresh_meta"] = cached_refresh_meta
        source_is_manual_review = bool(cached_refresh_meta.get("manual_review"))
        refresh_this_source = (
            cached_analysis is not None
            and refresh_incomplete_evidence
            and cached_audit.get("status") == "needs_review"
            and not source_is_manual_review
        )

        if cached_analysis is not None and not refresh_this_source:
            output = attach_runtime_evidence(
                cached_analysis, cache_info, game, platform, requested_url
            )
            source_analyses.append(output)

            quality_status = (output.get("evidence_cache", {}) or {}).get(
                "quality_audit", {}
            ).get("status", "unknown")
            if source_is_manual_review:
                print(
                    f"Evidence cache: MANUAL REVIEW | "
                    f"{cached_analysis.get('site_name', 'Unknown Source')} "
                    f"| OpenAI skipped | retries exhausted"
                )
            else:
                quality_note = f" | quality: {quality_status}"
                print(
                    f"Evidence cache: HIT | "
                    f"{cached_analysis.get('site_name', 'Unknown Source')} "
                    f"| OpenAI skipped{quality_note}"
                )
            continue

        if refresh_all_evidence:
            print(f"Evidence cache: FULL REFRESH | {requested_url}")
        elif refresh_this_source:
            reason_text = "; ".join(cached_audit.get("reasons_fa", []))
            print(
                f"Evidence cache: QUALITY REFRESH | "
                f"{cached_analysis.get('site_name', requested_url)}"
                f" | {reason_text or 'cache needs review'}"
            )
        elif cache_info.get("status") not in {"miss", "disabled"}:
            print(
                f"Evidence cache: {cache_info.get('status', 'miss').upper()} "
                f"| {requested_url}"
            )

        page = extract_page_info(requested_url)

        if not page["ok"]:
            print(f"Skipped source: {requested_url} | HTTP {page['status']}")
            if cached_analysis is not None:
                cache_info["status"] = "retained_http_error"
                source_analyses.append(
                    attach_runtime_evidence(
                        cached_analysis, cache_info, game, platform, requested_url
                    )
                )
            else:
                dropped_urls.append({
                    "url": requested_url,
                    "reason_fa": f"دریافت صفحه ناموفق بود (HTTP {page['status']}) و نسخه‌ی cache‌شده‌ای هم در دسترس نبود.",
                })
            continue

        print(f"Loaded: {page['site_name']} | {page['title'][:80]}")
        detected = extract_review_score(page)

        print(
            f"Score scan: {page['site_name']} | "
            f"{detected['original_score'] or 'not found'} | "
            f"{detected['score_method']}"
        )
        print(f"Analyzing with OpenAI: {page['site_name']}")

        candidate_analysis = analyze_review_source(
            client,
            game,
            platform,
            page,
            recovery_mode=refresh_this_source,
        )

        # Refresh انتخابی یک جایگزینی کور نیست: شواهد تازه با cache قبلی ادغام می‌شوند.
        # فقط اگر پوشش بهتر شود، نسخه‌ی ادغام‌شده جایگزین می‌گردد.
        refresh_meta_for_save = None
        if refresh_this_source and cached_analysis is not None:
            merged_analysis = merge_cached_and_candidate_analysis(
                cached_analysis,
                candidate_analysis,
            )
            old_audit = evidence_quality_audit(cached_analysis)
            merged_audit = evidence_quality_audit(merged_analysis)
            improved = merged_audit["quality_rank"] > old_audit["quality_rank"]
            refresh_meta_for_save = _after_selective_refresh(
                cached_refresh_meta,
                merged_audit if improved else old_audit,
                improved=improved,
            )

            if not improved:
                # حتی وقتی استخراج تازه مفید نبود، شمارش تلاش‌ها و manual-review باید پایدار شود.
                if REVIEW_EVIDENCE_CACHE_ENABLED:
                    cache_info = save_evidence_cache(
                        game,
                        platform,
                        requested_url,
                        page,
                        cached_analysis,
                        refresh_meta=refresh_meta_for_save,
                    )
                    cache_info["status"] = (
                        "manual_review"
                        if refresh_meta_for_save.get("manual_review")
                        else "retained_not_stronger"
                    )
                else:
                    cache_info = {
                        "status": "retained_not_stronger",
                        "quality_audit": old_audit,
                        "refresh_meta": refresh_meta_for_save,
                    }

                source_analyses.append(
                    attach_runtime_evidence(
                        cached_analysis, cache_info, game, platform, requested_url
                    )
                )
                if refresh_meta_for_save.get("manual_review"):
                    print(
                        "Evidence cache: MANUAL REVIEW | new extraction added no new "
                        "coverage; automatic retries stopped"
                    )
                else:
                    print(
                        "Evidence cache: RETAINED | new extraction added no new "
                        "coverage"
                    )
                continue

            analysis = merged_analysis
        else:
            analysis = candidate_analysis

        if REVIEW_EVIDENCE_CACHE_ENABLED:
            cache_info = save_evidence_cache(
                game,
                platform,
                requested_url,
                page,
                analysis,
                refresh_meta=refresh_meta_for_save,
            )
            if refresh_all_evidence:
                cache_info["status"] = "refreshed"
                print(f"Evidence cache: REFRESHED | {analysis['site_name']}")
            elif refresh_this_source:
                cache_info["status"] = "merged_refreshed"
                print(f"Evidence cache: MERGED REFRESH | {analysis['site_name']}")
            else:
                print(f"Evidence cache: SAVED | {analysis['site_name']}")
        else:
            cache_info = {
                "status": "disabled",
                "quality_audit": evidence_quality_audit(analysis),
                "refresh_meta": _refresh_meta(evidence_quality_audit(analysis)),
            }

        source_analyses.append(
            attach_runtime_evidence(
                analysis, cache_info, game, platform, requested_url
            )
        )

    usable_sources = [
        source for source in source_analyses
        if (source.get("runtime_quality_audit") or {}).get("status") == "acceptable"
    ]
    thin_sources = [
        source for source in source_analyses
        if (source.get("runtime_quality_audit") or {}).get("status") != "acceptable"
    ]

    # نکته‌ی کلیدی: شمارش صرفِ len(source_analyses) کافی نیست، چون یک منبع
    # می‌تواند بارگیری شود ولی صفر یا فقط یک شاهد واقعی بدهد (مثلاً صفحه‌ای که
    # هیچ نقدی در متنش نبود، یا cacheِ manual_review که هرگز بهتر نشد). فقط
    # منابعی که واقعاً شاهدِ دسته‌بندی‌شده‌ی کافی دارند («acceptable») به حداقل
    # {REVIEW_MIN_SOURCES} شمرده می‌شوند.
    if len(usable_sources) < REVIEW_MIN_SOURCES:
        print(
            f"INCOMPLETE: only {len(usable_sources)} of {len(source_analyses)} "
            f"loaded source(s) have acceptable, categorizable evidence "
            f"(need at least {REVIEW_MIN_SOURCES}). No article will be generated "
            "for this run."
        )
        for source in usable_sources:
            print(f"  OK      | {source.get('site_name')} | {source.get('url')}")
        for source in thin_sources:
            audit = source.get("runtime_quality_audit") or {}
            reasons = "؛ ".join(audit.get("reasons_fa", [])) or "شواهد کافی ثبت نشد"
            cache_status = (source.get("evidence_cache") or {}).get("status", "")
            print(
                f"  THIN    | {source.get('site_name')} | {source.get('url')} "
                f"| {reasons} | cache: {cache_status}"
            )
        for dropped in dropped_urls:
            print(f"  DROPPED | {dropped['url']} | {dropped['reason_fa']}")

        report_path = save_incomplete_evidence_report(
            game, platform, usable_sources, thin_sources, dropped_urls
        )
        print(f"Incomplete-evidence report saved to: {report_path}")
        return

    quality_rows = _source_quality_rows(source_analyses)
    needs_review_rows = [
        row for row in quality_rows if row.get("status") == "needs_review"
    ]
    manual_review_rows = [
        row for row in quality_rows if row.get("status") == "manual_review"
    ]

    dossier = {
        "game": game,
        "platform": platform,
        "release_date": str(item.get("release_date") or ""),
        "game_status": game_status_info,
        "metacritic": metacritic_data,
        "review_sources": source_analyses,
        "evidence_cache": {
            "enabled": REVIEW_EVIDENCE_CACHE_ENABLED,
            "full_refresh_requested": refresh_all_evidence,
            "selective_quality_refresh_requested": refresh_incomplete_evidence,
            "version": EVIDENCE_CACHE_VERSION,
            "min_verified_points": EVIDENCE_CACHE_MIN_VERIFIED_POINTS,
            "max_failed_selective_refreshes": EVIDENCE_CACHE_MAX_FAILED_SELECTIVE_REFRESHES,
            "manual_evidence_file": os.path.basename(MANUAL_EVIDENCE_FILE),
            "manual_evidence_policy_fa": (
                "شواهد دستی فقط برای منبع و بازیِ دقیقِ ثبت‌شده در manual_evidence.yaml "
                "اعمال می‌شوند؛ cache خودکار و OpenAI را تغییر نمی‌دهند."
            ),
            "quality_audit": quality_rows,
            "policy_fa": (
                "شواهد هر منبع پس از اولین تحلیل ذخیره می‌شوند. بازخوانی انتخابی "
                "خروجی تازه را با cache قبلی ادغام می‌کند و فقط در صورت بهتر شدن "
                "پوشش شواهد ذخیره می‌شود. منبعی که پس از چند کوشش ناموفق همچنان "
                "ضعیف بماند، برای بازبینی دستی علامت می‌خورد و دیگر خودکار تحلیل نمی‌شود."
            ),
        },
        "status": "analysis_only",
        "wordpress_post_created": False,
    }

    dossier["reputable_coverage"] = reputable_coverage_report(source_analyses)
    print(f"Reputable site coverage: {dossier['reputable_coverage']['note_fa']}")

    print("Writing short factual game introduction...")
    dossier["game_intro"] = build_game_intro(
        client,
        game,
        platform,
        dossier["release_date"],
        game_status_info["label_fa"],
        metacritic_page.get("description", ""),
    )

    print("Building Poormaz scorecard from verified evidence...")
    dossier["poormaz_assessment"] = build_poormaz_assessment(
        client,
        dossier,
    )

    print("Building Persian article preview from verified evidence...")
    dossier["article_preview"] = build_article_preview(client, dossier)
    article_preview_path = save_article_preview(
        game,
        dossier["article_preview"],
    )
    dossier["article_preview"]["markdown_file"] = os.path.basename(
        article_preview_path
    )

    wordpress_draft = create_wordpress_review_draft(dossier)
    dossier["wordpress_draft"] = wordpress_draft
    dossier["wordpress_post_created"] = bool(wordpress_draft.get("created"))
    if wordpress_draft.get("created"):
        dossier["status"] = "wordpress_draft_created"
    elif wordpress_draft.get("status") == "duplicate_blocked":
        dossier["status"] = "wordpress_draft_duplicate_blocked"

    output_path = save_dossier(game, dossier)

    print("\n--- REVIEW DOSSIER SUMMARY ---")
    print(f"Game: {game}")
    print(f"Game status: {game_status_info['label_fa']}")
    print(f"Metascore: {metacritic_data['metascore_100']}")
    print(f"Review count: {metacritic_data['critic_review_count']}")

    for source in source_analyses:
        cache_meta = source.get("evidence_cache", {}) or {}
        cache_status = cache_meta.get("status", "")
        cache_suffix = f" | cache: {cache_status}" if cache_status else ""
        print(
            f"- {source['site_name']}: "
            f"{source['original_score'] or 'No deterministic score found'} "
            f"[{source['score_method']}]{cache_suffix}"
        )

    coverage = dossier.get("reputable_coverage", {}) or {}
    print("\n--- REPUTABLE SITE COVERAGE ---")
    print(
        "Covered core sites: "
        + (", ".join(coverage.get("covered_core_sites", [])) or "none")
    )
    if coverage.get("missing_core_sites"):
        print("Missing core sites: " + ", ".join(coverage["missing_core_sites"]))
    if coverage.get("other_sources"):
        print("Other sources used: " + ", ".join(coverage["other_sources"]))

    print("\n--- EVIDENCE CACHE QUALITY ---")
    for row in quality_rows:
        reasons = "; ".join(row.get("reasons_fa", []))
        suffix = f" | {reasons}" if reasons else ""
        retry_text = (
            f" | failed refreshes: {row.get('failed_selective_refreshes', 0)}/"
            f"{row.get('max_failed_selective_refreshes', EVIDENCE_CACHE_MAX_FAILED_SELECTIVE_REFRESHES)}"
        )
        manual_text = (
            f" | manual points: +{row.get('manual_points_added', 0)}"
            if row.get("manual_evidence_applied")
            else ""
        )
        print(
            f"- {row['site_name']}: {row['status']} | "
            f"verified points: {row['verified_point_count']}"
            f" | categorized: {row['categorized_point_count']}"
            f" | coverage: {row.get('coverage_fa', 'نامشخص')}"
            f"{manual_text}{retry_text}{suffix}"
        )

    if needs_review_rows:
        print(
            "Quality action: rerun with refresh_incomplete_evidence=true "
            "to re-extract only the sources above."
        )
    elif manual_review_rows:
        names = ", ".join(row["site_name"] for row in manual_review_rows)
        print(
            "Quality action: manual review required for: " + names + ". "
            "Automatic retries are disabled for these sources."
        )
    else:
        print("Quality action: all cached sources meet the current threshold.")

    assessment = dossier.get("poormaz_assessment", {})
    print(
        "Poormaz aggregate score: "
        f"{assessment.get('overall_score_10')}"
    )

    for category in assessment.get("scorecard", []):
        print(
            f"  * {category['label_fa']}: "
            f"{category['score_10']}/10 | "
            f"{category.get('trend_fa', 'نامشخص')} | "
            f"{category.get('confidence_fa', 'محدود')}"
        )

    article_preview = dossier.get("article_preview", {}) or {}
    print("\n--- ARTICLE PREVIEW ---")
    print(article_preview.get("title_fa", "پیش‌نمایش مقاله ساخته نشد."))
    print(f"Preview file: {article_preview.get('markdown_file', '')}")

    print("\n--- WORDPRESS DRAFT ---")
    draft_status = wordpress_draft.get("status", "not_created")
    if wordpress_draft.get("created"):
        print(f"Draft created: ID {wordpress_draft.get('post_id')}")
        print(f"Edit draft: {wordpress_draft.get('edit_url')}")
        if wordpress_draft.get("link"):
            print(f"Draft link: {wordpress_draft.get('link')}")
    elif draft_status == "duplicate_blocked":
        print("Draft creation blocked: a matching post already exists.")
        print(f"Existing draft edit link: {wordpress_draft.get('edit_url', '')}")
    else:
        print("Draft not created.")
        print(f"Reason: {wordpress_draft.get('reason', '')}")

    print(f"\nSaved: {output_path}")
    print("\n--- REVIEW DOSSIER JSON ---")
    print(json.dumps(dossier, ensure_ascii=False, indent=2))

def main():
    print("=== Poormaz Review Bot: Verified Dossier Builder ===")
    run_rule_based_regression_checks()
    print("Rule-based regression checks: passed")
    run_evidence_cache_regression_checks()
    print("Evidence cache regression checks: passed")
    run_wordpress_draft_regression_checks()
    print("WordPress draft regression checks: passed")
    run_metadata_regression_checks()
    print("Metadata regression checks (game status, reputable sites): passed")

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
