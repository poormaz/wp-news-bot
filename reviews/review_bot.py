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
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0") or "0")

REVIEW_MIN_SOURCES = int(os.getenv("REVIEW_MIN_SOURCES", "3") or "3")
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
        "score_text": page_text,
        "text_chars": len(page_text),
        "html": html,
        "meta_items": meta_parser.meta_items,
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

Return JSON with exactly:
- positives: array of objects with point_fa and evidence_en
- negatives: array of objects with point_fa and evidence_en
- technical_notes: array of objects with point_fa and evidence_en
- platform_mentioned: string or null

Page text:
{page["text"]}
""".strip()

    raw = ask_openai_json(client, prompt)

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
    "story": "داستان و روایت",
    "technical": "فنی و عملکرد",
    "visuals": "تصویرسازی و گرافیک",
    "value": "ارزش خرید",
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
    positive = []
    negative = []
    mixed = []

    for category in scorecard:
        label = clean_text(str(category.get("label_fa") or ""))
        trend = clean_text(str(category.get("trend_fa") or ""))
        confidence = clean_text(str(category.get("confidence_fa") or ""))

        if not label:
            continue

        item = f"{label} ({confidence} از نظر پوشش شواهد)"

        if trend == "مثبت":
            positive.append(item)
        elif trend == "منفی":
            negative.append(item)
        else:
            mixed.append(item)

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


def _build_template_editorial_article_preview(dossier: dict) -> dict:
    """
    پیش‌نمایش تحریریه‌ایِ امن: متن با الگوهای ثابت ساخته می‌شود و هر نکته
    مستقیماً به یک شاهد یا کارت قابل‌ردیابی وصل است.
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

    evidence_by_id = {
        item.get("id"): item
        for item in fact_pack.get("evidence", [])
        if item.get("id")
    }

    title_fa = f"نقد و بررسی {game} | جمع‌بندی امتیازها و نقدها"
    excerpt_bits = [f"جمع‌بندی Poormaz از نقدهای منتخب {game}"]
    if metascore is not None:
        excerpt_bits.append(f"متاکریتیک {metascore} از ۱۰۰")
    if overall_score is not None:
        excerpt_bits.append(f"امتیاز Poormaz {_format_score_10(overall_score)}")
    excerpt_fa = " | ".join(excerpt_bits) + "."

    markdown = [
        f"# {title_fa}",
        "",
        f"> {excerpt_fa}",
        "",
        "## نتیجه در یک نگاه",
    ]

    if overall_score is not None:
        markdown.append(f"- **امتیاز Poormaz:** {_format_score_10(overall_score)}")
    if metascore is not None:
        count_part = f" بر پایه‌ی {critic_count} نقد منتقدان" if critic_count is not None else ""
        markdown.append(f"- **متاکریتیک:** {metascore}/100{count_part}")
    if platform:
        markdown.append(f"- **پلتفرم پرونده:** {platform}")

    overview = (
        f"این پرونده از {len(review_sources)} نقد انتخاب‌شده و داده‌ی متاکریتیک ساخته شده است. "
        f"امتیاز تجمیعی Poormaz برای {game} {_format_score_10(overall_score)} است."
    )
    if metascore is not None:
        overview += f" متاکریتیک ثبت‌شده نیز {metascore}/100 است."

    markdown.extend([
        "",
        "## نگاه کلی به نقدها",
        overview + _render_inline_citations(["META"], fact_pack),
        "",
        "## کارت امتیاز Poormaz",
        "",
        "| بخش | امتیاز | جهت‌گیری | پوشش شواهد |",
        "|---|---:|---|---|",
    ])

    for card in scorecards:
        markdown.append(
            f"| {card.get('label_fa', 'نامشخص')} | "
            f"{_format_score_10(card.get('score_10'))} | "
            f"{card.get('trend_fa', 'نامشخص')} | "
            f"{card.get('confidence_fa', 'برداشت اولیه')} |"
        )

    markdown.extend(["", "## جزئیات ارزیابی"])
    categorized_refs = set()

    for card in scorecards:
        label = clean_text(str(card.get("label_fa") or "این بخش"))
        trend = clean_text(str(card.get("trend_fa") or "ترکیبی"))
        confidence = clean_text(str(card.get("confidence_fa") or "برداشت اولیه"))
        positives, negatives, neutral = _editorial_points_for_card(card, evidence_by_id)
        supports = list(card.get("supported_refs", []) or [])
        categorized_refs.update(supports)

        markdown.extend([
            "",
            f"### {label}",
            (
                f"در این پرونده، جهت‌گیری {label} «{trend}» و پوشش شواهد «{confidence}» است."
                + _render_inline_citations(supports, fact_pack)
            ),
        ])

        if positives:
            markdown.append("**نکات مثبت ثبت‌شده:**")
            for item in positives:
                markdown.append(
                    "- "
                    + clean_text(str(item.get("point_fa") or ""))
                    + _render_inline_citations([item["id"]], fact_pack)
                )

        if negatives:
            markdown.append("**نکات احتیاطی ثبت‌شده:**")
            for item in negatives:
                markdown.append(
                    "- "
                    + clean_text(str(item.get("point_fa") or ""))
                    + _render_inline_citations([item["id"]], fact_pack)
                )

        if neutral:
            markdown.append("**نکات فنی ثبت‌شده:**")
            for item in neutral:
                markdown.append(
                    "- "
                    + clean_text(str(item.get("point_fa") or ""))
                    + _render_inline_citations([item["id"]], fact_pack)
                )

        if confidence in {"برداشت اولیه", "محدود"}:
            markdown.append(
                f"> این ارزیابی فعلاً «{confidence}» است و برای نتیجه‌گیری محکم‌تر به شواهد بیشتری نیاز دارد."
            )

    uncategorized = [
        item for ref_id, item in evidence_by_id.items()
        if ref_id not in categorized_refs
    ]
    if uncategorized:
        markdown.extend(["", "## نکات تکمیلی ثبت‌شده"])
        for item in uncategorized[:ARTICLE_MAX_POINTS_PER_SECTION]:
            point = clean_text(str(item.get("point_fa") or ""))
            if point:
                markdown.append(
                    "- " + point + _render_inline_citations([item["id"]], fact_pack)
                )

    conclusion = (
        f"امتیاز تجمیعی Poormaz برای {game} {_format_score_10(overall_score)} است. "
        + clean_text(str(meta.get("formula_fa") or ""))
    )
    limited_labels = [
        clean_text(str(card.get("label_fa") or ""))
        for card in scorecards
        if clean_text(str(card.get("confidence_fa") or "")) in {"برداشت اولیه", "محدود"}
    ]
    if limited_labels:
        conclusion += (
            " در این مرحله، بخش‌های "
            + "، ".join(label for label in limited_labels if label)
            + " هنوز بر پایه‌ی شواهد محدودتر ارزیابی شده‌اند."
        )

    markdown.extend([
        "",
        "## جمع‌بندی Poormaz",
        conclusion + _render_inline_citations(["META"], fact_pack),
        "",
        "## منابع بررسی‌شده",
    ])

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
        "یادداشت تحریریه: این پیش‌نمایش به‌صورت خودکار و فقط از شواهد ثبت‌شده در پرونده ساخته شده است. هنوز پستی در وردپرس ایجاد یا منتشر نشده است.",
    ])

    return {
        "status": "preview_only_template_grounded_v7",
        "wordpress_post_created": False,
        "title_fa": title_fa,
        "excerpt_fa": excerpt_fa,
        "markdown": "\n".join(markdown).strip() + "\n",
        "source_links": fact_pack.get("sources", []),
        "review_note_fa": "این متن صرفاً پیش‌نمایش است و هیچ پستی در وردپرس ایجاد یا منتشر نشده است.",
        "writing_mode": "template_grounded_with_per_evidence_sentiment",
    }


