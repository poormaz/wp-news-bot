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
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.1") or "0.1")

REVIEW_MIN_SOURCES = int(os.getenv("REVIEW_MIN_SOURCES", "3") or "3")
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30") or "30")
SOURCE_TEXT_LIMIT = int(os.getenv("REVIEW_SOURCE_TEXT_LIMIT", "18000") or "18000")

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

    # 3. امتیازهای نوشته‌شده و برچسب‌دار در صفحه
    labelled_patterns = [
        r"(?is)\b(?:review(?:\s+score)?|final\s+score|rating|verdict)\b.{0,80}?(\d{1,3}(?:\.\d+)?)\s*(?:/|out\s+of)\s*(\d{1,3})",
        r"(?is)\b(?:review(?:\s+score)?|final\s+score|rating|verdict)\b.{0,45}?\b(\d{1,3}(?:\.\d+)?)\b",
    ]

    for pattern_index, pattern in enumerate(labelled_patterns):
        for match in re.finditer(pattern, visible_text):
            raw_value = match.group(1)
            raw_best = match.group(2) if match.lastindex and match.lastindex >= 2 else None

            evidence = visible_text[
                max(0, match.start() - 70): match.end() + 70
            ]

            add_candidate(
                raw_value,
                raw_best,
                method="visible_labelled_score",
                confidence=86 if pattern_index == 0 else 75,
                evidence=evidence,
            )

    # 4. امتیازهای 8/10 یا 80/100 نزدیک ابتدای نقد
    for match in re.finditer(
        r"(?<![\d/])(\d{1,3}(?:\.\d+)?)\s*/\s*(10|5|100)\b",
        visible_text,
    ):
        context = visible_text[
            max(0, match.start() - 120): match.end() + 120
        ]

        if match.start() >= 2800:
            continue

        context_lower = context.lower()

        if not any(
            word in context_lower
            for word in (
                "review",
                "score",
                "rating",
                "verdict",
                "wccftech",
                "pc gamer",
                "destructoid",
                "ign",
                "gamespot",
            )
        ):
            continue

        add_candidate(
            match.group(1),
            match.group(2),
            method="visible_nearby_fraction",
            confidence=70,
            evidence=context,
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
            1 if "jsonld" in item["score_method"] else 0,
        ),
        reverse=True,
    )

    return candidates[0]

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
- Do not invent details, including performance, bugs, hardware results, story specifics, or localization issues.
- Every point must be a concise Persian paraphrase of a clear point in the review.
- If there is no clear evidence for a field, return an empty array.
- Never quote the review verbatim.

Return JSON with exactly:
- positives_fa: array of 0 to 4 short Persian points
- negatives_fa: array of 0 to 4 short Persian points
- technical_notes_fa: array of 0 to 3 short Persian points, only if explicitly discussed
- verdict_fa: one concise Persian paragraph based only on the review
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
        "positives_fa": short_list(raw.get("positives_fa")),
        "negatives_fa": short_list(raw.get("negatives_fa")),
        "technical_notes_fa": short_list(
            raw.get("technical_notes_fa"),
            3,
        ),
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

    print(f"\nSaved: {output_path}")
    print("\n--- REVIEW DOSSIER JSON ---")
    print(json.dumps(dossier, ensure_ascii=False, indent=2))
    print("\nNo WordPress post was created.")


def main():
    print("=== Poormaz Review Bot: Verified Dossier Builder ===")

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
