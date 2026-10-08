"""Text, URL and normalization helpers shared by every pipeline stage."""

from __future__ import annotations

import hashlib
import html as html_lib
import math
import re
import unicodedata
from collections import Counter
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
ARABIC_INDIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_DIGIT_TABLE = str.maketrans(PERSIAN_DIGITS + ARABIC_INDIC_DIGITS, "0123456789" * 2)
_TO_PERSIAN_TABLE = str.maketrans("0123456789", PERSIAN_DIGITS)

ZWNJ = "‌"


def to_ascii_digits(value: str) -> str:
    return (value or "").translate(_DIGIT_TABLE)


def to_persian_digits(value: str) -> str:
    return (value or "").translate(_TO_PERSIAN_TABLE)


def collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def strip_tags(value: str) -> str:
    value = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", value or "")
    value = re.sub(r"(?s)<!--.*?-->", " ", value)
    return re.sub(r"(?s)<[^>]+>", " ", value)


def clean_text(value: str) -> str:
    """Plain text from a possibly HTML-bearing string."""
    return collapse_ws(html_lib.unescape(strip_tags(value or "")))


def truncate_words(value: str, max_chars: int) -> str:
    value = collapse_ws(value)
    if len(value) <= max_chars:
        return value
    cut = value[:max_chars].rsplit(" ", 1)[0]
    return (cut or value[:max_chars]).rstrip(" ,.;:-") + "…"


def sha256(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def short_hash(value: str, length: int = 16) -> str:
    return sha256(value)[:length]


# ---------------------------------------------------------------------------
# Latin folding / entity keys
# ---------------------------------------------------------------------------
_LATIN_FOLD_EXTRA = {"ø": "o", "æ": "ae", "œ": "oe", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "ı": "i", "ß": "ss"}


def fold_latin(value: str) -> str:
    """Strip accents from Latin letters only (Yōtei -> Yotei); Persian is untouched."""
    out = []
    for ch in value or "":
        low = ch.lower()
        if low in _LATIN_FOLD_EXTRA:
            rep = _LATIN_FOLD_EXTRA[low]
            out.append(rep.upper() if ch != low else rep)
        elif ord(ch) > 127 and "LATIN" in unicodedata.name(ch, ""):
            out.append("".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)))
        else:
            out.append(ch)
    return "".join(out)


_ROMAN = {
    "II": "2", "III": "3", "IV": "4", "VI": "6", "VII": "7", "VIII": "8", "IX": "9",
    "XI": "11", "XII": "12", "XIII": "13", "XIV": "14", "XV": "15", "XVI": "16",
}


def _roman_to_digits(value: str) -> str:
    # Only multi-letter numerals in upper case: "Final Fantasy VII" -> 7, but never the
    # pronoun "I", "Series X" or "GTA V" (those are handled by explicit aliases).
    return re.sub(r"\b(II|III|IV|VI|VII|VIII|IX|XI|XII|XIII|XIV|XV|XVI)\b", lambda m: _ROMAN[m.group(1)], value)


def entity_key(value: str) -> str:
    """Loose, stable key for game/company names.

    Case, accents, trademark symbols, apostrophes, punctuation, a leading "The" and
    multi-letter Roman numerals are ignored, so "The Witcher IV" == "Witcher 4" and
    "Tom Clancy's Ghost Recon®: Wildlands" == "tom clancys ghost recon wildlands".
    """
    value = html_lib.unescape(value or "")
    value = re.sub(r"[™®©]", "", value)
    value = unicodedata.normalize("NFKC", value)
    value = _roman_to_digits(value)
    value = fold_latin(value).casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"['‘’`´]", "", value)
    value = re.sub(r"[^0-9a-z؀-ۿ]+", " ", value)
    value = collapse_ws(value)
    if value.startswith("the "):
        value = value[4:]
    return value