def build_article_preview(client: OpenAI, dossier: dict) -> dict:
    """
    نوشتار مقاله در این مرحله عمداً قالب‌محور است. خروجی V5 نشان داد که
    وجود شناسه‌ی منبع به‌تنهایی جلوی پیش‌بینی و ادعای زمانیِ مدل را نمی‌گیرد.
    نسخه‌ی فعلی برای نمایش نکات فنی نیز جهت‌گیری هر شاهد را جدا نگه می‌دارد.
    """
    del client
    return _build_template_editorial_article_preview(dossier)


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


def save_dossier(game: str, dossier: dict) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    output_path = os.path.join(
        OUTPUT_DIR,
        f"{safe_filename(game)}-dossier.json",
    )

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
            print(f"Loaded: {page['site_name']} | {page['title'][:80]}")
        else:
            print(f"Skipped source: {url} | HTTP {page['status']}")

    if len(source_pages) < REVIEW_MIN_SOURCES:
        print(
            f"SKIP: only {len(source_pages)} usable review sources. "
            f"Need at least {REVIEW_MIN_SOURCES}."
        )
        return

    metacritic_data = extract_metacritic_data(
        metacritic_page,
        platform,
    )

    source_analyses = []

    for page in source_pages:
        detected = extract_review_score(page)

        print(
            f"Score scan: {page['site_name']} | "
            f"{detected['original_score'] or 'not found'} | "
            f"{detected['score_method']}"
        )

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

    output_path = save_dossier(game, dossier)

    print("\n--- REVIEW DOSSIER SUMMARY ---")
    print(f"Game: {game}")
    print(f"Metascore: {metacritic_data['metascore_100']}")
    print(f"Review count: {metacritic_data['critic_review_count']}")

    for source in source_analyses:
        print(
            f"- {source['site_name']}: "
            f"{source['original_score'] or 'No deterministic score found'} "
            f"[{source['score_method']}]"
        )

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
    print("No WordPress post was created.")

    print(f"\nSaved: {output_path}")
    print("\n--- REVIEW DOSSIER JSON ---")
    print(json.dumps(dossier, ensure_ascii=False, indent=2))
    print("\nNo WordPress post was created.")


def main():
    print("=== Poormaz Review Bot: Verified Dossier Builder ===")
    run_rule_based_regression_checks()
    print("Rule-based regression checks: passed")

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
