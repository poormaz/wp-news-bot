import os
import re
import sys
import json
import yaml
import hashlib
import math
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

# پروتکل جدید شواهد: مدل فقط شناسه‌ی جمله‌های آماده را انتخاب می‌کند؛
# بنابراین شاهد انگلیسی هر نکته مستقیماً از متن صفحه می‌آید و نه از بازنویسی مدل.
EVIDENCE_PROTOCOL_VERSION = "excerpt_ids_v2"
EVIDENCE_MIN_WORDS = 8
EVIDENCE_MAX_WORDS = 26

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
            "analysis_protocol": clean_text(str(analysis.get("analysis_protocol") or "")),
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
        "analysis_protocol": info.get("analysis_protocol", ""),
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
            r"(?is)\bGaming\s+(?:Score\s*)?(\d{1,2}(?:\.\d+)?)\b",
            r"(?is)\bScore\s*:?\s*(\d{1,2}(?:\.\d+)?)\b.{0,120}?\bGaming\b",
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


def _evidence_word_count(value: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", clean_text(str(value or ""))))


def _has_broken_character(value: str) -> bool:
    return "\ufffd" in str(value or "")


def _split_into_evidence_sentences(text: str) -> list[str]:
    """
    متن خام نقد را به قطعه‌های کوتاه و قابل‌استناد تبدیل می‌کند. فقط جمله‌هایی
    نگه داشته می‌شوند که طولشان برای شاهد دقیق مناسب باشد. مدل هیچ‌وقت متن
    انگلیسی را خودش نمی‌نویسد؛ فقط از این فهرست شناسه انتخاب می‌کند.
    """
    cleaned = clean_text(str(text or ""))
    if not cleaned:
        return []

    chunks = re.split(r"(?<=[.!?])\s+|(?<=;)\s+", cleaned)
    output = []
    seen = set()

    for chunk in chunks:
        chunk = clean_text(chunk).strip(" -–—")
        if not chunk or _has_broken_character(chunk):
            continue

        words = _evidence_word_count(chunk)
        if not (EVIDENCE_MIN_WORDS <= words <= EVIDENCE_MAX_WORDS):
            continue

        key = chunk.casefold()
        if key in seen:
            continue

        seen.add(key)
        output.append(chunk)

    return output


def build_evidence_excerpt_catalog(page: dict, max_items: int = 150) -> dict[str, str]:
    """
    کاتالوگ شماره‌دار جمله‌های قابل‌استناد. عنوان و توضیح متا نیز فقط وقتی
    وارد می‌شوند که واقعاً جمله‌ای با طول مناسب باشند، تا سایت‌هایی که متن
    اصلی‌شان ناقص استخراج می‌شود هم یک فرصت منصفانه داشته باشند.
    """
    candidates = []

    for value in (
        page.get("title", ""),
        page.get("description", ""),
        page.get("text", ""),
    ):
        candidates.extend(_split_into_evidence_sentences(value))

    catalog: dict[str, str] = {}
    seen = set()

    for candidate in candidates:
        key = candidate.casefold()
        if key in seen:
            continue
        seen.add(key)

        evidence_id = f"E{len(catalog) + 1:03d}"
        catalog[evidence_id] = candidate

        if len(catalog) >= max_items:
            break

    return catalog


def _catalog_prompt_text(catalog: dict[str, str]) -> str:
    return "\n".join(f"{evidence_id}: {quote}" for evidence_id, quote in catalog.items())


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    lowered = clean_text(text).casefold()
    return any(re.search(pattern, lowered, re.I) for pattern in patterns)


def _point_claim_is_compatible(point_fa: str, evidence_en: str) -> bool:
    """
    یک نگهبان سبک‌وزن برای خطاهای واضح در ترجمه/برداشت مدل. این جای فهم انسانی
    را نمی‌گیرد، اما جلوی جهش‌هایی مثل «نبود صندوق ذخیره» به «تعادل نبرد» یا
    «باگ» به «اتلاف وقت بازیکن» را می‌گیرد.
    """
    fa = clean_text(point_fa)
    en = clean_text(evidence_en).casefold()

    if not fa or _has_broken_character(fa) or _has_broken_character(en):
        return False

    domains = (
        (("نبرد", "مبارزه", "کامبت", "باس"), (r"\bcombat\b", r"\bbattle", r"\bfight", r"\bboss", r"\bweapon")),
        (("داستان", "روایت", "شخصیت", "دیالوگ"), (r"\bstory\b", r"\bnarrative\b", r"\bplot\b", r"\bcharacter", r"\bdialogue\b", r"\bwriting\b", r"\bquest")),
        (("عملکرد", "فنی", "فریم", "بهینه", "باگ", "کرش"), (r"\bperformance\b", r"\bframe", r"\bbug", r"\bcrash", r"\bstutter", r"\btechnical\b", r"\boptimization")),
        (("ذخیره", "انبار", "صندوق"), (r"\bstorage\b", r"\binventory\b", r"\bchest")),
        (("پازل",), (r"\bpuzzle\b",)),
        (("جهان باز", "محیط", "اکتشاف", "دنیا"), (r"\bopen[- ]world\b", r"\bworld\b", r"\benvironment", r"\bexplor")),
    )

    for fa_terms, en_patterns in domains:
        # حتی اگر در نسخه‌های بعدی یک tuple تک‌عضوی اشتباهاً بدون comma نوشته شود،
        # رشته را یک الگوی واحد در نظر می‌گیریم، نه فهرستی از کاراکترها.
        if isinstance(en_patterns, str):
            en_patterns = (en_patterns,)

        if any(term in fa for term in fa_terms):
            if not any(re.search(pattern, en, re.I) for pattern in en_patterns):
                return False

    if any(phrase in fa for phrase in ("زمان بازیکن", "احترام نمی‌گذارد")):
        if not re.search(r"\btime\b|\brespect", en, re.I):
            return False

    return True


def _selected_catalog_points(
    raw_items,
    catalog: dict[str, str],
    section: str,
    used_ids: set[str],
    used_quotes: set[str],
    max_items: int,
) -> list[dict]:
    if not isinstance(raw_items, list):
        return []

    output = []

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        evidence_id = clean_text(str(item.get("evidence_id") or "")).upper()
        point_fa = clean_text(str(item.get("point_fa") or ""))
        evidence_en = catalog.get(evidence_id, "")

        if not evidence_id or not point_fa or not evidence_en:
            continue
        if evidence_id in used_ids:
            continue
        if len(point_fa.split()) < 2 or len(point_fa.split()) > 30:
            continue
        if not (EVIDENCE_MIN_WORDS <= _evidence_word_count(evidence_en) <= EVIDENCE_MAX_WORDS):
            continue
        if not _point_claim_is_compatible(point_fa, evidence_en):
            continue

        quote_key = evidence_en.casefold()
        if quote_key in used_quotes:
            continue

        # یادداشت فنی فقط باید واقعاً پشتوانه‌ی فنی داشته باشد.
        if section == "technical_notes" and not _matches_any(
            evidence_en, CATEGORY_PATTERNS["technical"]
        ):
            continue

        used_ids.add(evidence_id)
        used_quotes.add(quote_key)
        output.append(
            {
                "point_fa": point_fa,
                "evidence_en": evidence_en,
                "evidence_id": evidence_id,
            }
        )

        if len(output) >= max_items:
            break

    return output


def _legacy_point_is_clean(point: dict) -> bool:
    if not isinstance(point, dict):
        return False

    point_fa = clean_text(str(point.get("point_fa") or ""))
    evidence_en = clean_text(str(point.get("evidence_en") or ""))

    if not point_fa or not evidence_en:
        return False
    if _has_broken_character(point_fa) or _has_broken_character(evidence_en):
        return False
    if not (EVIDENCE_MIN_WORDS <= _evidence_word_count(evidence_en) <= EVIDENCE_MAX_WORDS):
        return False
    return _point_claim_is_compatible(point_fa, evidence_en)


def analysis_requires_protocol_upgrade(analysis: dict) -> bool:
    """
    فقط cacheهای واقعاً ناسالم به پروتکل شناسه‌ای ارتقا می‌گیرند. منابع قدیمیِ
    تمیز دوباره هزینه‌ی OpenAI ایجاد نمی‌کنند.
    """
    if not isinstance(analysis, dict):
        return False

    seen_quotes: dict[str, str] = {}

    for section in ("positives", "negatives", "technical_notes"):
        for point in analysis.get(section, []) or []:
            if not _legacy_point_is_clean(point):
                return True

            quote_key = clean_text(str(point.get("evidence_en") or "")).casefold()
            previous_section = seen_quotes.get(quote_key)
            if previous_section and previous_section != section:
                return True
            seen_quotes[quote_key] = section

    return False


def _clean_legacy_analysis_for_upgrade(analysis: dict) -> dict:
    output = _json_copy(analysis or {})
    used_quotes = set()

    for section in ("positives", "negatives", "technical_notes"):
        clean_points = []

        for point in output.get(section, []) or []:
            if not _legacy_point_is_clean(point):
                continue

            quote_key = clean_text(str(point.get("evidence_en") or "")).casefold()
            if quote_key in used_quotes:
                continue

            used_quotes.add(quote_key)
            clean_points.append(
                {
                    "point_fa": clean_text(str(point.get("point_fa") or "")),
                    "evidence_en": clean_text(str(point.get("evidence_en") or "")),
                }
            )

        output[section] = clean_points[:EVIDENCE_CACHE_MAX_MERGED_POINTS_PER_SECTION]

    verdict = []
    for point in output.get("verdict", []) or []:
        if _legacy_point_is_clean(point):
            verdict = [{
                "point_fa": clean_text(str(point.get("point_fa") or "")),
                "evidence_en": clean_text(str(point.get("evidence_en") or "")),
            }]
            break
    output["verdict"] = verdict
    return output


def merge_protocol_upgrade_analysis(cached_analysis: dict, candidate_analysis: dict) -> dict:
    """
    برخلاف refresh عادی، ارتقای پروتکل نباید شاهدهای ناسالم قدیمی را حفظ کند.
    ابتدا خروجی شناسه‌ای تازه و سپس فقط نکات سالم cache قبلی نگه داشته می‌شوند.
    """
    cleaned_cached = _clean_legacy_analysis_for_upgrade(cached_analysis)
    merged = _json_copy(candidate_analysis or {})

    for key in (
        "site_name", "title", "url", "original_score", "review_score_10",
        "score_method", "score_confidence", "score_evidence", "platform_mentioned",
    ):
        if not clean_text(str(merged.get(key) or "")):
            merged[key] = _json_copy(cleaned_cached.get(key))

    used_quotes = set()
    for section in ("positives", "negatives", "technical_notes"):
        candidates = list(merged.get(section, []) or []) + list(
            cleaned_cached.get(section, []) or []
        )
        filtered = []

        for point in candidates:
            if not _legacy_point_is_clean(point):
                continue
            quote_key = clean_text(str(point.get("evidence_en") or "")).casefold()
            if quote_key in used_quotes:
                continue
            used_quotes.add(quote_key)
            filtered.append(point)

            if len(filtered) >= EVIDENCE_CACHE_MAX_MERGED_POINTS_PER_SECTION:
                break

        merged[section] = filtered

    merged["verdict"] = (
        list(candidate_analysis.get("verdict", []) or [])[:1]
        or list(cleaned_cached.get("verdict", []) or [])[:1]
    )
    merged["analysis_protocol"] = EVIDENCE_PROTOCOL_VERSION
    return merged


def verified_points(items, source_text: str, max_items: int = 4) -> list[dict]:
    """
    مسیر سازگاری برای cacheهای قدیمی. خروجی جدید از شناسه‌ی جمله استفاده می‌کند،
    اما این تابع هنوز برای داده‌های قدیمی و تست‌ها تنها نکات تمیز را می‌پذیرد.
    """
    if not isinstance(items, list):
        return []

    output = []
    seen = set()

    for item in items:
        if not _legacy_point_is_clean(item):
            continue

        quote_key = clean_text(str(item.get("evidence_en") or "")).casefold()
        if quote_key in seen:
            continue

        seen.add(quote_key)
        output.append(
            {
                "point_fa": clean_text(str(item.get("point_fa") or "")),
                "evidence_en": clean_text(str(item.get("evidence_en") or "")),
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
    """
    مدل فقط شناسه‌ی شاهد را برمی‌گرداند. نگاشت شناسه به جمله انگلیسی در همین
    تابع و بدون دخالت مدل انجام می‌شود؛ بنابراین نقل‌قول بلند، جعلی یا تکراری
    وارد cache و مقاله نمی‌شود.
    """
    score = extract_review_score(page)
    catalog = build_evidence_excerpt_catalog(page)

    if not catalog:
        return {
            "site_name": page["site_name"],
            "title": page["title"],
            "url": page["url"],
            **score,
            "analysis_protocol": EVIDENCE_PROTOCOL_VERSION,
            "positives": [],
            "negatives": [],
            "technical_notes": [],
            "verdict": [],
            "platform_mentioned": None,
        }

    prompt = f"""
You are selecting evidence-backed editorial themes from one professional game review.

Game: {game}
Requested platform: {platform}
Website: {page["site_name"]}
Review title: {page["title"]}
Review URL: {page["url"]}

The deterministic review score is:
- original_score: {score["original_score"]}
- review_score_10: {score["review_score_10"]}

Below is a numbered catalog of exact English excerpts from this review. You MUST
select only IDs from this catalog. Never write or edit an English quote.

{_catalog_prompt_text(catalog)}

Rules:
- Use only the catalog. Do not infer facts not stated by the selected excerpt.
- Each selected ID may appear ONCE across positives, negatives, technical_notes,
  and verdict. Do not reuse the same ID with two opposing labels.
- Every point_fa is ONE concise Persian sentence, with one claim only.
- Do not broaden the claim. For example, "there are no storage chests" may only
  support a statement about storage/inventory, not combat balance or save systems.
- A point about bugs, performance, crashes, UI, controls or optimization belongs
  in technical_notes only when the selected excerpt explicitly mentions it.
- Do not use a title as evidence unless it itself directly states the claim.
- Return at most 3 positives, 3 negatives, 2 technical_notes, and 1 verdict.
- Omit any category with no direct support.
- platform_mentioned must be a platform explicitly named in the supplied excerpts
  or null. Do not guess.

Return strict JSON:
{{
  "positives": [{{"point_fa": "...", "evidence_id": "E001"}}],
  "negatives": [{{"point_fa": "...", "evidence_id": "E002"}}],
  "technical_notes": [{{"point_fa": "...", "evidence_id": "E003"}}],
  "verdict": {{"point_fa": "...", "evidence_id": "E004"}} or null,
  "platform_mentioned": "..." or null
}}

Extraction mode: {"protocol upgrade: inspect the catalog carefully because prior cache contained invalid or duplicated evidence" if recovery_mode else "normal"}
""".strip()

    raw = ask_openai_json(client, prompt, max_tokens=1500)
    used_ids: set[str] = set()
    used_quotes: set[str] = set()

    positives = _selected_catalog_points(
        raw.get("positives"), catalog, "positives", used_ids, used_quotes, 3
    )
    negatives = _selected_catalog_points(
        raw.get("negatives"), catalog, "negatives", used_ids, used_quotes, 3
    )
    technical_notes = _selected_catalog_points(
        raw.get("technical_notes"), catalog, "technical_notes", used_ids, used_quotes, 2
    )

    raw_verdict = raw.get("verdict")
    verdict = _selected_catalog_points(
        [raw_verdict] if isinstance(raw_verdict, dict) else [],
        catalog,
        "verdict",
        used_ids,
        used_quotes,
        1,
    )

    return {
        "site_name": page["site_name"],
        "title": page["title"],
        "url": page["url"],
        **score,
        "analysis_protocol": EVIDENCE_PROTOCOL_VERSION,
        "positives": positives,
        "negatives": negatives,
        "technical_notes": technical_notes,
        "verdict": verdict,
        "platform_mentioned": clean_text(
            str(raw.get("platform_mentioned") or "")
        ) or None,
    }


def run_evidence_protocol_regression_checks() -> None:
    catalog = {
        "E001": "The inventory system offers no storage chests for collected gear.",
        "E002": "Combat is outstanding and rewards careful use of its many tools.",
    }
    selected = _selected_catalog_points(
        [
            {
                "point_fa": "سیستم انبارداری بازی صندوقی برای نگهداری تجهیزات جمع‌آوری‌شده ندارد.",
                "evidence_id": "E001",
            },
            {
                "point_fa": "تعادل نبردها مشکل دارد.",
                "evidence_id": "E001",
            },
        ],
        catalog,
        "negatives",
        set(),
        set(),
        3,
    )
    assert len(selected) == 1, "یک شاهد نباید دو بار یا با ادعای نامرتبط پذیرفته شود"
    assert selected[0]["evidence_id"] == "E001"
    assert _point_claim_is_compatible(
        "پازل‌های بازی آزاردهنده هستند.",
        "The puzzle design is confusing and frustrating for many players.",
    ), "الگوی تک‌عضوی پازل نباید به رشته‌ی کاراکترها تبدیل شود"

    broken = {
        "positives": [{
            "point_fa": "یک نکته‌ی خیلی کلی ثبت شده است.",
            "evidence_en": "This sentence has far too many words to be accepted as a concise, direct, and reliable evidence quote in the review dossier.",
        }],
        "negatives": [{
            "point_fa": "همان نکته به‌اشتباه منفی هم ثبت شده است.",
            "evidence_en": "This sentence has far too many words to be accepted as a concise, direct, and reliable evidence quote in the review dossier.",
        }],
        "technical_notes": [],
    }
    assert analysis_requires_protocol_upgrade(broken)

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


def _floor_to_half(value) -> float | None:
    """عدد را همیشه رو به پایین به نزدیک‌ترین نیم‌نمره می‌برد."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    numeric = min(max(numeric, 1.0), 10.0)
    return math.floor((numeric + 1e-9) * 2) / 2


def _round_to_half(value) -> float | None:
    """برای امتیازهای جزئی فقط عدد کامل یا نیم‌نمره تولید می‌کند."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    numeric = min(max(numeric, 1.0), 10.0)
    return round(numeric * 2) / 2


def _source_is_score_eligible(source: dict) -> bool:
    """منبع manual_review یا کم‌کیفیت نباید روی هیچ امتیازی اثر بگذارد."""
    runtime = source.get("runtime_quality_audit") or {}
    if runtime:
        return runtime.get("status") == "acceptable"
    cached = (source.get("evidence_cache") or {}).get("quality_audit") or {}
    return cached.get("status") == "acceptable"


def calculate_overall_score(dossier: dict) -> dict:
    """
    امتیاز نهایی Poormaz مستقیماً از متاکریتیک می‌آید و همیشه رو به پایین
    به نزدیک‌ترین نیم‌نمره گرد می‌شود. مثال: 88 -> 8.5 و 77 -> 7.5.
    نمره‌های نقدهای انتخاب‌شده فقط برای گزارش و کنترل تحریریه نگه داشته
    می‌شوند و منبعی که acceptable نیست، در آن میانگین هم وارد نمی‌شود.
    """
    metacritic_raw = (dossier.get("metacritic", {}) or {}).get("metascore_100")

    metacritic_score = None
    try:
        if metacritic_raw is not None:
            metacritic_score = round(float(metacritic_raw) / 10, 1)
    except (TypeError, ValueError):
        metacritic_score = None

    source_scores = []
    for source in dossier.get("review_sources", []) or []:
        if not _source_is_score_eligible(source):
            continue
        score = as_score_10(source.get("review_score_10"))
        if score is not None:
            source_scores.append(score)

    source_average = (
        round(sum(source_scores) / len(source_scores), 2)
        if source_scores
        else None
    )

    if metacritic_score is not None:
        overall_score = _floor_to_half(metacritic_score)
        formula_fa = (
            "امتیاز نهایی از نمره متاکریتیک گرفته شده و همیشه رو به پایین "
            "به نزدیک‌ترین نیم‌نمره گرد شده است."
        )
    elif source_average is not None:
        overall_score = _floor_to_half(source_average)
        formula_fa = (
            "به‌دلیل نبود نمره متاکریتیک، میانگین نقدهای معتبر رو به پایین "
            "به نزدیک‌ترین نیم‌نمره گرد شده است."
        )
    else:
        overall_score = None
        formula_fa = "داده‌ی عددی کافی برای محاسبه امتیاز نهایی وجود ندارد."

    return {
        "overall_score_10": overall_score,
        "metacritic_score_10": metacritic_score,
        "selected_review_average_10": source_average,
        "selected_review_scores": source_scores,
        "formula_fa": formula_fa,
        "rounding_policy": "floor_to_nearest_0.5",
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


SCORECARD_CATEGORY_ORDER = (
    "gameplay",
    "visuals",
    "story",
    "world_design",
    "technical",
    "value",
)

SCORECARD_CATEGORY_LABELS = {
    "gameplay": "گیم‌پلی",
    "visuals": "گرافیک و طراحی هنری",
    "story": "داستان و شخصیت‌پردازی",
    "world_design": "طراحی جهان و اکتشاف",
    "technical": "عملکرد فنی",
    "value": "ارزش خرید و محتوا",
}


def _signal_direction(evidence_signals: list[dict]) -> float:
    values = {
        "positive": 1.0,
        "negative": -1.0,
        "mixed": 0.0,
        "neutral": 0.0,
    }
    if not evidence_signals:
        return 0.0
    return sum(values.get(item.get("sentiment"), 0.0) for item in evidence_signals) / len(evidence_signals)


def _balance_scorecard_to_overall(rows: list[dict], overall_score: float) -> list[dict]:
    """جمع شش امتیاز جزئی را دقیقاً برابر شش برابر امتیاز نهایی می‌کند."""
    if not rows:
        return rows

    target_units = int(round(float(overall_score) * 2)) * len(rows)
    for row in rows:
        row["_units"] = int(round(float(row["score_10"]) * 2))
        row["_raw_units"] = float(row.get("_raw_score_10", row["score_10"])) * 2

    def choose(direction: int) -> dict | None:
        if direction > 0:
            candidates = [row for row in rows if row["_units"] < 20]
            candidates.sort(
                key=lambda row: (
                    row["_raw_units"] - row["_units"],
                    row.get("_direction_value", 0.0),
                    row.get("evidence_count", 0),
                    -row["_units"],
                ),
                reverse=True,
            )
        else:
            candidates = [row for row in rows if row["_units"] > 2]
            candidates.sort(
                key=lambda row: (
                    row["_raw_units"] - row["_units"],
                    row.get("_direction_value", 0.0),
                    -row.get("evidence_count", 0),
                    row["_units"],
                )
            )
        return candidates[0] if candidates else None

    guard = 0
    while sum(row["_units"] for row in rows) != target_units and guard < 500:
        guard += 1
        direction = 1 if sum(row["_units"] for row in rows) < target_units else -1
        row = choose(direction)
        if row is None:
            break
        row["_units"] += direction

    for row in rows:
        row["score_10"] = row.pop("_units") / 2
        row.pop("_raw_units", None)
        row.pop("_raw_score_10", None)
        row.pop("_direction_value", None)

    return rows


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


def run_wccftech_score_extraction_regression_checks() -> None:
    """
    قفل کردنِ باگِ واقعی: الگوی امتیازِ Wccftech از \\d به‌جای \\d{1,2} استفاده
    می‌کرد، پس یک نمره‌ی کامل «۱۰» هرگز به‌عنوان نمره تشخیص داده نمی‌شد (چون
    \\d فقط یک رقم می‌گیرد و مرزِ کلمه بعد از رقمِ اول شکست می‌خورد). این تست
    دقیقاً همان صفحه‌ی واقعی را شبیه‌سازی می‌کند، نه یک سناریوی انتزاعیِ مشابه.
    """
    page_perfect_score = {
        "url": "https://wccftech.com/review/crimson-desert-review-blissfully-lost-in-pywell/",
        "html": "<html><body>Crimson Desert Review</body></html>",
        "score_text": (
            "Crimson Desert Review – Blissfully Lost In Pywel. "
            "Gaming Score 10. A staggering achievement in open-world design "
            "that rarely lets up across its many locations and side content."
        ),
    }
    result_perfect = extract_review_score(page_perfect_score)
    assert result_perfect["review_score_10"] == 10.0, (
        f"a perfect Wccftech score of 10 must be captured as 10.0, got: {result_perfect}"
    )
    assert result_perfect["score_method"] == "wccftech_header_score"

    # امتیازهای اعشاریِ معمولی (که همیشه درست کار می‌کردند) نباید با این
    # اصلاح خراب شوند.
    page_decimal_score = {
        "url": "https://wccftech.com/review/some-other-game-review/",
        "html": "<html><body>Some Other Game Review</body></html>",
        "score_text": "Some Other Game Review. Gaming Score 8.5. A strong outing overall.",
    }
    result_decimal = extract_review_score(page_decimal_score)
    assert result_decimal["review_score_10"] == 8.5, (
        f"a normal decimal Wccftech score must still parse correctly, got: {result_decimal}"
    )


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
) -> tuple[float | None, str, str, float]:
    """
    امتیاز اولیه‌ی هر بخش را از جهت شواهد می‌سازد. خروجی بعداً در سطح کل
    کارت امتیاز متعادل می‌شود تا میانگین شش بخش دقیقاً برابر امتیاز نهایی باشد.
    """
    if overall_score is None:
        return None, "نامشخص", "داده محدود", 0.0

    direction_value = _signal_direction(evidence_signals)
    evidence_count = len(evidence_signals)
    source_count = len({
        item.get("site_name", "")
        for item in evidence_signals
        if item.get("site_name")
    })

    if evidence_count >= 4 and source_count >= 3:
        coverage_factor = 1.0
        confidence_fa = "خوب"
    elif evidence_count >= 3 and source_count >= 2:
        coverage_factor = 0.75
        confidence_fa = "متوسط"
    elif evidence_count >= 1:
        coverage_factor = 0.40
        confidence_fa = "محدود"
    else:
        coverage_factor = 0.0
        confidence_fa = "داده محدود"

    raw_score = float(overall_score) + REVIEW_CATEGORY_MAX_DELTA * direction_value * coverage_factor
    raw_score = min(max(raw_score, 1.0), 10.0)
    score_10 = _round_to_half(raw_score)

    if direction_value >= 0.35:
        trend_fa = "مثبت"
    elif direction_value <= -0.35:
        trend_fa = "منفی"
    elif evidence_signals:
        trend_fa = "ترکیبی"
    else:
        trend_fa = "داده محدود"

    return score_10, trend_fa, confidence_fa, raw_score

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
    شش امتیاز ثابت Poormaz را فقط از منابع acceptable می‌سازد. همه‌ی نمره‌ها
    کامل یا نیم‌نمره‌اند و میانگینشان دقیقاً با امتیاز نهایی برابر است.
    """
    del client

    score_info = calculate_overall_score(dossier)
    eligible_sources = [
        source for source in dossier.get("review_sources", []) or []
        if _source_is_score_eligible(source)
    ]
    evidence_index = build_evidence_index(eligible_sources)

    if score_info.get("overall_score_10") is None:
        return {
            **score_info,
            "scorecard": [],
            "scorecard_average_10": None,
            "scorecard_sum_10": None,
            "evidence_index": evidence_index,
            "classification_method": "rule_based_balanced_half_steps",
            "uncategorized_refs": [],
        }

    signals_by_category: dict[str, list[dict]] = {
        key: [] for key in SCORECARD_CATEGORY_ORDER
    }
    uncategorized_refs = []

    for ref_id, item in evidence_index.items():
        category, sentiment = classify_evidence_rule_based(item)
        # شواهد صوتی در مقاله باقی می‌مانند، اما برای کارت شش‌گانه به بخش
        # ارائه‌ی هنری کمک می‌کنند تا یک کارت هفتمِ ناخواسته نسازند.
        if category == "audio":
            category = "visuals"

        if category not in signals_by_category or sentiment is None:
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
    overall = float(score_info["overall_score_10"])

    for key in SCORECARD_CATEGORY_ORDER:
        signals = signals_by_category[key]
        score_10, trend_fa, confidence_fa, raw_score = category_score_from_evidence(
            overall,
            signals,
        )
        scorecard.append(
            {
                "key": key,
                "label_fa": SCORECARD_CATEGORY_LABELS[key],
                "score_10": score_10 if score_10 is not None else overall,
                "trend_fa": trend_fa,
                "confidence_fa": confidence_fa,
                "evidence_count": len(signals),
                "source_count": len({
                    signal["site_name"] for signal in signals if signal["site_name"]
                }),
                "supported_refs": [signal["ref_id"] for signal in signals],
                "evidence_signals": [
                    {"ref_id": signal["ref_id"], "sentiment": signal["sentiment"]}
                    for signal in signals
                ],
                "_raw_score_10": raw_score,
                "_direction_value": _signal_direction(signals),
            }
        )

    scorecard = _balance_scorecard_to_overall(scorecard, overall)
    scorecard_sum = round(sum(float(row["score_10"]) for row in scorecard), 1)
    scorecard_average = round(scorecard_sum / len(scorecard), 1)

    return {
        **score_info,
        "scorecard": scorecard,
        "scorecard_average_10": scorecard_average,
        "scorecard_sum_10": scorecard_sum,
        "scorecard_average_matches_overall": abs(scorecard_average - overall) < 1e-9,
        "evidence_index": evidence_index,
        "classification_method": "rule_based_balanced_half_steps",
        "uncategorized_refs": uncategorized_refs,
    }

def _format_score_10(value) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "نامشخص"
    number = str(int(numeric)) if numeric.is_integer() else f"{numeric:.1f}"
    return f"{number}/10"


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


def _public_article_sources(dossier: dict) -> list[dict]:
    """متن عمومی و امتیازدهی فقط از منابعی با وضعیت acceptable استفاده می‌کنند."""
    return [
        source for source in dossier.get("review_sources", []) or []
        if _source_is_score_eligible(source)
    ]


_PUBLIC_TOPIC_LABELS = {
    "world_design": "جهان بازی و اکتشاف",
    "gameplay": "گیم‌پلی و مبارزه",
    "visuals": "گرافیک و طراحی هنری",
    "audio": "صداگذاری و موسیقی",
    "story": "روایت و شخصیت‌پردازی",
    "technical": "فنی و عملکرد",
    "value": "ارزش خرید و محتوا",
    "general": "تصویر کلی",
}

_PUBLIC_TOPIC_ORDER = (
    "gameplay",
    "world_design",
    "visuals",
    "technical",
    "story",
    "value",
    "audio",
    "general",
)

# چنین نکاتی به‌تنهایی برای نوشتن یک نقد خوب کافی نیستند. آن‌ها معمولاً فقط
# تکرار «جذاب است» هستند و بدون توضیح چرایی، متن را به تبلیغ بازی تبدیل می‌کنند.
_PUBLIC_GENERIC_POINT_MARKERS = (
    "تجربه‌ای جذاب",
    "تجربه جذاب",
    "شگفت‌انگیز",
    "ارزش تجربه",
    "ارزش بازی کردن",
    "غیرقابل ترک",
    "بسیار سرگرم‌کننده",
)

_PUBLIC_SPECIFIC_POINT_MARKERS = (
    "جهان", "اکتشاف", "کاوش", "تعامل", "مبارز", "نبرد", "باس",
    "رئیس", "پازل", "سیستم", "مکانیک", "کنترل", "اسب", "داستان",
    "روایت", "شخصیت", "نوشتار", "ترجمه", "ماموریت", "عملکرد", "فنی",
    "باگ", "موجودی", "ذخیره", "زمان", "طراحی", "پیشرفت",
    "گرافیک", "نورپردازی", "انیمیشن", "بافت", "صداگذاری", "موسیقی",
    "قیمت", "محتوا", "ارزش خرید", "تکرارپذیری",
)


def _public_fact_topic(point: dict, section: str) -> str:
    """یک دسته‌ی تحریریه‌ای برای انتخاب واقعیت‌های مقاله، نه برای امتیازدهی."""
    category, _ = classify_evidence_rule_based(
        {
            "evidence_en": str(point.get("evidence_en") or ""),
            "section": section,
        }
    )
    if category in _PUBLIC_TOPIC_LABELS:
        return category

    text = clean_text(str(point.get("point_fa") or "")).casefold()
    if any(word in text for word in ("جهان", "اکتشاف", "کاوش", "محیط", "تعامل")):
        return "world_design"
    if any(word in text for word in ("مبارز", "نبرد", "باس", "رئیس", "پازل", "کنترل", "اسب", "مکانیک", "سیستم")):
        return "gameplay"
    if any(word in text for word in ("داستان", "روایت", "شخصیت", "نوشتار", "ترجمه", "ماموریت")):
        return "story"
    if any(word in text for word in ("عملکرد", "فنی", "باگ", "کرش", "بهینه")):
        return "technical"
    return "general"


def _public_fact_is_specific(point_fa: str, evidence_en: str) -> bool:
    """کلی‌گویی‌های تکراری را پیش از رسیدن به نویسنده حذف می‌کند."""
    point_fa = clean_text(point_fa)
    evidence_en = clean_text(evidence_en)

    if not point_fa or _has_broken_character(point_fa):
        return False
    if len(point_fa.split()) < 4:
        return False

    # نقل‌قول‌های خیلی بلند معمولاً از استخراج بدِ HTML آمده‌اند و نباید به
    # عنوان پایه‌ی یک ادعای تحریریه‌ای استفاده شوند.
    evidence_word_count = len(re.findall(r"\b[\w'-]+\b", evidence_en))
    if evidence_en and evidence_word_count > 42:
        return False

    folded = point_fa.casefold()
    has_generic_marker = any(marker in folded for marker in _PUBLIC_GENERIC_POINT_MARKERS)
    has_specific_marker = any(marker in folded for marker in _PUBLIC_SPECIFIC_POINT_MARKERS)
    if has_generic_marker and not has_specific_marker:
        return False

    return True


def _public_fact_key(point_fa: str) -> str:
    text = clean_text(point_fa).casefold()
    text = re.sub(r"[\W_]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _public_facts_from_sources(review_sources: list[dict]) -> list[dict]:
    """
    یک brief فشرده برای نویسنده می‌سازد. هر منبع حداکثر پنج واقعیت دارد تا یک
    نقد بلند، ناخواسته به بازنویسی یک سایت واحد تبدیل نشود.
    """
    raw = []
    evidence_sentiments: dict[str, set[str]] = {}

    for source_index, source in enumerate(review_sources, start=1):
        site_name = clean_text(str(source.get("site_name") or ""))
        for section, sentiment in (
            ("positives", "positive"),
            ("negatives", "negative"),
            ("technical_notes", "caution"),
        ):
            for point_index, point in enumerate(source.get(section, []) or [], start=1):
                if not isinstance(point, dict):
                    continue
                point_fa = clean_text(str(point.get("point_fa") or ""))
                evidence_en = clean_text(str(point.get("evidence_en") or ""))
                if not _public_fact_is_specific(point_fa, evidence_en):
                    continue

                evidence_key = evidence_en.casefold() or _public_fact_key(point_fa)
                evidence_sentiments.setdefault(evidence_key, set()).add(sentiment)
                raw.append(
                    {
                        "id": f"F{source_index:02d}{section[0].upper()}{point_index}",
                        "site_name": site_name,
                        "topic": _public_fact_topic(point, section),
                        "sentiment": sentiment,
                        "text_fa": point_fa,
                        # متن انگلیسی فقط به نویسنده کمک می‌کند ظرافت ادعا را بفهمد؛
                        # هرگز در مقاله‌ی عمومی نقل یا ترجمه‌ی تحت‌اللفظی نمی‌شود.
                        "evidence_en": evidence_en,
                        "evidence_key": evidence_key,
                    }
                )

    # یک شاهد واحد که هم مثبت و هم منفی ثبت شده، نتیجه‌گیری دوپهلو است. آن را
    # به نویسنده نمی‌دهیم تا مجبور نشود از همان جمله دو ادعای متضاد بسازد.
    conflict_keys = {
        key for key, sentiments in evidence_sentiments.items()
        if "positive" in sentiments and "negative" in sentiments
    }

    raw = [item for item in raw if item["evidence_key"] not in conflict_keys]

    # اولویت با نکات مشخص‌تر است، اما تنوع منبع و موضوع هم حفظ می‌شود.
    def priority(item: dict) -> tuple:
        text = item["text_fa"]
        specificity = sum(marker in text.casefold() for marker in _PUBLIC_SPECIFIC_POINT_MARKERS)
        topic_rank = _PUBLIC_TOPIC_ORDER.index(item["topic"]) if item["topic"] in _PUBLIC_TOPIC_ORDER else 99
        sentiment_rank = 0 if item["sentiment"] in {"positive", "negative"} else 1
        return (topic_rank, sentiment_rank, -specificity, len(text))

    raw.sort(key=priority)

    selected = []
    seen_texts = set()
    source_counts: dict[str, int] = {}
    topic_counts: dict[str, int] = {}
    topic_limits = {
        "gameplay": 6,
        "world_design": 6,
        "visuals": 4,
        "story": 5,
        "technical": 4,
        "value": 3,
        "audio": 3,
        "general": 2,
    }

    for item in raw:
        text_key = _public_fact_key(item["text_fa"])
        if not text_key or text_key in seen_texts:
            continue

        site_name = item["site_name"] or "Unknown"
        topic = item["topic"]
        if source_counts.get(site_name, 0) >= 5:
            continue
        if topic_counts.get(topic, 0) >= topic_limits.get(topic, 3):
            continue

        seen_texts.add(text_key)
        source_counts[site_name] = source_counts.get(site_name, 0) + 1
        topic_counts[topic] = topic_counts.get(topic, 0) + 1
        selected.append({key: value for key, value in item.items() if key != "evidence_key"})

    return selected[:24]


def _public_article_fact_pack(dossier: dict) -> dict:
    review_sources = _public_article_sources(dossier)
    assessment = dossier.get("poormaz_assessment", {}) or {}
    metacritic = dossier.get("metacritic", {}) or {}
    game_intro = dossier.get("game_intro", {}) or {}

    return {
        "game": clean_text(str(dossier.get("game") or "")) or "بازی",
        "platform": clean_text(str(dossier.get("platform") or "")),
        "game_intro_fa": clean_text(str(game_intro.get("intro_fa") or "")),
        "poormaz_score": assessment.get("overall_score_10"),
        "metascore": metacritic.get("metascore_100"),
        "critic_count": metacritic.get("critic_review_count"),
        "scorecard": assessment.get("scorecard", []) or [],
        "facts": _public_facts_from_sources(review_sources),
        "sources": review_sources,
    }


def _public_article_forbidden_text(text: str) -> bool:
    forbidden = (
        "پوشش شواهد", "شواهد تأییدشده", "پرونده‌ی فعلی", "پرونده فعلی",
        "سطح اطمینان", "برداشت اولیه", "به‌صورت خودکار", "منبع بررسی‌شده",
        "منابع بررسی‌شده", "نقدهای انتخاب‌شده", "مدل زبانی", "هوش مصنوعی",
        "evidence", "confidence", "متاکریتیک", "poormaz", "امتیاز",
        "نمره", "منتقد", "سایت", "وب‌سایت", "منبع",
    )
    folded = clean_text(text).casefold()
    return any(token.casefold() in folded for token in forbidden)


def _public_section_is_safe(
    value: str,
    *,
    min_words: int,
    max_words: int,
    allowed_site_names: set[str],
) -> bool:
    value = clean_text(str(value or ""))

    if not value or _has_broken_character(value):
        return False
    if len(value.split()) < min_words or len(value.split()) > max_words:
        return False
    if re.search(r"https?://|[#*`]|(?:^|\s)-\s", value):
        return False
    if _public_article_forbidden_text(value):
        return False

    for site_name in allowed_site_names:
        if site_name and site_name.casefold() in value.casefold():
            return False

    # جمله‌های تریلری معمولاً از توضیح تبلیغاتی بازی می‌آیند، نه از خود نقد.
    trailer_markers = (
        "وفاداری", "قهرمانان شکل", "سرزمین‌های سخت", "خطرات ناشناخته",
        "به دنیای بازی", "وارد دنیای",
    )
    if any(marker in value.casefold() for marker in trailer_markers):
        return False

    return True


def _fact_map(facts: dict) -> dict[str, dict]:
    return {
        item["id"]: item
        for item in facts.get("facts", [])
        if isinstance(item, dict) and item.get("id")
    }


def _public_section_validation_reason(
    value,
    *,
    allowed_fact_ids: set[str],
    fact_by_id: dict[str, dict],
    min_words: int,
    max_words: int,
    min_supports: int,
    required_topics: set[str] | None = None,
    required_sentiments: set[str] | None = None,
    allowed_site_names: set[str],
) -> str | None:
    """دلیل قابل‌فهم رد شدن یک بخش عمومی، برای retry هدفمند."""
    if not isinstance(value, dict):
        return "فرمت JSON بخش معتبر نیست"

    text_fa = clean_text(str(value.get("text_fa") or ""))
    if not text_fa:
        return "متن بخش خالی است"
    if _has_broken_character(text_fa):
        return "متن دارای نویسه‌ی خراب است"

    word_count = len(text_fa.split())
    if word_count < min_words:
        return f"متن کوتاه است ({word_count} واژه، حداقل {min_words})"
    if word_count > max_words:
        return f"متن بلند است ({word_count} واژه، حداکثر {max_words})"
    if re.search(r"https?://|[#*`]|(?:^|\s)-\s", text_fa):
        return "متن شامل Markdown یا لینک است"
    if _public_article_forbidden_text(text_fa):
        return "متن شامل واژه‌های داخلی یا نمره‌دهی است"

    for site_name in allowed_site_names:
        if site_name and site_name.casefold() in text_fa.casefold():
            return "نام منبع در متن عمومی آمده است"

    trailer_markers = (
        "وفاداری", "قهرمانان شکل", "سرزمین‌های سخت", "خطرات ناشناخته",
        "به دنیای بازی", "وارد دنیای",
    )
    if any(marker in text_fa.casefold() for marker in trailer_markers):
        return "متن به لحن تریلری یا تبلیغاتی رفته است"

    supports = []
    for ref in value.get("supports", []) or []:
        ref = clean_text(str(ref or ""))
        if ref in allowed_fact_ids and ref not in supports:
            supports.append(ref)

    if len(supports) < min_supports:
        return f"شناسه‌های پشتیبان کافی نیستند ({len(supports)} از {min_supports})"

    support_facts = [fact_by_id[item] for item in supports]
    if required_topics and not any(item.get("topic") in required_topics for item in support_facts):
        return "پشتیبان‌ها موضوع لازم برای این بخش را ندارند"
    if required_sentiments and not any(
        item.get("sentiment") in required_sentiments for item in support_facts
    ):
        return "پشتیبان‌ها جهت‌گیری لازم برای این بخش را ندارند"

    return None


def _section_has_balanced_supports(section: dict, fact_by_id: dict[str, dict]) -> bool:
    sentiments = {
        fact_by_id[item].get("sentiment")
        for item in section.get("supports", [])
        if item in fact_by_id
    }
    return "positive" in sentiments and bool(sentiments & {"negative", "caution"})


_SINGLE_PASS_SECTION_LABELS = {
    "opening": "شروع نقد و تز اصلی",
    "world_gameplay": "جهان بازی و گیم‌پلی",
    "friction": "اصطکاک‌ها و ضعف‌ها",
    "audience": "مخاطب مناسب",
    "conclusion": "جمع‌بندی",
}


def _write_targeted_section_revision(
    client: OpenAI,
    *,
    key: str,
    game: str,
    assigned_facts: list[dict],
    spec: dict,
    allowed_site_names: set[str],
    failure_reason: str,
) -> dict | None:
    """
    پیش از هر بازنویسیِ کل مقاله یا هر fallback، فقط همان یک بخشِ مردود را،
    با همان مجموعه‌ی واقعیت‌های از پیش قفل‌شده (نه یک مخزن بزرگ‌تر برای
    انتخاب)، دوباره می‌نویسد. این دقیقاً «پاسِ نگارشِ» پیش از تصمیمِ fallback
    است. اگر خروجی باز هم رد شود یا OpenAI خطا بدهد، None برمی‌گردد و تصمیم
    نهایی (بازنویسیِ کل مقاله، سپس در نهایت fallback) به تماس‌گیرنده می‌رسد.
    """
    writer_facts = [
        {
            "id": item["id"],
            "بخش": _PUBLIC_TOPIC_LABELS.get(item.get("topic"), "تصویر کلی"),
            "جهت": {
                "positive": "مثبت",
                "negative": "منفی",
                "caution": "احتیاط",
            }.get(item.get("sentiment"), "خنثی"),
            "واقعیت": item["text_fa"],
        }
        for item in assigned_facts
    ]
    section_label = _SINGLE_PASS_SECTION_LABELS.get(key, key)

    prompt = f"""
You are revising ONLY one section of an already-mostly-approved Persian game review
for Poormaz, about "{game}". The rest of the article is fine; only this section was
rejected.

Section: {section_label}
Why the previous attempt was rejected: {failure_reason}

You MUST use exactly these facts, and ONLY these -- they are the full boundary of
what may be asserted in this section, not a menu to pick from:
{json.dumps({"facts": writer_facts}, ensure_ascii=False, indent=2)}

Return exactly this JSON object:
{{"text_fa": "..."}}

Hard rules:
- Write fluent contemporary Persian prose only (no bullets, no headings, no Markdown).
- Use only the facts above. Do not add lore, plot, features, scores, sources,
  reviewer names, or website names.
- Do not use: امتیاز، نمره، متاکریتیک، Poormaz، منتقد، سایت، منبع، شواهد.
- Explain concrete cause and effect instead of generic praise.
- Length target: {spec["min_words"]} to {spec["max_words"]} Persian words -- this is
  a goal, not a hard wall; a few words over or under is fine.
""".strip()

    try:
        raw = ask_openai_json(client, prompt, max_tokens=900)
    except Exception as exc:
        print(f"Targeted writing pass for '{key}' failed: {repr(exc)}")
        return None

    text_fa = _editorial_text_value(raw)
    if not _public_section_is_safe(
        text_fa,
        min_words=spec["min_words"],
        max_words=spec["max_words"],
        allowed_site_names=allowed_site_names,
    ):
        print(f"Targeted writing pass for '{key}' still failed length/safety checks.")
        return None

    claim_quality_reason = _editorial_claim_quality_reason(key, text_fa)
    if claim_quality_reason:
        print(f"Targeted writing pass for '{key}' still has a claim-quality issue: {claim_quality_reason}")
        return None

    print(f"Section '{key}' fixed by a targeted writing pass (no full regeneration needed).")
    return {"text_fa": text_fa, "supports": [item["id"] for item in assigned_facts]}


def _fallback_text_from_facts(facts: list[dict], *, topic: set[str] | None = None, sentiment: set[str] | None = None, limit: int = 4) -> str:
    chosen = []
    for item in facts:
        if topic and item.get("topic") not in topic:
            continue
        if sentiment and item.get("sentiment") not in sentiment:
            continue
        text_fa = clean_text(str(item.get("text_fa") or ""))
        # مسیر fallback، برخلاف مسیر مدل، تا اینجا هیچ‌جا نویسه‌ی خراب را چک
        # نکرده بود؛ یک واقعیتِ خراب از استخراج قبلی می‌توانست بی‌هیچ فیلتری
        # مستقیم وارد امن‌ترین مسیر بشود. اینجا همان آزمونی که مسیر مدل همیشه
        # از آن رد می‌شود، اعمال می‌شود.
        if not text_fa or _has_broken_character(text_fa):
            continue
        chosen.append(text_fa)
        if len(chosen) >= limit:
            break
    return " ".join(chosen)


def _build_public_article_fallback(facts: dict) -> dict:
    """مسیر امن در صورت شکست مدل. کوتاه‌تر است، اما ادعای تازه نمی‌سازد."""
    all_facts = facts.get("facts", [])
    game = facts["game"]
    world_and_gameplay = _fallback_text_from_facts(
        all_facts,
        topic={"world_design", "gameplay"},
        sentiment={"positive"},
        limit=4,
    )
    friction = _fallback_text_from_facts(
        all_facts,
        topic={"gameplay", "story", "technical"},
        sentiment={"negative", "caution"},
        limit=5,
    )
    opening = (
        f"{game} از آن بازی‌هایی است که در یک بخش می‌تواند بازیکن را کاملاً درگیر کند و در بخش دیگر، صبر او را محک بزند. "
        "در هسته‌ی تجربه، جهان بازی و شیوه‌ی تعامل با آن نقش پررنگی دارند، اما همین جاه‌طلبی همیشه به یک ریتم یکدست تبدیل نمی‌شود."
    )
    strengths = (
        "بهترین لحظات بازی زمانی شکل می‌گیرند که فرصت اکتشاف و درگیری با سیستم‌های آن را می‌دهد. "
        + (world_and_gameplay or "جهان بازی و گیم‌پلی، بخش‌های برجسته‌ی تجربه هستند.")
    )
    weaknesses = (
        "در مقابل، چند اصطکاک مهم اجازه نمی‌دهد این کیفیت در تمام مسیر حفظ شود. "
        + (friction or "روایت و طراحی بعضی از بخش‌ها می‌توانند تجربه را ناهموار کنند.")
    )
    audience = (
        f"{game} برای کسی مناسب‌تر است که از اکتشاف، مبارزه و سر و کله زدن با یک تجربه‌ی پرسیستم لذت می‌برد. "
        "اما اگر روایت منسجم، ریتم نرم و کمترین اصطکاک در طراحی برایتان اولویت مطلق است، بهتر است با انتظار واقع‌بینانه‌تری سراغش بروید."
    )
    conclusion = (
        f"{game} بازی بی‌نقصی نیست، اما جاه‌طلبی‌اش در بهترین لحظات واقعاً نتیجه می‌دهد. "
        "ارزش آن بیش از هر چیز به این بستگی دارد که چقدر با نقاط اصطکاکش کنار می‌آیید و در عوض، از اکتشاف و سیستم‌های متنوعش چه می‌خواهید."
    )
    return {
        "opening_fa": opening,
        "world_gameplay_fa": strengths,
        "friction_fa": weaknesses,
        "audience_fa": audience,
        "conclusion_fa": conclusion,
        "method": "deterministic_editorial_fallback_v27",
    }


def _pick_editorial_facts(
    all_facts: list[dict],
    *,
    used_ids: set[str],
    topics: set[str] | None,
    sentiments: set[str],
    count: int,
) -> list[dict]:
    """انتخاب متنوعِ واقعیت‌ها برای یک بخش، بدون استفاده‌ی دوباره در مقاله."""
    candidates = [
        item for item in all_facts
        if item.get("id") not in used_ids
        and item.get("sentiment") in sentiments
        and (topics is None or item.get("topic") in topics)
    ]

    # اولویت با تنوع منبع و سپس ادعاهای مشخص‌تر است. این جلوی تبدیل‌شدن متن به
    # بازنویسیِ یک نقد واحد را می‌گیرد، آن هم چیزی که اینترنت همین حالا هم به
    # اندازه‌ی کافی از آن دارد.
    def rank(item: dict) -> tuple:
        source = clean_text(str(item.get("site_name") or ""))
        specificity = sum(
            marker in clean_text(str(item.get("text_fa") or "")).casefold()
            for marker in _PUBLIC_SPECIFIC_POINT_MARKERS
        )
        return (
            0 if source else 1,
            -specificity,
            len(clean_text(str(item.get("text_fa") or ""))),
            clean_text(str(item.get("id") or "")),
        )

    candidates.sort(key=rank)
    chosen: list[dict] = []
    source_counts: dict[str, int] = {}

    # گذر اول: تا حد امکان هر منبع فقط یک بار وارد هر بخش شود.
    for item in candidates:
        source = clean_text(str(item.get("site_name") or "Unknown"))
        if source_counts.get(source, 0) > 0:
            continue
        chosen.append(item)
        source_counts[source] = source_counts.get(source, 0) + 1
        if len(chosen) >= count:
            return chosen

    # گذر دوم: اگر منبع کافی نبود، باقیمانده را پر کن، ولی همچنان بدون تکرار fact.
    chosen_ids = {item.get("id") for item in chosen}
    for item in candidates:
        if item.get("id") in chosen_ids:
            continue
        chosen.append(item)
        chosen_ids.add(item.get("id"))
        if len(chosen) >= count:
            break
    return chosen


def _single_pass_editorial_plan(facts: dict) -> dict[str, list[dict]] | None:
    """
    یک نقشه‌ی تحریریِ کم‌تعداد اما پُرمحتوا می‌سازد. بخش‌های گزارشی از
    واقعیت‌های مجزا استفاده می‌کنند؛ «مخاطب» و «جمع‌بندی» synthesis هستند و
    فقط از نتیجه‌ی بدنه استفاده می‌کنند. هدف، مقاله است نه بازخوانی پنج کارت
    اطلاعاتی با کلمات متفاوت.
    """
    all_facts = [item for item in facts.get("facts", []) if isinstance(item, dict) and item.get("id")]
    used_ids: set[str] = set()

    def take(*, topics: set[str] | None, sentiments: set[str], count: int) -> list[dict]:
        picked = _pick_editorial_facts(
            all_facts,
            used_ids=used_ids,
            topics=topics,
            sentiments=sentiments,
            count=count,
        )
        used_ids.update(item["id"] for item in picked if item.get("id"))
        return picked

    # افتتاحیه فقط تز مقاله را می‌سازد: یک کشش اصلی و یک هزینه‌ی اصلی.
    opening_pos = take(topics={"world_design"}, sentiments={"positive"}, count=1)
    opening_neg = take(topics={"gameplay", "story", "technical"}, sentiments={"negative", "caution"}, count=1)

    # بدنه واقعاً جا برای تحلیل داشته باشد. هر واقعیت گزارشی تنها در همین یکی
    # از دو بخش مصرف می‌شود و مدل می‌تواند رابطه‌ی علت و معلولشان را توضیح دهد.
    # عمداً روی ۲ تا ۳ نکته در هر بخش قفل می‌شود، نه بیشتر: وقتی مدل مجبور به
    # پوشش ۴ کارت جدا در یک بخش کوتاه باشد، معمولاً با صفت‌های کلی آن‌ها را به
    # هم می‌چسباند تا جا شوند. با ۳ نکته، هرکدام واقعاً یک مشاهده می‌شود.
    world = take(topics={"world_design", "gameplay"}, sentiments={"positive"}, count=3)
    friction = take(topics={"gameplay", "story", "technical"}, sentiments={"negative", "caution"}, count=3)

    if (
        len(opening_pos) < 1
        or len(opening_neg) < 1
        or len(world) < 2
        or len(friction) < 2
    ):
        return None

    # این‌ها واقعیت تازه مصرف نمی‌کنند. شناسه‌ها فقط لنگر استدلال‌اند و باید
    # باعث توصیه‌ی مشخص شوند، نه کپی‌برداری از تیترهای قبلی.
    audience = [world[0], friction[0]]
    conclusion = [opening_pos[0], opening_neg[0]]

    return {
        "opening": opening_pos + opening_neg,
        "world_gameplay": world,
        "friction": friction,
        "audience": audience,
        "conclusion": conclusion,
    }


def _single_pass_prompt(facts: dict, plan: dict[str, list[dict]], retry_reason: str = "") -> str:
    """بریف تحریریِ فشرده و مبتنی بر شواهد؛ هدف، نقد دقیق است نه طول‌دادن متن با صفت‌های کلی."""
    labels = {
        "opening": "بازی در عمل",
        "world_gameplay": "جهان بازی و گیم‌پلی",
        "friction": "اصطکاک‌هایی که نمی‌شود نادیده گرفت",
        "audience": "مناسب چه کسی است؟",
        "conclusion": "جمع‌بندی Poormaz",
    }
    synthesis_sections = {"audience", "conclusion"}
    section_jobs = {
        "opening": (
            "در دو یا سه جمله، فقط تز مقاله را بساز: مهم‌ترین کشش تجربه در برابر "
            "هزینه‌ی اصلی آن. از داستان، شخصیت‌ها یا کیفیت فنی حرف نزن مگر کارت همین بخش "
            "صراحتاً همان را گفته باشد."
        ),
        "world_gameplay": (
            "دقیقاً همین دو یا سه کارت را با جزئیات مشخص باز کن؛ توضیح بده چرا اکتشاف یا درگیری‌ها "
            "می‌توانند کشش ایجاد کنند. هر نکته باید یک اثر عملی برای بازیکن داشته باشد: چه چیزی او را "
            "به جلو می‌برد، به آزمودن وادار می‌کند یا ریتم بازی را عوض می‌کند. مشکلات و ضعف‌ها را به "
            "این بخش نبر. این نکات سقف ادعای مجاز است، نه نمونه‌ای از میان گزینه‌های بیشتر."
        ),
        "friction": (
            "دقیقاً همین دو یا سه اصطکاک را به یک استدلال وصل کن: مشکل دقیق چیست، در بازی کردن چه احساسی "
            "ایجاد می‌کند، و چگونه ریتم یا حس کنترل را خراب می‌کند. از کلی‌گویی درباره‌ی «مشکلات جدی» "
            "پرهیز کن. این نکات سقف ادعای مجاز است، نه نمونه‌ای از میان گزینه‌های بیشتر."
        ),
        "audience": (
            "یک توصیه‌ی عملیِ خرید بده، نه بازنویسی بدنه. روشن بگو چه بازیکنی با این معامله کنار می‌آید "
            "و چه کسی بهتر است از بازی عبور کند. «صبر کردن» فقط وقتی مجاز است که کارت‌ها از اصلاح یا آپدیت مشخصی حرف بزنند."
        ),
        "conclusion": (
            "حکم نهاییِ دقیق بده: ارزش تجربه کجاست، بهای آن چیست، و برای مخاطب مناسب آیا این معامله می‌ارزد یا نه. "
            "بدون تکرار افتتاحیه و بدون وعده‌ی نامستند درباره‌ی آینده‌ی بازی."
        ),
    }

    brief = {}
    for key, items in plan.items():
        brief[key] = {
            "label": labels[key],
            "mode": "synthesis" if key in synthesis_sections else "reporting",
            "job_fa": section_jobs[key],
            "evidence_cards": [
                {
                    "topic": _PUBLIC_TOPIC_LABELS.get(item.get("topic"), "تصویر کلی"),
                    "sentiment": {
                        "positive": "مثبت",
                        "negative": "منفی",
                        "caution": "احتیاط",
                    }.get(item.get("sentiment"), "خنثی"),
                    # این جمله تنها ادعای مجازِ کارت است؛ ویژگی تازه به آن اضافه نکن.
                    "approved_claim_fa": item["text_fa"],
                    # زمینه فقط برای فهم دقیق است، نه نقل، ترجمه‌ی لفظی یا افزودن جزئیات تازه.
                    "context_en": clean_text(str(item.get("evidence_en") or ""))[:420],
                }
                for item in items
            ],
        }

    retry_note = ""
    if retry_reason:
        retry_note = f"""
The previous full draft was rejected for this reason: {retry_reason}
Rewrite the ENTIRE article from scratch. Remove the problem directly. Do not compensate
by adding generic praise, claims about the future, or unsupported details.
"""

    return f"""
You are the senior Persian games editor for Poormaz. Write ONE concise, evidence-disciplined
Persian review of "{facts['game']}".

The goal is not a long article. The goal is a useful critic's argument. Do not inflate
the prose to sound literary. A 450-word review with concrete judgment is better than an
800-word cloud of adjectives.

You receive a LOCKED editorial map. In reporting sections, every assigned card is the
full boundary of what may be asserted. Use each assigned card once, naturally, and only
in its assigned section. Do not move a negative card into the world/gameplay section or
turn a narrow positive card into a claim about the whole game. The English context is
only for nuance. Never quote it, translate it literally, or introduce facts beyond it.

Locked editorial map:
{json.dumps(brief, ensure_ascii=False, indent=2)}
{retry_note}
Return exactly one JSON object, with exactly these five objects and NOTHING else:
{{
  "opening": {{"text_fa": "..."}},
  "world_gameplay": {{"text_fa": "..."}},
  "friction": {{"text_fa": "..."}},
  "audience": {{"text_fa": "..."}},
  "conclusion": {{"text_fa": "..."}}
}}

Writing rules:
- Write one connected review, not five summaries.
- Each reporting sentence must do one of two things: state a precise approved claim, or explain
  its direct consequence for pacing, learning, exploration, control, or fatigue.
- A consequence must follow from the card. Do not invent rewards, quest types, plot events,
  technical causes, player behavior, critical consensus, comparisons, or future fixes.
- Never turn «worth exploring» into «deep», «compelling», «exciting», «memorable» or «strong».
  Never turn «beautiful» into «best-in-class», «technically flawless», «immersive» or «rich».
- Never say «داستانی جذاب»، «شخصیت‌های جذاب»، «تجربه‌ای دوگانه»، «مشکلات جدی»، «نیاز به بهبود»,
  «بازیکنان را رها می‌کند», «بهترین در تاریخ», «با وعده‌های بزرگ», or «جهان غنی».
- Avoid generic filler such as «به طرز شگفت‌انگیزی»، «به بازیکنان اجازه می‌دهد»،
  «می‌تواند تجربه‌ای جذاب ایجاد کند»، «ساعت‌ها غرق می‌شوند»، and «از هر گوشه لذت می‌برند».
- Do not repeat a fact with a new adjective. State it once, then move the argument forward.
- Audience must make a buy-or-skip recommendation. Do not tell readers to wait unless the evidence
  explicitly documents a future patch or release change.
- Do not use English quotations, URLs, source names, IDs, Markdown, labels, or references to scores
  inside text_fa. Do not use: امتیاز، نمره، متاکریتیک، Poormaz، منتقد، سایت، منبع، شواهد.
- Do not state that the story or characters are attractive unless a card explicitly says so.
- Do not put system ambiguity, boss imbalance, inventory friction, or slowed progression in world_gameplay.
  Those belong only to friction.

Length:
- opening: 45–75 Persian words
- world_gameplay: 100–150 Persian words
- friction: 100–150 Persian words
- audience: 55–80 Persian words
- conclusion: 60–90 Persian words
- Total target: 450–550 Persian words. Stop when the argument is complete. Current
  evidence supports a precise 500-word review, not a longer one — padding past 550
  words to sound more literary is a failure, not a bonus.

Before returning JSON, silently check: every claim is grounded in its assigned card, no sentence
uses a generic compliment in place of an observation, and the audience advice is actionable.
""".strip()

def _editorial_text_value(value) -> str:
    """مدل گاهی wrapper شیء را درست می‌فرستد و گاهی متن را مستقیم؛ هر دو قابل قبول‌اند."""
    if isinstance(value, dict):
        return clean_text(str(value.get("text_fa") or ""))
    if isinstance(value, str):
        return clean_text(value)
    return ""


# این فهرست فقط عبارت‌های دقیق قدیمی نیست؛ صفت‌های کلی را تک‌تک نگه می‌دارد تا
# جایگزینیِ مترادف (مثلاً «پرجزئیات» به‌جای «غنی») دیگر از زیر چک در نرود. یک
# جمله فقط وقتی مردود می‌شود که این صفتِ کلی را داشته باشد و هیچ جزئیات مشخصی
# (از _PUBLIC_SPECIFIC_POINT_MARKERS) در همان جمله نیاورده باشد.
_PROSE_GENERIC_ADJECTIVE_MARKERS = (
    "غنی", "پرجزئیات", "زیبا", "خیره‌کننده", "شگفت‌انگیز", "عالی", "فوق‌العاده",
    "گیرا", "پویا", "چشم‌نواز", "نفس‌گیر", "دلنشین", "لذت‌بخش", "بی‌نظیر",
    "استثنایی", "خارق‌العاده", "تحسین‌برانگیز", "درخشان", "بی‌نقص", "جذاب",
    "دوگانه", "ماندگار", "فراموش‌نشدنی", "منحصربه‌فرد", "چشمگیر", "افسانه‌ای",
    "رویایی", "سرشار", "پربار", "کامل", "بی‌عیب",
)


# برخلاف _PUBLIC_SPECIFIC_POINT_MARKERS (که برای فیلترِ واقعیت‌های ورودی
# ساخته شده و اسم‌های کلیِ موضوعی مثل «جهان»/«داستان»/«شخصیت» را هم دارد)،
# این فهرست عمداً آن اسم‌های کلی را ندارد. اگر آن‌ها اینجا می‌ماندند، هر
# جمله‌ای که فقط موضوعش را نام می‌برد (مثلاً «این جهان») بدون هیچ جزئیاتِ
# واقعی، به‌اشتباه «مشخص» به حساب می‌آمد.
_PROSE_CONCRETE_DETAIL_MARKERS = (
    "اکتشاف", "کاوش", "تعامل", "مبارز", "نبرد", "باس", "رئیس", "پازل",
    "سیستم", "مکانیک", "کنترل", "اسب", "نوشتار", "ترجمه", "ماموریت",
    "عملکرد", "فنی", "باگ", "موجودی", "ذخیره", "پیشرفت",
)


def _sentence_lacks_concrete_backing(sentence: str) -> bool:
    """
    یک جمله را رد می‌کند اگر فقط با صفتِ کلی تعریف کند، بدون هیچ جزئیات مشخصی
    در همان جمله. این چک روی خودِ ریشه‌ی کلمه کار می‌کند، نه یک عبارتِ دقیقِ
    از پیش‌نویسی‌شده -- بنابراین با عوض کردن مترادف دور زده نمی‌شود.

    یک استثنا: اگر جمله صریحاً همان صفتِ کلی را نفی یا رد می‌کند (مثلاً «نه با
    یک لحظه‌ی درخشان، بلکه...»)، رد نمی‌شود -- این خودش نشانه‌ی پرهیز از
    تعریفِ خالی است، نه خودِ مشکل.
    """
    folded = clean_text(sentence).casefold()
    has_generic = any(marker in folded for marker in _PROSE_GENERIC_ADJECTIVE_MARKERS)
    if not has_generic:
        return False

    has_specific = any(marker in folded for marker in _PROSE_CONCRETE_DETAIL_MARKERS)
    if has_specific:
        return False

    negation_markers = ("نه ", "نه‌فقط", "نه‌تنها", " نیست", "به‌جای", "بدون ", "فاقد ")
    if any(marker in folded for marker in negation_markers):
        return False

    return True


def _editorial_claim_quality_reason(section_key: str, text_fa: str) -> str | None:
    """چند خطای معناییِ پرتکرار را پیش از پذیرش مقاله می‌گیرد، نه با چک طولِ بی‌معنا."""
    text = clean_text(text_fa).casefold()
    forbidden_by_section = {
        "opening": (
            "داستانی جذاب", "شخصیت‌های جذاب", "تجربه‌ای دوگانه", "وعده‌های بزرگ",
        ),
        "world_gameplay": (
            "سیستم‌های بازی به طور نامشخص", "سیستم‌های بازی نامشخص",
            "سیستم‌های ناسازگار", "به سمت حدس زدن", "باعث سردرگمی",
            "نبردهای رئیس", "سیستم موجودی", "سینه‌های ذخیره‌سازی",
            "سرعت پیشرفت", "کند کردن پیشرفت",
        ),
        "audience": (
            "کمی صبر کنید", "بهتر است صبر کنید", "صبر کنید یا",
            "بعداً بخرید", "بعدا بخرید",
        ),
        "conclusion": (
            "نیاز به بهبود", "بازیکنان را رها می‌کند", "جستجوی یک تجربه بهتر",
        ),
    }
    for phrase in forbidden_by_section.get(section_key, ()):
        if phrase in text:
            return f"عبارت یا ادعای کلیِ نامعتبر دارد: «{phrase}»"

    generic_phrases = (
        "به طرز شگفت‌انگیزی", "از هر گوشه", "ساعت‌ها در این دنیا",
        "تجربه‌ای دوگانه", "مشکلات جدی",
    )
    for phrase in generic_phrases:
        if phrase in text:
            return f"عبارت کلی و کم‌اطلاع دارد: «{phrase}»"

    # چکِ سطحِ جمله فقط برای بخش‌هایی اعمال می‌شود که مستقیماً از واقعیت‌های
    # مشخصِ بازی ساخته شده‌اند (شروع، جهان/گیم‌پلی، اصطکاک‌ها). بخش‌های «مخاطب»
    # و «جمع‌بندی» ذاتاً زبانِ چارچوب‌بندی/توصیه دارند که واژگانِ مکانیک‌محور
    # را در هر جمله نخواهد داشت؛ اجباری کردنِ آن روی این دو بخش، مثبتِ کاذب
    # زیاد تولید می‌کند بدون این‌که مشکلِ واقعی را بگیرد.
    if section_key in ("opening", "world_gameplay", "friction"):
        for sentence in re.split(r"(?<=[.!؟?])\s+", clean_text(text_fa)):
            if sentence and _sentence_lacks_concrete_backing(sentence):
                return f"جمله‌ی کلی و بدون جزئیاتِ مشخص دارد: «{sentence.strip()}»"

    return None


# این اعداد سقفِ عقل‌سلیم‌اند، نه هدفِ سخت‌گیرانه. هدفِ واقعیِ ۴۵۰ تا ۵۵۰ واژه
# فقط در پرامپت به مدل گفته می‌شود؛ اعتبارسنجی فقط جلوی متنِ واقعاً ناقص یا
# واقعاً افسارگسیخته را می‌گیرد، نه هر انحرافی از آن هدف را.
_SINGLE_PASS_SECTION_SPECS = {
    "opening": {"min_words": 24, "max_words": 220},
    "world_gameplay": {"min_words": 55, "max_words": 420},
    "friction": {"min_words": 55, "max_words": 420},
    "audience": {"min_words": 24, "max_words": 220},
    "conclusion": {"min_words": 24, "max_words": 220},
}


def _validate_single_pass_editorial_per_section(
    raw,
    *,
    plan: dict[str, list[dict]],
    allowed_site_names: set[str],
) -> dict[str, dict]:
    """
    برخلاف _validate_single_pass_editorial، در اولین خطا متوقف نمی‌شود؛ نتیجه‌ی
    هر بخش را جدا برمی‌گرداند تا پاسِ نگارشِ هدفمند فقط سراغ همان بخش(های)
    مردود برود، نه این‌که کل مقاله را از نو بنویسد یا مستقیم fallback بزند.
    """
    results: dict[str, dict] = {}

    if not isinstance(raw, dict):
        for key in _SINGLE_PASS_SECTION_SPECS:
            results[key] = {"ok": False, "reason": "فرمت JSON مقاله معتبر نیست"}
        return results

    for key, spec in _SINGLE_PASS_SECTION_SPECS.items():
        text_fa = _editorial_text_value(raw.get(key))
        if not _public_section_is_safe(
            text_fa,
            min_words=spec["min_words"],
            max_words=spec["max_words"],
            allowed_site_names=allowed_site_names,
        ):
            if not text_fa:
                reason = f"بخش {key}: متن خالی یا فرمت نامعتبر است"
            else:
                words = len(text_fa.split())
                if words < spec["min_words"]:
                    reason = f"بخش {key}: متن کوتاه است ({words} واژه، حداقل {spec['min_words']})"
                elif words > spec["max_words"]:
                    reason = f"بخش {key}: متن بلند است ({words} واژه، حداکثر {spec['max_words']})"
                else:
                    reason = f"بخش {key}: متن برای انتشار امن نیست"
            results[key] = {"ok": False, "reason": reason}
            continue

        claim_quality_reason = _editorial_claim_quality_reason(key, text_fa)
        if claim_quality_reason:
            results[key] = {"ok": False, "reason": f"بخش {key}: {claim_quality_reason}"}
            continue

        assigned_ids = [item["id"] for item in plan.get(key, []) if item.get("id")]
        results[key] = {"ok": True, "text_fa": text_fa, "supports": assigned_ids}

    return results


_TRIM_PASS_SECTION_ORDER = ("opening", "world_gameplay", "friction", "audience", "conclusion")

_SECTION_DISPLAY_LABELS = {
    "opening": "شروع نقد",
    "world_gameplay": "جهان بازی و گیم‌پلی",
    "friction": "اصطکاک‌ها و ضعف‌ها",
    "audience": "مخاطب مناسب",
    "conclusion": "جمع‌بندی",
}


def _polish_pass_is_safe(
    original_texts: dict[str, str],
    edited_raw,
    allowed_site_names: set[str],
) -> tuple[bool, str | None]:
    """
    برخلاف یک پاسِ صرفاً کوتاه‌کننده، این نسخه اجازه‌ی بهبود هم می‌دهد: می‌تواند
    یک صفتِ کلی را با جزئیاتی که از قبل جای دیگری در همین مقاله تأیید شده
    جایگزین کند. آنچه هرگز مجاز نیست، وارد کردن یک جزئیاتِ مشخصِ کاملاً تازه
    است که نه در همین بخش و نه در بقیه‌ی مقاله‌ی اصلی نبوده -- این دقیقاً
    مرزِ «بازآرایی از روی مطالبِ تأییدشده» در برابر «اختراعِ ادعای تازه» است.
    """
    if not isinstance(edited_raw, dict):
        return False, "خروجی ویرایش JSON معتبر نیست"

    full_original_text = " ".join(
        clean_text(original_texts.get(key, "")) for key in _TRIM_PASS_SECTION_ORDER
    ).casefold()

    for key in _TRIM_PASS_SECTION_ORDER:
        original_text = clean_text(original_texts.get(key, ""))
        edited_text = _editorial_text_value(edited_raw.get(key))

        if not edited_text:
            return False, f"بخش {key} در ویرایش خالی برگشته است"
        if _has_broken_character(edited_text):
            return False, f"بخش {key} پس از ویرایش نویسه‌ی خراب دارد"

        original_words = len(original_text.split()) or 1
        edited_words = len(edited_text.split())

        # این پاس هم می‌تواند کوتاه کند و هم بهبود دهد، اما نباید عملاً مقاله
        # را از نو بنویسد یا به‌شکل مشکوکی متورم کند.
        if edited_words > original_words * 1.35 + 8:
            return False, f"بخش {key} بیش از حد بلندتر شده است (احتمال بازنویسیِ کامل به‌جای بهبود)"
        if edited_words < original_words * 0.5:
            return False, f"بخش {key} بیش از حد کوتاه شده است"

        if not _extract_numbers(edited_text).issubset(_extract_numbers(original_text)):
            return False, f"بخش {key} عدد تازه‌ای اضافه کرده است"

        for site_name in allowed_site_names:
            if site_name and site_name.casefold() in edited_text.casefold():
                return False, f"بخش {key} نام یک منبع را اضافه کرده است"

        if _editorial_claim_quality_reason(key, edited_text):
            return False, f"بخش {key} پس از ویرایش همچنان عبارت کلی یا نامعتبر دارد"

        # جایگزینیِ صفتِ کلی با جزئیاتِ از-قبل-تأییدشده مجاز است؛ اختراعِ
        # جزئیاتِ کاملاً تازه (نه در همین بخش، نه در بقیه‌ی مقاله) مجاز نیست.
        folded_edited = edited_text.casefold()
        folded_own_original = original_text.casefold()
        for marker in _PUBLIC_SPECIFIC_POINT_MARKERS:
            if (
                marker in folded_edited
                and marker not in folded_own_original
                and marker not in full_original_text
            ):
                return False, (
                    f"بخش {key} به‌جای بازآرایی، جزئیاتِ کاملاً تازه‌ای اضافه کرده "
                    f"که نه در همین بخش و نه در بقیه‌ی مقاله نبوده"
                )

    return True, None


def apply_editorial_polish_pass(
    client: OpenAI,
    sections: dict,
    allowed_site_names: set[str],
) -> dict:
    """
    یک پاسِ ویرایشیِ محدود درست بعد از تولید مقاله‌ی تأییدشده. برخلاف یک پاسِ
    صرفاً کوتاه‌کننده، این پاس هم می‌تواند حذف/کوتاه کند و هم بهبود دهد: مثلاً
    یک صفتِ کلیِ بی‌پشتوانه مثل «پرجزئیات» را با ارجاع به جزئیاتی که از قبل
    جای دیگری در همین مقاله تأیید شده جایگزین کند. آنچه هرگز مجاز نیست،
    اختراعِ یک جزئیات یا ادعای کاملاً تازه است.

    اگر OpenAI در دسترس نباشد یا خروجی از آزمون‌های ایمنی رد شود، دقیقاً همان
    مقاله‌ی قبل از ویرایش بدون تغییر برگردانده می‌شود -- این پاس هرگز یک مقاله‌ی
    خوب را به fallback خشک تبدیل نمی‌کند و هرگز کل مقاله را از نو نمی‌نویسد.
    """
    if client is None:
        return sections

    original_texts = {
        key: clean_text(str(sections.get(f"{key}_fa") or ""))
        for key in _TRIM_PASS_SECTION_ORDER
    }

    prompt = f"""
You are a copy editor for a Persian game review, NOT its writer. You receive five
already-approved sections. You may:
1. Delete or shorten broken text, unsupported absolute claims, or repeated phrasing.
2. IMPROVE a vague/generic adjective (e.g. "detailed", "rich", "amazing", "stunning")
   by replacing it with a reference to a concrete detail -- BUT ONLY if that concrete
   detail is ALREADY stated somewhere in the five sections below (this section or
   another one). You are reusing/rephrasing existing approved material, never adding
   anything new.

You must NEVER add a new fact, a new claim, a new number, or any concrete detail that
is not already present somewhere in the five sections below.

Sections (Persian labels shown for context only, keys stay in English in your reply):
{json.dumps({_SECTION_DISPLAY_LABELS[key]: original_texts[key] for key in _TRIM_PASS_SECTION_ORDER}, ensure_ascii=False, indent=2)}

Fix these problems, by cutting, shortening, or substituting with already-established
material only:
1. Broken or garbled characters.
2. Unsupported absolute claims ("the best game ever", "flawless", "undeniably",
   "definitely", "without question").
3. A fact or phrase repeated across sections, or repeated within one section using a
   different adjective. Keep the first occurrence; cut or replace the repeat.
4. Any generic marketing adjective (rich, detailed, amazing, stunning, gripping, etc.)
   that has no concrete backing in the same sentence -- either delete it, or replace
   it with a short reference to a concrete detail already established elsewhere in
   these five sections. Do not invent a new detail to justify it.
5. Over-definitive concluding language that claims more certainty than the sections
   themselves support.

If a section already has none of these problems, return it completely unchanged,
character for character.

Return exactly this JSON object, with English keys, one per section:
{{
  "opening": {{"text_fa": "..."}},
  "world_gameplay": {{"text_fa": "..."}},
  "friction": {{"text_fa": "..."}},
  "audience": {{"text_fa": "..."}},
  "conclusion": {{"text_fa": "..."}}
}}
""".strip()

    try:
        raw = ask_openai_json(client, prompt, max_tokens=5000)
    except Exception as exc:
        print(f"Editorial polish pass failed, keeping pre-edit article: {repr(exc)}")
        return sections

    is_safe, reason = _polish_pass_is_safe(original_texts, raw, allowed_site_names)
    if not is_safe:
        print(f"Editorial polish pass rejected ({reason}); keeping pre-edit article.")
        return sections

    result = dict(sections)
    changed_count = 0
    for key in _TRIM_PASS_SECTION_ORDER:
        edited_text = _editorial_text_value(raw.get(key))
        if edited_text and edited_text != original_texts[key]:
            changed_count += 1
        result[f"{key}_fa"] = edited_text or original_texts[key]

    result["word_count"] = sum(
        len(result[f"{key}_fa"].split()) for key in _TRIM_PASS_SECTION_ORDER
    )
    if changed_count:
        print(f"Editorial polish pass improved {changed_count} section(s).")
        result["method"] = f"{result.get('method', '')}+polish_pass"
    else:
        print("Editorial polish pass found nothing to change.")

    return result


def _write_public_article_sections(client: OpenAI, facts: dict) -> dict:
    """
    کل مقاله را می‌سازد. هر بخشی که یک‌بار معتبر شود -- چه در تلاش اول، چه با
    پاسِ نگارشِ هدفمند، چه در تلاشِ کاملِ دوم -- برای همیشه نگه داشته می‌شود؛
    هیچ تلاشِ بعدی یک بخشِ از قبل تأییدشده را دور نمی‌ریزد. اگر در پایان هم
    یک یا دو بخش هنوز معتبر نشوند، فقط همان بخش(های) با متنِ قالب‌محورِ
    قطعی پر می‌شوند -- نه کل مقاله. fallbackِ قالب‌محور فقط وقتی کل مقاله را
    جایگزین می‌کند که از ابتدا شواهدِ کافی برای ساختنِ نقشه‌ی سرمقاله نبوده،
    یا واقعاً هیچ بخشی از هیچ تلاشی قابل‌استفاده نشده باشد.
    """
    allowed_site_names = {
        clean_text(str(source.get("site_name") or ""))
        for source in facts.get("sources", [])
        if clean_text(str(source.get("site_name") or ""))
    }
    if client is None or len(_fact_map(facts)) < 10:
        return _build_public_article_fallback(facts)

    plan = _single_pass_editorial_plan(facts)
    if plan is None:
        print(
            "Single-pass editorial plan lacks enough verified reporting facts "
            f"({len(_fact_map(facts))} available); using deterministic fallback."
        )
        return _build_public_article_fallback(facts)

    best: dict[str, dict] = {}
    last_validation_reason = ""

    for attempt in range(2):
        prompt = _single_pass_prompt(facts, plan, retry_reason=last_validation_reason)
        try:
            raw = ask_openai_json(client, prompt, max_tokens=5000)
        except Exception as exc:
            print(f"Single-pass editorial article attempt {attempt + 1} failed: {repr(exc)}")
            raw = None

        if raw is not None:
            per_section = _validate_single_pass_editorial_per_section(
                raw, plan=plan, allowed_site_names=allowed_site_names
            )
            for key, result in per_section.items():
                if key in best:
                    continue  # قبلاً معتبر شده؛ هیچ تلاشِ بعدی آن را دور نمی‌ریزد.
                if result["ok"]:
                    best[key] = {
                        "text_fa": result["text_fa"],
                        "supports": result["supports"],
                        "method": "openai_full_pass" if attempt == 0 else "openai_full_retry",
                    }
                    continue

                last_validation_reason = result["reason"]
                # پیش از رفتن به تلاشِ کاملِ بعدی یا fallback، پاسِ نگارشِ
                # هدفمند فقط همین یک بخش را امتحان می‌کند.
                spec = _SINGLE_PASS_SECTION_SPECS[key]
                assigned_facts = plan.get(key, [])
                if not assigned_facts:
                    continue
                fix = _write_targeted_section_revision(
                    client,
                    key=key,
                    game=facts.get("game", ""),
                    assigned_facts=assigned_facts,
                    spec=spec,
                    allowed_site_names=allowed_site_names,
                    failure_reason=result["reason"],
                )
                if fix is not None:
                    best[key] = {
                        "text_fa": fix["text_fa"],
                        "supports": fix["supports"],
                        "method": "openai_targeted_fix",
                    }

        if len(best) == 5:
            break

        remaining = [key for key in _SINGLE_PASS_SECTION_SPECS if key not in best]
        if attempt == 0 and remaining:
            print(
                f"Section(s) still unresolved after attempt 1 ({', '.join(remaining)}); "
                "retrying the whole article once, but keeping every section already "
                "validated."
            )

    if len(best) < 5:
        missing_keys = [key for key in _SINGLE_PASS_SECTION_SPECS if key not in best]
        print(
            f"Could not get valid AI text for: {', '.join(missing_keys)}. Filling only "
            "those sections with deterministic text; every AI-written section that "
            "already passed validation is kept exactly as-is."
        )
        deterministic = _build_public_article_fallback(facts)
        for key in missing_keys:
            best[key] = {
                "text_fa": deterministic.get(f"{key}_fa", ""),
                "supports": [item["id"] for item in plan.get(key, [])],
                "method": "deterministic_section_fallback",
            }

    total_words = sum(len(value["text_fa"].split()) for value in best.values())
    all_ai_written = all(value["method"].startswith("openai") for value in best.values())
    sections = {
        "opening_fa": best["opening"]["text_fa"],
        "world_gameplay_fa": best["world_gameplay"]["text_fa"],
        "friction_fa": best["friction"]["text_fa"],
        "audience_fa": best["audience"]["text_fa"],
        "conclusion_fa": best["conclusion"]["text_fa"],
        "section_supports": {key: value["supports"] for key, value in best.items()},
        "editorial_plan": {key: [item["id"] for item in items] for key, items in plan.items()},
        "section_methods": {key: value["method"] for key, value in best.items()},
        "method": (
            "openai_single_pass_grounded_editorial_v34"
            if all_ai_written
            else "openai_partial_with_deterministic_sections_v34"
        ),
        "word_count": total_words,
    }
    # پاسِ ویرایشِ نهایی هم می‌تواند کوتاه کند و هم با ارجاع به جزئیاتِ
    # از-قبل-موجود بهبود دهد؛ هرگز مقاله را از نو نمی‌نویسد و هرگز یک بخشِ خوب
    # را با متنِ قالب‌محور عوض نمی‌کند (رجوع کنید به apply_editorial_polish_pass).
    return apply_editorial_polish_pass(client, sections, allowed_site_names)

def run_public_article_regression_checks() -> None:
    assert not _public_section_is_safe(
        "این متن خراب است � و نباید وارد مقاله شود.",
        min_words=1,
        max_words=40,
        allowed_site_names=set(),
    )
    assert not _public_section_is_safe(
        "این متن درباره‌ی پوشش شواهد و جزئیات داخلی صحبت می‌کند و برای خواننده‌ی عمومی مناسب نیست.",
        min_words=1,
        max_words=40,
        allowed_site_names=set(),
    )
    assert not _public_section_is_safe(
        "این بخش درباره‌ی امتیاز بازی و نمره‌ی منتقدان صحبت می‌کند و نباید در متن اصلی مقاله باشد.",
        min_words=1,
        max_words=40,
        allowed_site_names=set(),
    )
    assert _public_section_is_safe(
        "بازی در بهترین لحظاتش میان اکتشاف، سیستم‌های متنوع و درگیری‌های پرانرژی تعادل جذابی پیدا می‌کند.",
        min_words=1,
        max_words=40,
        allowed_site_names=set(),
    )

    generic = {
        "point_fa": "بازی تجربه‌ای جذاب و شگفت‌انگیز ارائه می‌دهد.",
        "evidence_en": "The game is wonderful and impossible to put down.",
    }
    specific = {
        "point_fa": "مبارزات با برخی از رئیس‌ها احساس ناعادلانه‌ای دارند.",
        "evidence_en": "Several boss fights feel unbalanced and punish mistakes too heavily.",
    }
    assert not _public_fact_is_specific(generic["point_fa"], generic["evidence_en"])
    assert _public_fact_is_specific(specific["point_fa"], specific["evidence_en"])




def run_single_pass_editorial_regression_checks() -> None:
    facts = {
        "game": "Sample Game",
        "facts": [
            {"id": "F1", "topic": "world_design", "sentiment": "positive", "text_fa": "جهان بازی مسیرهای متنوعی برای اکتشاف دارد."},
            {"id": "F2", "topic": "gameplay", "sentiment": "negative", "text_fa": "برخی نبردهای رئیس تعادل مناسبی ندارند."},
            {"id": "F3", "topic": "gameplay", "sentiment": "positive", "text_fa": "کنترل حرکت در مبارزه روان و پاسخ‌گو است."},
            {"id": "F4", "topic": "world_design", "sentiment": "positive", "text_fa": "تعامل با محیط بازیکن را به جست‌وجو تشویق می‌کند."},
            {"id": "F5", "topic": "story", "sentiment": "negative", "text_fa": "روایت در برخی بخش‌ها عمق کافی ندارد."},
            {"id": "F6", "topic": "technical", "sentiment": "caution", "text_fa": "افت عملکرد در زمان‌های شلوغ گزارش شده است."},
            {"id": "F7", "topic": "gameplay", "sentiment": "positive", "text_fa": "سیستم‌های پیشرفت حس رشد تدریجی ایجاد می‌کنند."},
            {"id": "F8", "topic": "story", "sentiment": "negative", "text_fa": "بعضی مأموریت‌ها تکراری می‌شوند."},
            {"id": "F9", "topic": "world_design", "sentiment": "positive", "text_fa": "طراحی محیط حس کشف را تقویت می‌کند."},
            {"id": "F10", "topic": "gameplay", "sentiment": "negative", "text_fa": "مدیریت موجودی گاهی دست‌وپاگیر است."},
        ],
    }
    plan = _single_pass_editorial_plan(facts)
    assert plan is not None
    reporting_ids = [
        item["id"]
        for key in ("opening", "world_gameplay", "friction")
        for item in plan[key]
    ]
    assert len(reporting_ids) == len(set(reporting_ids))
    assert len(plan["opening"]) == 2 and len(plan["friction"]) == 3

    # Editorial validation must accept a properly-lengthed five-part article
    # that lands inside the new 450–550-word target band, not just any
    # grounded text regardless of length.
    concise_raw = {
        "opening": {"text_fa": "این بازی میان فرصت‌های فراوان برای درگیر شدن با جهانش و چند مانع طراحی‌شده در مسیر پیشرفت، تجربه‌ای نابرابر اما قابل توجه می‌سازد. کیفیت کلی آن نه با یک لحظه‌ی درخشان، بلکه با توان بازیکن برای کنار آمدن با این نوسان دائمی سنجیده می‌شود، و همین نوسان تا پایان بازی هم به‌طور کامل برطرف نمی‌شود.", "supports": [item["id"] for item in plan["opening"]]},
        "world_gameplay": {"text_fa": "دنیای بازی فرصت کاوش و تعامل را جدی می‌گیرد و همین موضوع در بهترین لحظات، ریتم تجربه را جلو می‌برد. وقتی بازیکن مسیر خودش را انتخاب می‌کند و به‌جای دنبال کردن یک خط مستقیم، به کاوش در گوشه‌های کمتر دیده‌شده‌ی نقشه می‌رود، جزئیات محیطی و امکان واکنش به موقعیت‌های تازه، حس ماجراجویی را زنده نگه می‌دارد. کنترل حرکت هنگام مبارزه هم به همین حس کمک می‌کند، چون واکنش‌ها سریع و قابل پیش‌بینی‌اند و بازیکن به‌جای حدس زدن، روی مهارت خودش حساب می‌کند. زنجیره کردن حرکات پیاپی بدون از دست دادن کنترل، حتی در میانه‌ی درگیری‌های شلوغ هم روان می‌ماند، و امکان جابه‌جایی سریع بین سلاح‌ها همین ریتم را در مبارزات طولانی‌تر هم حفظ می‌کند. این ترکیب باعث می‌شود حتی مسیرهای فرعی هم ارزش وقت گذاشتن داشته باشند.", "supports": [item["id"] for item in plan["world_gameplay"]]},
        "friction": {"text_fa": "در سوی دیگر، چند سیستم طراحی‌شده گاهی به‌جای ساختن فشار جذاب، مسیر بازی را کند می‌کنند. مدیریت موجودی یکی از این نقطه‌هاست: نبود جای کافی برای نگهداری منابع، بازیکن را مجبور می‌کند مدام به منوها برگردد و همین وقفه، ریتم اکتشاف را می‌شکند. برخی مبارزات با رئیس‌ها هم تعادل مشخصی ندارند و به‌جای آزمودن مهارت، بیشتر به حفظ کردن الگوی حمله شبیه می‌شوند، طوری که یک اشتباه ساده می‌تواند کل تلاش چند دقیقه‌ای را از بین ببرد و بازیکن را وادار به تکرار همان توالی کند. این ایرادها باعث می‌شوند رسیدن به لحظات خوب همیشه به اندازه‌ی خود آن لحظات روان نباشد و بازیکن باید مدام با این ناهماهنگی کنار بیاید.", "supports": [item["id"] for item in plan["friction"]]},
        "audience": {"text_fa": "برای بازیکنی که از آزمون‌وخطا، کشف تدریجی و ساختن مسیر شخصی در دنیایی بزرگ لذت می‌برد، این تجربه می‌تواند جذاب باشد و ساعت‌های زیادی از او بگیرد، بی‌آنکه احساس اتلاف وقت کند. کسانی که ریتم کاملاً روان، مدیریت ساده‌تر منابع و راهنمایی دائمی می‌خواهند، بهتر است با انتظار محتاطانه‌تری وارد آن شوند، چون همین نقطه‌ها می‌توانند به‌مرور خستگی‌شان کنند و انگیزه‌شان را کم کنند.", "supports": [item["id"] for item in plan["audience"]]},
        "conclusion": {"text_fa": "نتیجه، اثری بلندپروازانه است که ارزشش به میزان صبر بازیکن و علاقه‌اش به درگیری با سیستم‌های متعدد بستگی دارد. در بهترین حالت، تجربه‌ای ماندگار می‌سازد که کاوش و کنترل مبارزه محور اصلی آن است؛ در بدترین حالت، مدیریت موجودی و تعادل نامنظم مبارزات، همان بلندپروازی را به مانعی برای لذت بردن از همان کاوش تبدیل می‌کنند.", "supports": [item["id"] for item in plan["conclusion"]]},
    }
    def _validate_full_article(raw, plan, allowed_site_names):
        """
        معادلِ دقیقِ همان چیزی که _write_public_article_sections واقعاً صدا
        می‌زند، نه wrapper از رده خارج‌شده‌ای که دیگر در مسیر تولید نیست.
        """
        per_section = _validate_single_pass_editorial_per_section(
            raw, plan=plan, allowed_site_names=allowed_site_names
        )
        for key, result in per_section.items():
            if not result["ok"]:
                return None, result["reason"]
        normalized = {
            key: {"text_fa": result["text_fa"], "supports": result["supports"]}
            for key, result in per_section.items()
        }
        return normalized, None

    normalized, reason = _validate_full_article(concise_raw, plan, set())
    assert normalized is not None, reason
    total_words = sum(len(value["text_fa"].split()) for value in normalized.values())
    # این عدد صرفاً برای مستندسازی است: نمونه‌ی fixture عمداً نزدیک به هدفِ
    # ۴۵۰ تا ۵۵۰ نوشته شده، اما خودِ validator دیگر با این بازه قبول/رد
    # نمی‌کند -- فقط سقفِ عقل‌سلیمِ ۱۱۰۰ واژه را نگه می‌دارد (رجوع کنید به
    # run_length_is_a_goal_not_a_wall_regression_checks برای آزمونِ خودِ این رفتار).
    assert 300 <= total_words <= 1100, f"fixture total word count unexpectedly out of range: {total_words}"
    assert len(plan["audience"]) == 2 and len(plan["conclusion"]) == 2
    assert all(item["id"] in reporting_ids for item in plan["audience"] + plan["conclusion"])

    # A truly runaway section (not just "over the soft 450-550 goal") must still
    # be rejected by the per-section ceiling.
    bloated_raw = dict(concise_raw)
    bloated_raw["world_gameplay"] = {
        "text_fa": concise_raw["world_gameplay"]["text_fa"] + (" " + "کلمه " * 400).strip(),
        "supports": concise_raw["world_gameplay"]["supports"],
    }
    bloated_normalized, bloated_reason = _validate_full_article(bloated_raw, plan, set())
    assert bloated_normalized is None
    assert bloated_reason and "بلند" in bloated_reason

def run_longform_retry_regression_checks() -> None:
    sample_facts = {
        "F1": {"topic": "world_design", "sentiment": "positive"},
        "F2": {"topic": "story", "sentiment": "negative"},
    }
    reason = _public_section_validation_reason(
        {"text_fa": "کوتاه است.", "supports": ["F1", "F2"]},
        allowed_fact_ids=set(sample_facts),
        fact_by_id=sample_facts,
        min_words=10,
        max_words=30,
        min_supports=2,
        required_topics=None,
        required_sentiments=None,
        allowed_site_names=set(),
    )
    assert reason and "کوتاه" in reason
    assert _section_has_balanced_supports(
        {"supports": ["F1", "F2"]},
        sample_facts,
    )


def run_length_is_a_goal_not_a_wall_regression_checks() -> None:
    """
    این تست دقیقاً همان اصلاحیه را قفل می‌کند: طول در پرامپت هدف است، نه
    دیوارِ رد کردن. اعتبارسنجی فقط باید متنِ واقعاً ناقص را رد کند، و پیش از
    تصمیمِ fallback باید یک پاسِ نگارشِ هدفمند امتحان شود.
    """
    # ۱) کف هر بخش دقیقاً همان عددهایی است که قرار بود باشد، نه اعداد سخت‌گیرانه‌تر.
    assert _SINGLE_PASS_SECTION_SPECS["opening"]["min_words"] == 24
    assert _SINGLE_PASS_SECTION_SPECS["world_gameplay"]["min_words"] == 55
    assert _SINGLE_PASS_SECTION_SPECS["friction"]["min_words"] == 55
    assert _SINGLE_PASS_SECTION_SPECS["audience"]["min_words"] == 24
    assert _SINGLE_PASS_SECTION_SPECS["conclusion"]["min_words"] == 24

    facts = {
        "game": "Sample Game",
        "facts": [
            {"id": "F1", "topic": "world_design", "sentiment": "positive", "text_fa": "دنیای بازی کاوش را تشویق می‌کند.", "site_name": "IGN"},
            {"id": "F2", "topic": "gameplay", "sentiment": "positive", "text_fa": "کنترل مبارزه روان است.", "site_name": "PC Gamer"},
            {"id": "F3", "topic": "gameplay", "sentiment": "negative", "text_fa": "مدیریت موجودی دست‌وپاگیر است.", "site_name": "IGN"},
            {"id": "F4", "topic": "story", "sentiment": "negative", "text_fa": "روایت عمق کافی ندارد.", "site_name": "PC Gamer"},
            {"id": "F5", "topic": "world_design", "sentiment": "positive", "text_fa": "طراحی محیط حس اکتشاف می‌سازد.", "site_name": "IGN"},
            {"id": "F6", "topic": "technical", "sentiment": "caution", "text_fa": "افت عملکرد در صحنه‌های شلوغ دیده می‌شود.", "site_name": "PC Gamer"},
            {"id": "F7", "topic": "gameplay", "sentiment": "positive", "text_fa": "زنجیره کردن حرکات مبارزه حس رضایت‌بخشی دارد.", "site_name": "IGN"},
            {"id": "F8", "topic": "story", "sentiment": "negative", "text_fa": "برخی مأموریت‌های فرعی تکراری‌اند.", "site_name": "PC Gamer"},
            {"id": "F9", "topic": "world_design", "sentiment": "positive", "text_fa": "جزئیات محیطی حس کاوش را زنده نگه می‌دارد.", "site_name": "IGN"},
            {"id": "F10", "topic": "gameplay", "sentiment": "negative", "text_fa": "برخی نبردهای رئیس تعادل مناسبی ندارند.", "site_name": "PC Gamer"},
        ],
    }
    plan = _single_pass_editorial_plan(facts)
    if plan is None:
        # این fixture کوچک برای آزمودن plan واقعی کافی نیست؛ فقط رفتار پچ را
        # با یک plan دستی می‌سنجیم.
        plan = {
            "opening": [facts["facts"][0], facts["facts"][2]],
            "world_gameplay": [facts["facts"][0], facts["facts"][1], facts["facts"][4]],
            "friction": [facts["facts"][2], facts["facts"][3], facts["facts"][5]],
            "audience": [facts["facts"][0], facts["facts"][3]],
            "conclusion": [facts["facts"][0], facts["facts"][3]],
        }

    # ۲) مقاله‌ای که هر بخشش بین کفِ جدید (۲۴/۵۵/۵۵/۲۴/۲۴) و کفِ قدیمیِ
    # سخت‌گیرانه‌تر (۳۵/۸۰/۸۰/۴۰/۴۵) است، دیگر رد نمی‌شود.
    borderline_raw = {
        "opening": {"text_fa": "بازی در بخشی از تجربه‌اش کاوش را جدی می‌گیرد و بازیکن را درگیر می‌کند، هرچند یک اصطکاک مشخص هم در پس‌زمینه‌اش از ابتدا تا انتهای مسیر باقی می‌ماند و هرگز کاملاً برطرف نمی‌شود."},
        "world_gameplay": {"text_fa": "کنترل حرکت هنگام مبارزه روان است و واکنش‌ها سریع و قابل پیش‌بینی‌اند، طوری که زنجیره کردن حمله‌های پیاپی حس رضایت‌بخشی می‌سازد و حتی در میانه‌ی درگیری‌های شلوغ هم از دست نمی‌رود. طراحی محیط هم بازیکن را به کاوش گوشه‌های کمتر دیده‌شده‌ی نقشه تشویق می‌کند و همین حس کنجکاوی، انگیزه‌ی ادامه دادن را تا پایان زنده نگه می‌دارد."},
        "friction": {"text_fa": "مدیریت موجودی بازی هم دست‌وپاگیر است، چون بازیکن را مدام و بدون دلیل روشنی به منوهای فرعی برمی‌گرداند و همین وقفه‌ی مکرر، ریتم اکتشاف را می‌شکند و لذت کاوش را به‌مرور کم می‌کند. روایت هم در بخش‌های میانی عمق کافی ندارد و همین کم‌عمقی آشکار، همراهی احساسی بازیکن با شخصیت‌های اصلی داستان را به‌مرور سست می‌کند."},
        "audience": {"text_fa": "برای بازیکنی که کاوش تدریجی و آزمون‌وخطا را دوست دارد، این تجربه مناسب و قابل توصیه است. برای کسی که روایت منسجم و ریتم بدون وقفه در اولویت اصلی‌اش قرار دارد، جذابیت کمتری خواهد داشت."},
        "conclusion": {"text_fa": "ارزش این بازی در نهایت به میزان تحمل بازیکن برای این اصطکاک‌های مشخص، در برابر لذتی که کاوش و کنترل روان مبارزه به او می‌دهند، بستگی پیدا می‌کند."},
    }
    for key, value in borderline_raw.items():
        words = len(value["text_fa"].split())
        old_strict_min = {"opening": 35, "world_gameplay": 80, "friction": 80, "audience": 40, "conclusion": 45}[key]
        assert words < old_strict_min, f"fixture for {key} should sit below the old, over-tightened floor to prove the point ({words} words)"
        assert words >= _SINGLE_PASS_SECTION_SPECS[key]["min_words"]

    def _validate_full_article(raw, plan, allowed_site_names):
        per_section = _validate_single_pass_editorial_per_section(
            raw, plan=plan, allowed_site_names=allowed_site_names
        )
        for key, result in per_section.items():
            if not result["ok"]:
                return None, result["reason"]
        normalized = {
            key: {"text_fa": result["text_fa"], "supports": result["supports"]}
            for key, result in per_section.items()
        }
        return normalized, None

    normalized, reason = _validate_full_article(borderline_raw, plan, set())
    assert normalized is not None, f"a borderline-short-but-not-incomplete article must be accepted, got: {reason}"

    # ۳) و ۴) پیش از تصمیمِ fallback، پاسِ نگارشِ هدفمند باید فقط بخشِ ناقص را
    # اصلاح کند -- و این باید از خودِ _write_public_article_sections (تابعی
    # که واقعاً در تولید صدا زده می‌شود) آزموده شود، نه یک wrapper واسطه‌ای که
    # دیگر در مسیر اصلی نیست.
    facts["sources"] = [{"site_name": "IGN"}, {"site_name": "PC Gamer"}]

    incomplete_raw = dict(borderline_raw)
    incomplete_raw["opening"] = {"text_fa": "خیلی کوتاه است."}

    def fake_ask_openai_json_recovers_via_patch(client, prompt, max_tokens=None):
        if "revising ONLY one section" in prompt:
            assert "شروع نقد" in prompt
            return {"text_fa": borderline_raw["opening"]["text_fa"]}
        return incomplete_raw

    original_ask = globals()["ask_openai_json"]
    globals()["ask_openai_json"] = fake_ask_openai_json_recovers_via_patch
    try:
        recovered_sections = _write_public_article_sections(object(), facts)
    finally:
        globals()["ask_openai_json"] = original_ask

    assert recovered_sections["opening_fa"] == borderline_raw["opening"]["text_fa"]
    # بخش‌های دیگر که از اول درست بودند نباید دست بخورند -- این دقیقاً همان
    # «حفظِ بهترین خروجیِ قبلی» است که در نسخه‌ی قبلی رعایت نمی‌شد.
    assert recovered_sections["friction_fa"] == borderline_raw["friction"]["text_fa"]
    assert recovered_sections["section_methods"]["opening"] == "openai_targeted_fix"
    assert recovered_sections["section_methods"]["friction"] == "openai_full_pass"

    # ۵) اگر پاسِ هدفمند هم شکست بخورد و تلاشِ کاملِ دوم هم همان بخش را حل
    # نکند، فقط همان یک بخشِ گیرکرده باید با متنِ قالب‌محور پر شود -- نه این‌که
    # کل مقاله (شاملِ بخش‌هایی که از اول درست بودند) دور ریخته شود.
    def fake_ask_openai_json_stuck_opening(client, prompt, max_tokens=None):
        if "revising ONLY one section" in prompt:
            raise RuntimeError("simulated persistent API failure for the opening section")
        return incomplete_raw

    globals()["ask_openai_json"] = fake_ask_openai_json_stuck_opening
    try:
        partially_stuck_sections = _write_public_article_sections(object(), facts)
    finally:
        globals()["ask_openai_json"] = original_ask

    assert partially_stuck_sections["section_methods"]["opening"] == "deterministic_section_fallback"
    # بخش‌های دیگر همچنان از تلاشِ اول مدل‌اند، نه از fallbackِ کلِ مقاله.
    assert partially_stuck_sections["friction_fa"] == borderline_raw["friction"]["text_fa"]
    assert partially_stuck_sections["section_methods"]["friction"] == "openai_full_pass"
    assert partially_stuck_sections["method"] == "openai_partial_with_deterministic_sections_v34"

    # ۶) مسیر fallback دیگر واقعیتِ خراب را بدون فیلتر قبول نمی‌کند.
    broken_facts = [
        {"id": "B1", "topic": "world_design", "sentiment": "positive", "text_fa": "این متن خراب است \ufffd و نباید وارد شود.", "site_name": "IGN"},
        {"id": "B2", "topic": "world_design", "sentiment": "positive", "text_fa": "این متن سالم است و باید وارد شود.", "site_name": "PC Gamer"},
    ]
    fallback_text = _fallback_text_from_facts(broken_facts, topic={"world_design"}, sentiment={"positive"}, limit=4)
    assert "\ufffd" not in fallback_text
    assert "سالم" in fallback_text


def run_editorial_polish_pass_regression_checks() -> None:
    original_texts = {
        "opening": "این بازی در بهترین لحظاتش کاوش را جدی می‌گیرد، اما یک ضعف مشخص همیشه در پس‌زمینه باقی می‌ماند.",
        "world_gameplay": "کنترل حرکت هنگام مبارزه روان است و زنجیره کردن حرکات پیاپی حس رضایت‌بخشی می‌سازد.",
        "friction": "مدیریت موجودی دست‌وپاگیر است، چون بازیکن را مدام و بدون دلیل روشن به منوهای فرعی برمی‌گرداند.",
        "audience": "برای بازیکنی که از آزمون‌وخطا لذت می‌برد مناسب است، برای بقیه شاید خسته‌کننده باشد.",
        "conclusion": "ارزش این بازی به میزان تحمل بازیکن برای این اصطکاک‌ها بستگی دارد.",
    }

    # 1) A genuine trim (shorter, same numbers/names, no new claims) must pass.
    good_edit = {
        key: {"text_fa": text} for key, text in original_texts.items()
    }
    good_edit["friction"] = {"text_fa": "مدیریت موجودی دست‌وپاگیر است، چون بازیکن را مدام به منوهای فرعی برمی‌گرداند."}
    is_safe, reason = _polish_pass_is_safe(original_texts, good_edit, set())
    assert is_safe, reason

    # 2) An edit that grew far beyond a reasonable improvement (effective rewrite)
    # must still be rejected, even though modest growth is now allowed.
    rewrite_edit = dict(good_edit)
    rewrite_edit["opening"] = {
        "text_fa": (
            original_texts["opening"]
            + " و این موضوع تا انتهای بازی هم با جزئیات بیشتری ادامه پیدا می‌کند و حتی "
            "به شکل‌های تازه‌ای هم بروز می‌کند و بازیکن باید مدام با آن دست‌وپنجه نرم کند "
            "و این خودش به بخش بزرگی از هویت کلی تجربه تبدیل می‌شود."
        )
    }
    is_safe, reason = _polish_pass_is_safe(original_texts, rewrite_edit, set())
    assert not is_safe and "بلندتر" in reason

    # 3) An edit that introduces a brand-new number must be rejected.
    new_number_edit = dict(good_edit)
    new_number_edit["conclusion"] = {"text_fa": "امتیاز این بازی ۹۵ از ۱۰۰ است."}
    is_safe, reason = _polish_pass_is_safe(original_texts, new_number_edit, set())
    assert not is_safe and "عدد" in reason

    # 4) An edit that introduces a source/site name must be rejected.
    site_name_edit = dict(good_edit)
    site_name_edit["audience"] = {"text_fa": original_texts["audience"] + " به گفته‌ی IGN."}
    is_safe, reason = _polish_pass_is_safe(original_texts, site_name_edit, {"IGN"})
    assert not is_safe and "منبع" in reason

    # 5) Over-trimming (losing more than half the section) must be rejected.
    over_trim_edit = dict(good_edit)
    over_trim_edit["world_gameplay"] = {"text_fa": "کنترل خوب است."}
    is_safe, reason = _polish_pass_is_safe(original_texts, over_trim_edit, set())
    assert not is_safe and "کوتاه" in reason

    # 6) apply_editorial_polish_pass must return sections untouched when client is None.
    sections = {f"{key}_fa": text for key, text in original_texts.items()}
    sections["method"] = "openai_single_pass_grounded_editorial_v34"
    unchanged = apply_editorial_polish_pass(None, sections, set())
    assert unchanged == sections

    # 7) A generic, ungrounded adjective must be catchable regardless of exact
    # wording -- synonym-swapping ("high-detail" instead of "rich") must not
    # dodge the check.
    assert _editorial_claim_quality_reason("world_gameplay", "این جهان بسیار غنی است.")
    assert _editorial_claim_quality_reason("world_gameplay", "این جهان بسیار پرجزئیات است.")
    # اما همان صفت، وقتی جزئیاتِ مشخص همراهش باشد، دیگر مردود نیست.
    assert _editorial_claim_quality_reason(
        "world_gameplay",
        "این جهان پرجزئیات با فرصت‌های فراوان برای کاوش و تعامل همراه است.",
    ) is None

    # 8) Improving a vague adjective by referencing a detail ALREADY established
    # elsewhere in the article (here: "کاوش" in world_gameplay) must be accepted --
    # this is the whole point of upgrading from a trim-only pass to a polish pass.
    generic_original = dict(original_texts)
    generic_original["friction"] = "این بخش از بازی نسبتاً پرجزئیات است."
    improved_edit = {key: {"text_fa": text} for key, text in generic_original.items()}
    improved_edit["friction"] = {
        "text_fa": "این بخش از بازی با جزئیاتی از کاوش همراه است."
    }
    is_safe, reason = _polish_pass_is_safe(generic_original, improved_edit, set())
    assert is_safe, reason

    # 9) But inventing a wholly new concrete detail that appears NOWHERE in the
    # original article (not this section, not any other) must be rejected --
    # this is the fabrication guardrail that makes "improve" safe to allow.
    fabricated_edit = {key: {"text_fa": text} for key, text in original_texts.items()}
    fabricated_edit["friction"] = {
        "text_fa": "این بخش از بازی با طراحی صداگذاری شخصیت اصلی برجسته می‌شود."
    }
    is_safe, reason = _polish_pass_is_safe(original_texts, fabricated_edit, set())
    assert not is_safe and "تازه" in reason



_LONGFORM_SECTION_SPECS_V35 = {
    "opening": {"min_words": 75, "max_words": 180},
    "world_gameplay": {"min_words": 190, "max_words": 380},
    "friction": {"min_words": 180, "max_words": 370},
    "audience": {"min_words": 65, "max_words": 155},
    "conclusion": {"min_words": 75, "max_words": 170},
}


def _longform_editorial_plan_v35(facts: dict) -> dict[str, list[dict]] | None:
    all_facts = [
        item for item in facts.get("facts", [])
        if isinstance(item, dict) and item.get("id")
    ]
    used_ids: set[str] = set()

    def take(*, topics: set[str] | None, sentiments: set[str], count: int) -> list[dict]:
        picked = _pick_editorial_facts(
            all_facts,
            used_ids=used_ids,
            topics=topics,
            sentiments=sentiments,
            count=count,
        )
        used_ids.update(item["id"] for item in picked if item.get("id"))
        return picked

    positive_topics = {"gameplay", "world_design", "visuals", "audio", "story", "value", "general"}
    negative_topics = {"gameplay", "world_design", "visuals", "audio", "story", "technical", "value", "general"}

    opening_pos = take(topics=positive_topics, sentiments={"positive"}, count=1)
    opening_neg = take(topics=negative_topics, sentiments={"negative", "caution"}, count=1)
    strengths = take(topics=positive_topics, sentiments={"positive"}, count=5)
    weaknesses = take(topics=negative_topics, sentiments={"negative", "caution"}, count=5)

    if len(opening_pos) < 1 or len(opening_neg) < 1 or len(strengths) < 4 or len(weaknesses) < 4:
        return None

    return {
        "opening": opening_pos + opening_neg,
        "world_gameplay": strengths,
        "friction": weaknesses,
        "audience": [strengths[0], weaknesses[0]],
        "conclusion": [opening_pos[0], opening_neg[0]],
    }


def _longform_prompt_v35(facts: dict, plan: dict[str, list[dict]], retry_reason: str = "") -> str:
    labels = {
        "opening": "معرفی و تز اصلی",
        "world_gameplay": "نقاط قوت: گیم‌پلی، جهان و ارائه",
        "friction": "نقاط ضعف: روایت، سیستم‌ها، فنی و ارزش خرید",
        "audience": "مخاطب مناسب و تصمیم خرید",
        "conclusion": "جمع‌بندی نهایی",
    }
    jobs = {
        "opening": "بازی را کوتاه و بی‌طرف معرفی کن و بعد کشش اصلی را در برابر هزینه‌ی اصلی تجربه قرار بده.",
        "world_gameplay": "کارت‌های مثبت را به چند پاراگراف پیوسته تبدیل کن. هر مشاهده را با اثرش بر کنترل، اکتشاف، ریتم یا لذت بازی توضیح بده.",
        "friction": "کارت‌های منفی و احتیاطی را تحلیل کن. روشن کن هر ایراد دقیقاً چگونه ریتم، فهم سیستم‌ها، روایت، عملکرد یا ارزش وقت و پول را تضعیف می‌کند.",
        "audience": "بر اساس معامله‌ی واقعی میان نقاط قوت و ضعف، روشن بگو چه بازیکنی احتمالاً از خرید راضی می‌شود و چه کسی بهتر است بازی را رد کند.",
        "conclusion": "حکم نهاییِ متعادل و مشخص بده؛ نه تکرار مقدمه و نه تبلیغ. ارزش تجربه و بهای آن را در چند جمله جمع کن.",
    }

    brief = {}
    for key, items in plan.items():
        brief[key] = {
            "label": labels[key],
            "job_fa": jobs[key],
            "evidence_cards": [
                {
                    "topic": _PUBLIC_TOPIC_LABELS.get(item.get("topic"), "تصویر کلی"),
                    "sentiment": item.get("sentiment"),
                    "approved_claim_fa": item.get("text_fa"),
                    "context_en": clean_text(str(item.get("evidence_en") or ""))[:420],
                }
                for item in items
            ],
        }

    retry_note = ""
    if retry_reason:
        retry_note = (
            "\nThe previous draft missed the publication target for this reason: "
            + retry_reason
            + "\nRewrite the complete article while preserving factual discipline."
        )

    return f"""
You are the senior Persian games editor for Poormaz. Write one complete Persian review
of "{facts['game']}" based on the locked evidence map below. The article must read like
an original critical review, not a digest of websites and not a list of pros and cons.

Neutral game introduction that may be used ONLY in opening:
{facts.get('game_intro_fa') or 'No separate introduction is available; introduce only the game name and platform.'}

Locked editorial map:
{json.dumps(brief, ensure_ascii=False, indent=2)}
{retry_note}

Return exactly one JSON object with these five objects:
{{
  "opening": {{"text_fa": "..."}},
  "world_gameplay": {{"text_fa": "..."}},
  "friction": {{"text_fa": "..."}},
  "audience": {{"text_fa": "..."}},
  "conclusion": {{"text_fa": "..."}}
}}

Editorial rules:
- Write fluent contemporary Persian in connected paragraphs. No bullets, Markdown, source names or quotations.
- Use only the approved claims. The English context is for nuance, never for literal translation or extra facts.
- Cover the available gameplay, world, graphics/presentation, story, technical and value evidence when assigned.
- Every paragraph needs concrete observation plus consequence. Do not pad with synonyms or generic praise.
- Do not mention scores, Metacritic, Poormaz, reviewers, sources, evidence or websites inside these sections.
- Do not invent plot details, mechanics, technical causes, prices, patch promises or comparisons.
- Avoid marketing language such as «جهان غنی»، «تجربه‌ای شگفت‌انگیز»، «شاهکار»، «بی‌نقص» and «بهترین در تاریخ».
- Do not tell readers to wait for updates unless an approved card explicitly mentions a specific update.
- The audience section must provide a practical buy-or-skip judgment.

Length targets:
- opening: 100–130 Persian words
- world_gameplay: 250–300 Persian words
- friction: 240–290 Persian words
- audience: 90–115 Persian words
- conclusion: 100–125 Persian words
- Total article: 800–1000 Persian words. Stay inside this range without filler.

Before returning JSON, silently verify that the total is between 800 and 1000 Persian words,
that no assigned fact is contradicted, and that each body section contains analysis rather
than a sequence of paraphrased evidence cards.
""".strip()


def _validate_longform_v35(raw, plan: dict[str, list[dict]], allowed_site_names: set[str]) -> tuple[dict | None, str | None]:
    if not isinstance(raw, dict):
        return None, "فرمت JSON مقاله معتبر نیست"

    normalized = {}
    for key, spec in _LONGFORM_SECTION_SPECS_V35.items():
        text_fa = _editorial_text_value(raw.get(key))
        if not _public_section_is_safe(
            text_fa,
            min_words=spec["min_words"],
            max_words=spec["max_words"],
            allowed_site_names=allowed_site_names,
        ):
            words = len(text_fa.split()) if text_fa else 0
            return None, f"بخش {key} از نظر طول یا ایمنی معتبر نیست ({words} واژه)"
        reason = _editorial_claim_quality_reason(key, text_fa)
        if reason:
            return None, f"بخش {key}: {reason}"
        normalized[key] = {
            "text_fa": text_fa,
            "supports": [item["id"] for item in plan.get(key, []) if item.get("id")],
        }

    total_words = sum(len(item["text_fa"].split()) for item in normalized.values())
    normalized["_total_words"] = total_words
    return normalized, None


def _write_public_article_sections_longform_v35(client: OpenAI, facts: dict) -> dict:
    allowed_site_names = {
        clean_text(str(source.get("site_name") or ""))
        for source in facts.get("sources", [])
        if clean_text(str(source.get("site_name") or ""))
    }
    if client is None or len(_fact_map(facts)) < 10:
        return _build_public_article_fallback(facts)

    plan = _longform_editorial_plan_v35(facts)
    if plan is None:
        print("Long-form editorial plan lacks enough diverse verified facts; using safe fallback.")
        return _build_public_article_fallback(facts)

    candidates = []
    retry_reason = ""
    for attempt in range(2):
        prompt = _longform_prompt_v35(facts, plan, retry_reason=retry_reason)
        try:
            raw = ask_openai_json(client, prompt, max_tokens=7000)
        except Exception as exc:
            retry_reason = f"OpenAI request failed: {repr(exc)}"
            print(f"Long-form article attempt {attempt + 1} failed: {repr(exc)}")
            continue

        normalized, reason = _validate_longform_v35(raw, plan, allowed_site_names)
        if normalized is None:
            retry_reason = reason or "اعتبارسنجی نامشخص"
            print(f"Long-form article attempt {attempt + 1} rejected: {retry_reason}")
            continue

        total_words = int(normalized.pop("_total_words"))
        in_target = 800 <= total_words <= 1000
        candidates.append((in_target, abs(total_words - 900), total_words, normalized, attempt))
        if in_target:
            break
        retry_reason = (
            f"طول کل {total_words} واژه بود؛ مقاله باید بین 800 و 1000 واژه باشد."
        )
        print(f"Long-form article length outside target ({total_words}); retrying once.")

    if not candidates:
        print("No safe long-form candidate survived; using the older section-preserving writer.")
        return _write_public_article_sections(client, facts)

    candidates.sort(key=lambda item: (not item[0], item[1], item[4]))
    in_target, _, total_words, best, attempt = candidates[0]
    sections = {
        "opening_fa": best["opening"]["text_fa"],
        "world_gameplay_fa": best["world_gameplay"]["text_fa"],
        "friction_fa": best["friction"]["text_fa"],
        "audience_fa": best["audience"]["text_fa"],
        "conclusion_fa": best["conclusion"]["text_fa"],
        "section_supports": {key: value["supports"] for key, value in best.items()},
        "editorial_plan": {key: [item["id"] for item in items] for key, items in plan.items()},
        "section_methods": {key: ("openai_full_pass" if attempt == 0 else "openai_full_retry") for key in best},
        "method": "openai_longform_grounded_editorial_v35",
        "word_count": total_words,
        "length_target_met": in_target,
        "length_target": "800-1000",
    }

    polished = apply_editorial_polish_pass(client, sections, allowed_site_names)
    polished_words = int(polished.get("word_count") or total_words)
    if in_target and not (800 <= polished_words <= 1000):
        print("Editorial polish moved the article outside 800–1000 words; keeping the approved pre-edit version.")
        return sections
    polished["length_target_met"] = 800 <= polished_words <= 1000
    polished["length_target"] = "800-1000"
    return polished


def _score_number_text(value) -> str:
    numeric = float(value)
    return str(int(numeric)) if numeric.is_integer() else f"{numeric:.1f}"


def _build_poormaz_scorecard_html(assessment: dict) -> str:
    """
    کارت گرافیکی نهایی نقد را بدون JavaScript می‌سازد تا مستقیماً داخل HTML
    وردپرس قرار بگیرد. مقدار دایره و نوارها از قبل در خود HTML رندر می‌شود؛
    بنابراین حذف شدن script توسط وردپرس، کارت را از کار نمی‌اندازد.
    """
    scorecard = assessment.get("scorecard", []) or []
    overall = assessment.get("overall_score_10")
    if overall is None or not scorecard:
        return ""

    overall = max(0.0, min(10.0, float(overall)))

    if overall >= 9.0:
        verdict_fa, tier_color = "شاهکار", "#e7b24e"
    elif overall >= 8.0:
        verdict_fa, tier_color = "ارزش تجربه", "#f0637e"
    elif overall >= 7.0:
        verdict_fa, tier_color = "انتخاب خوب", "#d22b4b"
    elif overall >= 5.5:
        verdict_fa, tier_color = "متوسط", "#c9974a"
    else:
        verdict_fa, tier_color = "پیشنهاد نمی‌شود", "#948da3"

    circumference = 339.292
    ring_offset = circumference * (1.0 - overall / 10.0)
    overall_text = escape(_score_number_text(overall))

    rows_html: list[str] = []
    for row in scorecard:
        label = escape(clean_text(str(row.get("label_fa") or "بخش")))
        score = max(0.0, min(10.0, float(row.get("score_10") or 0)))
        score_text = escape(_score_number_text(score))
        width = score * 10.0
        rows_html.append(
            '<div class="pmz-aspect-row">'
            '<div class="pmz-aspect-top">'
            f'<span class="pmz-aspect-label">{label}</span>'
            f'<span class="pmz-aspect-value">{score_text}<small>/10</small></span>'
            '</div>'
            '<div class="pmz-bar-track">'
            f'<div class="pmz-bar-fill" style="width:{width:.1f}%"></div>'
            '</div>'
            '</div>'
        )

    # تمام کارت عمداً در یک خط برگردانده می‌شود تا مبدل Markdown داخلی آن را
    # به‌عنوان یک بلوک HTML خام و امن مستقیماً وارد محتوای وردپرس کند.
    css = (
        '<style>'
        '.pmz-score-widget{--ink900:#131019;--ink800:#1d1824;--ink700:#272030;'
        '--crimson:#d22b4b;--crimsonBright:#f0637e;--mist:#948da3;--paper:#f6f2ec;'
        'box-sizing:border-box;max-width:820px;margin:38px auto 8px;padding:40px 44px;'
        'background:linear-gradient(160deg,var(--ink800),var(--ink900) 72%);'
        'border:1px solid var(--ink700);border-radius:18px;color:var(--paper);'
        'display:flex;flex-direction:row;align-items:center;gap:42px;position:relative;'
        'overflow:hidden;direction:rtl;font-family:Vazirmatn,Tahoma,Arial,sans-serif;'
        'box-shadow:0 18px 55px rgba(0,0,0,.30)}'
        '.pmz-score-widget,.pmz-score-widget *{box-sizing:border-box}'
        '.pmz-score-widget:before{content:"";position:absolute;top:-65%;right:-18%;width:58%;height:230%;'
        'background:radial-gradient(circle,rgba(210,43,75,.16),transparent 70%);pointer-events:none}'
        '.pmz-verdict{flex:0 0 190px;width:190px;display:flex;flex-direction:column;align-items:center;'
        'text-align:center;position:relative;z-index:1}'
        '.pmz-eyebrow{font-size:12px;font-weight:800;letter-spacing:.04em;color:var(--mist);margin-bottom:14px}'
        '.pmz-ring-wrap{position:relative;width:148px;height:148px}'
        '.pmz-ring-wrap svg{width:100%;height:100%;transform:rotate(-90deg)}'
        '.pmz-ring-track{fill:none;stroke:var(--ink700);stroke-width:7}'
        '.pmz-ring-fill{fill:none;stroke:var(--pmz-tier-color);stroke-width:7;stroke-linecap:round}'
        '.pmz-ring-number{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;flex-direction:column}'
        '.pmz-big-score{font-family:Arial Black,Impact,Tahoma,sans-serif;font-weight:900;font-size:45px;line-height:1;'
        'font-variant-numeric:tabular-nums;color:var(--paper);direction:ltr}'
        '.pmz-big-score small{font-size:14px;font-weight:700;color:var(--mist);margin-left:2px}'
        '.pmz-verdict-word{margin-top:15px;font-weight:900;font-size:18px;color:var(--pmz-tier-color)}'
        '.pmz-divider{flex:0 0 1px;align-self:stretch;background:linear-gradient(var(--ink700),rgba(255,255,255,.02),var(--ink700));z-index:1}'
        '.pmz-aspects{flex:1 1 auto;display:flex;flex-direction:column;gap:15px;position:relative;z-index:1;min-width:0}'
        '.pmz-aspect-top{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;gap:14px}'
        '.pmz-aspect-label{font-size:13px;font-weight:800;color:#c8c3d0;text-align:right}'
        '.pmz-aspect-value{font-family:Arial Black,Tahoma,sans-serif;font-size:16px;font-weight:900;'
        'font-variant-numeric:tabular-nums;color:var(--paper);direction:ltr;white-space:nowrap}'
        '.pmz-aspect-value small{color:var(--mist);font-size:11px;font-weight:700;margin-left:2px}'
        '.pmz-bar-track{position:relative;height:10px;border-radius:3px;background-color:var(--ink700);'
        'background-image:repeating-linear-gradient(90deg,transparent 0,transparent calc(10% - 2px),'
        'var(--ink900) calc(10% - 2px),var(--ink900) 10%);overflow:hidden;direction:ltr}'
        '.pmz-bar-fill{position:absolute;inset:0 auto 0 0;border-radius:3px;'
        'background:linear-gradient(90deg,var(--crimson),var(--crimsonBright))}'
        '@media(max-width:640px){.pmz-score-widget{flex-direction:column;padding:30px 24px;gap:26px}'
        '.pmz-verdict{width:100%;flex-basis:auto}.pmz-divider{width:100%;height:1px;align-self:auto;flex-basis:1px}'
        '.pmz-aspects{width:100%}}'
        '</style>'
    )

    return (
        f'<div data-poormaz-scorecard="1" class="pmz-score-widget" dir="rtl" '
        f'style="--pmz-tier-color:{tier_color}">'
        + css
        + '<div class="pmz-verdict">'
        '<div class="pmz-eyebrow">نظر نهایی Poormaz</div>'
        '<div class="pmz-ring-wrap">'
        '<svg viewBox="0 0 120 120" role="img" aria-label="امتیاز نهایی">'
        '<circle class="pmz-ring-track" cx="60" cy="60" r="54"></circle>'
        f'<circle class="pmz-ring-fill" cx="60" cy="60" r="54" stroke-dasharray="{circumference:.3f}" '
        f'stroke-dashoffset="{ring_offset:.3f}"></circle>'
        '</svg>'
        '<div class="pmz-ring-number">'
        f'<div class="pmz-big-score">{overall_text}<small>/10</small></div>'
        '</div></div>'
        f'<div class="pmz-verdict-word">{escape(verdict_fa)}</div>'
        '</div>'
        '<div class="pmz-divider"></div>'
        '<div class="pmz-aspects">'
        + ''.join(rows_html)
        + '</div></div>'
    )


def build_article_preview(client: OpenAI, dossier: dict) -> dict:
    """نقد ۸۰۰ تا ۱۰۰۰ کلمه‌ای، کارت امتیاز HTML و منابع را می‌سازد."""
    facts = _public_article_fact_pack(dossier)
    sections = _write_public_article_sections_longform_v35(client, facts)

    game = facts["game"]
    overall_score = facts["poormaz_score"]
    metascore = facts["metascore"]
    critic_count = facts["critic_count"]
    platform = facts["platform"]
    assessment = dossier.get("poormaz_assessment", {}) or {}

    title_fa = f"نقد و بررسی {game} | جمع‌بندی Poormaz"
    excerpt_fa = f"نقد کامل فارسی {game} بر پایه‌ی جمع‌بندی نقدهای حرفه‌ای."

    glance_lines = []
    if overall_score is not None:
        glance_lines.append(f"- **امتیاز Poormaz:** {_format_score_10(overall_score)}")
    if metascore is not None:
        count_part = f" بر پایه‌ی {critic_count} نقد" if critic_count is not None else ""
        glance_lines.append(f"- **متاکریتیک:** {metascore}/100{count_part}")
    if platform:
        glance_lines.append(f"- **پلتفرم بررسی:** {platform}")

    source_lines = []
    source_links = []
    seen_sources = set()
    for source in facts["sources"]:
        site_name = clean_text(str(source.get("site_name") or "منبع نامشخص"))
        url = str(source.get("url") or "").strip()
        key = (site_name, url)
        if not url or key in seen_sources:
            continue
        seen_sources.add(key)
        score_label = _source_score_label(source)
        source_lines.append(f"- [{site_name}]({url}) | نمره: {score_label}")
        source_links.append({
            "site_name": site_name,
            "title": clean_text(str(source.get("title") or "نقد بازی")),
            "url": url,
            "score": score_label,
        })

    scorecard_html = _build_poormaz_scorecard_html(assessment)
    markdown = [f"# {title_fa}", "", f"> {excerpt_fa}"]

    if glance_lines:
        markdown.extend(["", "## در یک نگاه", *glance_lines])

    markdown.extend(["", f"## {game} در عمل", sections["opening_fa"]])
    markdown.extend(["", "## جهان، گیم‌پلی و ارائه", sections["world_gameplay_fa"]])
    markdown.extend(["", "## ضعف‌ها و اصطکاک‌هایی که باقی می‌مانند", sections["friction_fa"]])
    markdown.extend(["", "## مناسب چه کسی است؟", sections["audience_fa"]])
    markdown.extend(["", "## جمع‌بندی Poormaz", sections["conclusion_fa"]])
    markdown.extend(["", "## منابع بررسی‌شده", *source_lines])
    if scorecard_html:
        markdown.extend(["", scorecard_html])

    return {
        "status": "preview_longform_editorial_v36",
        "wordpress_post_created": False,
        "title_fa": title_fa,
        "excerpt_fa": excerpt_fa,
        "markdown": "\n".join(markdown).strip() + "\n",
        "scorecard_html": scorecard_html,
        "source_links": source_links,
        "review_note_fa": "این متن فقط پیش‌نمایش است و هنوز در وردپرس ساخته یا منتشر نشده است.",
        "writing_mode": sections.get("method", "deterministic_editorial_fallback_v27"),
        "word_count": sections.get("word_count"),
        "length_target": sections.get("length_target", "800-1000"),
        "length_target_met": sections.get("length_target_met", False),
        "section_supports": sections.get("section_supports", {}),
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

        if stripped.startswith('<div data-poormaz-scorecard="1"') and stripped.endswith('</div>'):
            flush_all()
            html_lines.append(stripped)
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
    sample_card = '<div data-poormaz-scorecard="1" dir="rtl"><strong>Score</strong></div>'
    sample = "# Title\n\n- **Bold** [Source](https://example.com/)\n\n" + sample_card
    html = _markdown_to_wp_html(sample)
    assert "<h2>Title</h2>" in html
    assert "<strong>Bold</strong>" in html
    assert 'href="https://example.com/"' in html
    assert sample_card in html


def run_poormaz_v36_regression_checks() -> None:
    dossier = {
        "metacritic": {"metascore_100": 88},
        "review_sources": [
            {
                "review_score_10": 8.0,
                "runtime_quality_audit": {"status": "acceptable"},
                "positives": [], "negatives": [], "technical_notes": [],
            },
            {
                "review_score_10": 9.5,
                "runtime_quality_audit": {"status": "needs_review"},
                "positives": [], "negatives": [], "technical_notes": [],
            },
        ],
    }
    score = calculate_overall_score(dossier)
    assert score["overall_score_10"] == 8.5
    assert score["selected_review_scores"] == [8.0]

    rows = [
        {
            "key": key,
            "label_fa": SCORECARD_CATEGORY_LABELS[key],
            "score_10": 8.5,
            "_raw_score_10": 8.5,
            "_direction_value": 0.0,
            "evidence_count": 0,
        }
        for key in SCORECARD_CATEGORY_ORDER
    ]
    balanced = _balance_scorecard_to_overall(rows, 8.5)
    assert len(balanced) == 6
    assert sum(row["score_10"] for row in balanced) / 6 == 8.5
    assert all((row["score_10"] * 2).is_integer() for row in balanced)

    sample_assessment = {
        "overall_score_10": 8.5,
        "scorecard": [
            {"label_fa": SCORECARD_CATEGORY_LABELS[key], "score_10": 8.5}
            for key in SCORECARD_CATEGORY_ORDER
        ],
    }
    card = _build_poormaz_scorecard_html(sample_assessment)
    assert 'data-poormaz-scorecard="1"' in card
    assert "نظر نهایی Poormaz" in card
    assert "pmz-ring-fill" in card
    assert "stroke-dashoffset" in card
    assert card.count("pmz-aspect-row") >= 6
    assert "<script" not in card.casefold(), "کارت وردپرس نباید به JavaScript وابسته باشد"
    rendered_card = _markdown_to_wp_html("## منابع بررسی‌شده\n\n- Source\n\n" + card)
    assert rendered_card.rstrip().endswith(card), "کارت باید آخر HTML مقاله باقی بماند"
    assert _longform_prompt_v35(
        {
            "game": "Sample Game",
            "game_intro_fa": "یک معرفی کوتاه.",
        },
        {
            key: [{"topic": "gameplay", "sentiment": "positive", "text_fa": "کنترل روان است.", "evidence_en": "Controls are responsive."}]
            for key in ("opening", "world_gameplay", "friction", "audience", "conclusion")
        },
    ).find("800–1000") != -1

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
        protocol_upgrade = (
            cached_analysis is not None
            and analysis_requires_protocol_upgrade(cached_analysis)
            and not source_is_manual_review
        )
        refresh_this_source = (
            cached_analysis is not None
            and not source_is_manual_review
            and (
                protocol_upgrade
                or (
                    refresh_incomplete_evidence
                    and cached_audit.get("status") == "needs_review"
                )
            )
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
        elif protocol_upgrade:
            print(
                f"Evidence cache: PROTOCOL UPGRADE | "
                f"{cached_analysis.get('site_name', requested_url)}"
            )
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
            if protocol_upgrade:
                merged_analysis = merge_protocol_upgrade_analysis(
                    cached_analysis,
                    candidate_analysis,
                )
            else:
                merged_analysis = merge_cached_and_candidate_analysis(
                    cached_analysis,
                    candidate_analysis,
                )

            old_audit = evidence_quality_audit(cached_analysis)
            merged_audit = evidence_quality_audit(merged_analysis)
            improved = merged_audit["quality_rank"] > old_audit["quality_rank"]

            # ارتقای پروتکل باید cache قدیمیِ ناسالم را با نسخه‌ی شناسه‌دارِ
            # قابل‌قبول جایگزین کند، حتی وقتی نسخه‌ی تازه نکته‌های کمتری اما دقیق‌تر
            # دارد. مقایسه‌ی تعداد خام نکته‌ها اینجا معیار معناداری نیست.
            if protocol_upgrade and merged_audit["status"] == "acceptable":
                improved = True

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
            elif protocol_upgrade:
                cache_info["status"] = "protocol_upgraded"
                print(f"Evidence cache: PROTOCOL UPGRADED | {analysis['site_name']}")
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
    run_wccftech_score_extraction_regression_checks()
    print("Wccftech score extraction regression checks: passed")
    run_evidence_cache_regression_checks()
    print("Evidence cache regression checks: passed")
    run_wordpress_draft_regression_checks()
    print("WordPress draft regression checks: passed")
    run_metadata_regression_checks()
    print("Metadata regression checks (game status, reputable sites): passed")
    run_evidence_protocol_regression_checks()
    print("Evidence protocol regression checks: passed")
    run_public_article_regression_checks()
    print("Public article regression checks: passed")
    run_longform_retry_regression_checks()
    print("Long-form retry regression checks: passed")
    run_single_pass_editorial_regression_checks()
    print("Single-pass editorial regression checks: passed")
    run_length_is_a_goal_not_a_wall_regression_checks()
    print("Length-is-a-goal-not-a-wall regression checks: passed")
    run_editorial_polish_pass_regression_checks()
    print("Editorial polish pass regression checks: passed")
    run_poormaz_v36_regression_checks()
    print("Poormaz v36 score, long-form, and graphical HTML regression checks: passed")

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