# ---------------------------------------------------------------------------
# Headline tokens
# ---------------------------------------------------------------------------
STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "to", "of", "in", "on", "for", "with", "from", "by", "at",
    "is", "are", "was", "were", "be", "been", "being", "as", "this", "that", "these", "those", "it",
    "its", "into", "over", "about", "after", "before", "up", "out", "off", "than", "via", "your", "you",
    "we", "our", "they", "their", "his", "her", "has", "have", "had", "will", "would", "can", "could",
    "may", "might", "just", "now", "here", "heres", "theres", "more", "most", "very", "also", "all",
    "new", "latest", "finally", "officially", "reportedly", "according", "says", "said", "report",
    "reports", "gets", "get", "got", "set", "sets", "look", "looks", "first", "next", "one", "some",
}

CLICKBAIT = {"insane", "huge", "massive", "awesome", "amazing", "epic", "wild", "crazy", "stunning", "shocking"}


def light_stem(token: str) -> str:
    for suffix in ("ing", "ers", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def headline_tokens(value: str) -> list[str]:
    value = fold_latin(html_lib.unescape(value or "")).lower()
    value = re.sub(r"['’`]", "", value)
    words = re.findall(r"[a-z0-9]+", value)
    out = []
    for word in words:
        if word in STOPWORDS or word in CLICKBAIT:
            continue
        if len(word) < 2 and not word.isdigit():
            continue
        out.append(light_stem(word))
    return out


def jaccard(a, b) -> float:
    a, b = set(a), set(b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap_coefficient(a, b) -> float:
    a, b = set(a), set(b)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def char_ngrams(value: str, n: int = 3) -> Counter:
    value = " " + collapse_ws(fold_latin(value or "").lower()) + " "
    value = re.sub(r"[^a-z0-9 ]+", "", value)
    return Counter(value[i : i + n] for i in range(max(0, len(value) - n + 1)))


def tfidf_cosine(a: Counter, b: Counter, idf: dict[str, float] | None = None) -> float:
    if not a or not b:
        return 0.0
    idf = idf or {}

    def weight(counter: Counter) -> dict[str, float]:
        return {k: v * idf.get(k, 1.0) for k, v in counter.items()}

    wa, wb = weight(a), weight(b)
    dot = sum(wa[k] * wb.get(k, 0.0) for k in wa)
    na = math.sqrt(sum(v * v for v in wa.values()))
    nb = math.sqrt(sum(v * v for v in wb.values()))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def build_idf(docs: list[Counter]) -> dict[str, float]:
    df: Counter = Counter()
    for doc in docs:
        df.update(set(doc))
    total = max(1, len(docs))
    return {term: math.log((1 + total) / (1 + count)) + 1.0 for term, count in df.items()}


def word_shingles(value: str, size: int = 5) -> set[str]:
    words = re.findall(r"[a-z0-9]+", fold_latin(value or "").lower())
    if len(words) < size:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id", "gclid",
    "fbclid", "mc_cid", "mc_eid", "ref", "ref_src", "cmpid", "icid", "taid", "dclid", "_ga",
}


def url_identity(url: str) -> str:
    """Comparison key: no scheme, no www., no tracking params, no fragment or trailing slash."""
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    query.sort()
    path = re.sub(r"/+$", "", parts.path or "")
    q = ("?" + urlencode(query)) if query else ""
    return f"{host}{path}{q}".lower()


def clean_url(url: str) -> str:
    """Absolute URL without tracking parameters or fragment (keeps scheme/host)."""
    parts = urlsplit((url or "").strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    return urlunsplit((parts.scheme.lower() or "https", parts.netloc.lower(), parts.path or "/", urlencode(query), ""))


def site_host(url: str) -> str:
    host = (urlsplit((url or "").strip()).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def host_matches(url: str, domain: str) -> bool:
    host = site_host(url)
    domain = domain.lower().lstrip(".")
    return host == domain or host.endswith("." + domain)


def legacy_canonicalize_url(u: str) -> str:
    """Exact copy of bot v1 canonicalize_url, used to stay compatible with news_cache.db."""
    p = urlsplit((u or "").strip())
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
          if k.lower() not in {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid"}]
    qs.sort(key=lambda x: (x[0], x[1]))
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urlencode(qs), ""))


_LEGACY_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "from", "by", "at", "is", "are", "was",
    "were", "be", "been", "being", "as", "this", "that", "these", "those", "new", "latest", "update", "updates",
}


def legacy_title_norm(title: str) -> str:
    """Exact copy of bot v1 normalize_en_title (its DB dedup compares these values)."""
    t = re.sub(r"[^a-z0-9\s]", " ", (title or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    return " ".join(w for w in t.split(" ") if w and w not in _LEGACY_STOPWORDS and len(w) >= 3)[:220].strip()


def legacy_url_hash(url: str) -> str:
    return hashlib.sha256(legacy_canonicalize_url(url).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Slugs
# ---------------------------------------------------------------------------
SLUG_STOP_WORDS = {
    "a", "an", "the", "of", "and", "or", "for", "in", "on", "at", "to", "from", "by", "with", "as",
    "is", "are", "was", "were", "be", "been", "it", "its", "this", "that", "these", "those", "has",
    "have", "had", "will", "just", "now", "finally", "here", "heres", "you", "your", "we", "our",
    "they", "their", "can", "could", "may", "might", "gets", "get", "reportedly", "officially",
    "new", "latest", "more", "than", "into", "about", "after", "before", "over", "up", "out", "all",
    "some", "very", "really",
}


def make_latin_slug(text: str, max_words: int = 7, max_len: int = 75) -> str:
    base = unicodedata.normalize("NFKD", clean_text(text or ""))
    base = base.encode("ascii", "ignore").decode("ascii").lower()
    base = re.sub(r"['’`]", "", base)
    base = re.sub(r"[^a-z0-9]+", " ", base)
    base = re.sub(r"\b(?:later |earlier )?this (?:year|month|week)\b", " ", base)
    words = [w for w in base.split() if w not in SLUG_STOP_WORDS]
    if not words:
        return ""
    slug = "-".join(words[: max(1, max_words)])
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0] or slug[:max_len]
    return slug.strip("-")


# ---------------------------------------------------------------------------
# Persian helpers
# ---------------------------------------------------------------------------
_PERSIAN_FIXES = str.maketrans({"ي": "ی", "ك": "ک", "ى": "ی", "ة": "ه", "ـ": None})


def normalize_persian(value: str) -> str:
    value = (value or "").translate(_PERSIAN_FIXES)
    value = re.sub(ZWNJ + "{2,}", ZWNJ, value)
    value = re.sub(r"\s*" + ZWNJ + r"\s*", ZWNJ, value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r" +([،؛؟.!:])", r"\1", value)
    return value.strip()


PERSIAN_LETTER_RE = re.compile(r"[؀-ۿﮊپچگیک]")
LATIN_LETTER_RE = re.compile(r"[A-Za-z]")


def persian_ratio(value: str) -> float:
    persian = len(PERSIAN_LETTER_RE.findall(value or ""))
    latin = len(LATIN_LETTER_RE.findall(value or ""))
    total = persian + latin
    return persian / total if total else 0.0


def count_words(value: str) -> int:
    return len(re.findall(r"[A-Za-z0-9؀-ۿ‌]+", value or ""))


def split_sentences(value: str) -> list[str]:
    parts = re.split(r"(?<=[.!؟?])\s+", collapse_ws(value))
    return [p for p in parts if p]


def extract_numbers(value: str) -> list[str]:
    """Numbers (ASCII digits) including decimals/versions: '1.05', '69.99', '2026', '5090'."""
    value = to_ascii_digits(value or "").replace("٫", ".").replace("٬", ",")
    found = re.findall(r"\d+(?:[.,]\d+)*", value)
    return [n.replace(",", "") if re.fullmatch(r"\d{1,3}(?:,\d{3})+", n) else n for n in found]


def latin_phrases(value: str) -> list[str]:
    return [collapse_ws(m) for m in re.findall(r"[A-Za-z][A-Za-z0-9'’:&.\-+ ]*[A-Za-z0-9+]", value or "")]
