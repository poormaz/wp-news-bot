"""Main-content extraction from publisher pages (no third-party parser).

A small readability-style scorer: paragraphs contribute their text length to their
parent (and half to the grandparent); the best-scoring container is the article body.
Navigation, comments, "related" modules, newsletters and ads are excluded, so the
fact extractor sees the article and not the page chrome.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

from .textutil import collapse_ws

SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "button",
             "select", "iframe", "template", "figure", "picture", "video", "audio", "canvas"}
VOID_TAGS = {"br", "img", "meta", "link", "input", "hr", "source", "wbr", "area", "base", "col", "embed",
             "param", "track"}
TEXT_TAGS = {"p", "li", "h2", "h3", "h4", "blockquote", "td"}
NEGATIVE_ATTR = re.compile(
    r"(comment|related|sidebar|newsletter|promo|share|social|breadcrumb|subscribe|advert|\bad-|ad-slot|"
    r"affiliate|recommend|more-stories|popular|trending|outbrain|taboola|author-bio|byline|tags?-list|"
    r"post-tags|signup|sign-up|cookie|consent|modal|popup|related-posts|read-next|jp-relatedposts|"
    r"wp-block-latest|newsletter|disclaimer|footer|menu|masthead|navigation)",
    re.I,
)
BOILERPLATE = re.compile(
    r"^(read more|continue reading|see more|learn more|advertisement|sponsored|sign up|subscribe|"
    r"follow us|share this|click here|image credit|credit:|source:|via:|related:|you may also like|"
    r"this article contains affiliate links|we may earn a commission)",
    re.I,
)


@dataclass
class ExtractedPage:
    title: str = ""
    site_name: str = ""
    text: str = ""
    paragraphs: list[str] = field(default_factory=list)
    published_at: str = ""
    modified_at: str = ""
    canonical_url: str = ""
    image_url: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)
    h1_count: int = 0
    json_ld_types: list[str] = field(default_factory=list)


class _Parser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.stack: list[tuple[str, int, bool]] = []   # (tag, element id, negative)
        self.elements: dict[int, int] = {}              # element id -> parent id
        self.next_id = 1
        self.skip_depth = 0
        self.paragraphs: list[dict] = []
        self.current: dict | None = None
        self.current_link: dict | None = None
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self.in_title = False
        self.h1_count = 0
        self.json_ld: list[str] = []
        self.in_json_ld = False
        self.canonical = ""

    # -- helpers --------------------------------------------------------
    def _parent_id(self) -> int:
        return self.stack[-1][1] if self.stack else 0

    def _negative(self) -> bool:
        return any(neg for _, _, neg in self.stack)

    def _close_current(self):
        if self.current is not None:
            text = collapse_ws("".join(self.current["parts"]))
            if text:
                self.current["text"] = text
                self.paragraphs.append(self.current)
            self.current = None

    # -- parser callbacks -----------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (attr.get("property") or attr.get("name") or attr.get("itemprop") or "").lower()
            if key and attr.get("content"):
                self.meta.setdefault(key, attr["content"].strip())
            return
        if tag == "link" and "canonical" in attr.get("rel", "").lower() and attr.get("href"):
            self.canonical = urljoin(self.base_url, attr["href"])
            return
        if tag == "script":
            if "ld+json" in attr.get("type", "").lower():
                self.in_json_ld = True
                self.json_ld.append("")
            self.skip_depth += 1
            self.stack.append((tag, 0, True))
            return
        if tag == "title":
            self.in_title = True
        if tag == "h1":
            self.h1_count += 1
        if tag in VOID_TAGS:
            if tag == "br" and self.current is not None:
                self.current["parts"].append(" ")
            return
        if tag == "p" and self.current is not None and self.current["tag"] == "p":
            self._close_current()
        negative = bool(NEGATIVE_ATTR.search(f"{attr.get('id', '')} {attr.get('class', '')} {attr.get('role', '')}"))
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        element_id = self.next_id
        self.next_id += 1
        self.elements[element_id] = self._parent_id()
        self.stack.append((tag, element_id, negative))
        if tag in TEXT_TAGS and self.current is None and not self.skip_depth:
            self.current = {"tag": tag, "parent": self.elements[element_id], "id": element_id,
                            "negative": self._negative(), "parts": [], "links": []}
        if tag == "a" and self.current is not None:
            href = attr.get("href", "").strip()
            if href and not href.lower().startswith(("javascript:", "mailto:", "#")):
                self.current_link = {"href": urljoin(self.base_url, href), "parts": []}

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in ("meta", "link"):
            self.handle_starttag(tag, attrs)
        elif tag.lower() == "br" and self.current is not None:
            self.current["parts"].append(" ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        if tag == "a" and self.current_link is not None and self.current is not None:
            anchor = collapse_ws("".join(self.current_link["parts"]))
            self.current["links"].append((self.current_link["href"], anchor))
            self.current_link = None
        if tag in VOID_TAGS:
            return
        if not any(t == tag for t, _, _ in self.stack):
            return
        while self.stack:
            open_tag, element_id, _ = self.stack.pop()
            if open_tag == "script":
                self.in_json_ld = False
            if open_tag in SKIP_TAGS or open_tag == "script":
                self.skip_depth = max(0, self.skip_depth - 1)
            if self.current is not None and self.current["id"] == element_id:
                self._close_current()
            if open_tag == tag:
                break

    def handle_data(self, data):
        if self.in_json_ld and self.json_ld:
            self.json_ld[-1] += data
            return
        if self.in_title:
            self.title_parts.append(data)
        if self.skip_depth or self.current is None:
            return
        self.current["parts"].append(data)
        if self.current_link is not None:
            self.current_link["parts"].append(data)


def _json_ld_objects(raw_blocks: list[str]) -> list[dict]:
    out: list[dict] = []
    for raw in raw_blocks:
        try:
            data = json.loads(raw.strip())
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                out.append(item)
                if "@graph" in item:
                    stack.append(item["@graph"])
    return out


def extract_page(html: str, base_url: str = "") -> ExtractedPage:
    parser = _Parser(base_url)
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup: keep what was parsed
        pass
    parser._close_current()

    page = ExtractedPage()
    meta = parser.meta
    page.title = collapse_ws(html_lib.unescape(meta.get("og:title") or meta.get("twitter:title")
                                               or "".join(parser.title_parts)))
    page.site_name = collapse_ws(meta.get("og:site_name") or meta.get("application-name") or "")
    page.published_at = meta.get("article:published_time") or meta.get("og:published_time") or \
        meta.get("datepublished") or meta.get("pubdate") or ""
    page.modified_at = meta.get("article:modified_time") or meta.get("og:updated_time") or ""
    page.canonical_url = parser.canonical
    image = meta.get("og:image") or meta.get("og:image:url") or meta.get("twitter:image") or ""
    page.image_url = urljoin(base_url, image) if image else ""
    page.h1_count = parser.h1_count

    article_body = ""
    for obj in _json_ld_objects(parser.json_ld):
        types = obj.get("@type")
        types = types if isinstance(types, list) else [types]
        page.json_ld_types.extend(str(t) for t in types if t)
        if not page.published_at and obj.get("datePublished"):
            page.published_at = str(obj["datePublished"])
        body = obj.get("articleBody")
        if isinstance(body, str) and len(body) > len(article_body):
            article_body = body

    # Score containers.
    scores: dict[int, float] = {}
    candidates = [p for p in parser.paragraphs if not p["negative"]]
    for para in candidates:
        length = len(para["text"])
        if length < 25:
            continue
        weight = min(length, 1200)
        parent = para["parent"]
        scores[parent] = scores.get(parent, 0.0) + weight
        grand = parser.elements.get(parent, 0)
        if grand:
            scores[grand] = scores.get(grand, 0.0) + weight / 2
    best = max(scores, key=scores.get) if scores else 0

    def inside(element_id: int, container: int) -> bool:
        seen = 0
        while element_id and seen < 200:
            if element_id == container:
                return True
            element_id = parser.elements.get(element_id, 0)
            seen += 1
        return False

    selected = [p for p in candidates if best and inside(p["id"], best)] or candidates
    paragraphs: list[str] = []
    links: list[tuple[str, str]] = []
    seen_text: set[str] = set()
    for para in selected:
        text = para["text"]
        if len(text) < 2 or BOILERPLATE.match(text) or text in seen_text:
            continue
        if para["tag"] == "li" and len(text) < 15:
            continue
        seen_text.add(text)
        paragraphs.append(text)
        links.extend(para["links"])

    body_text = "\n".join(paragraphs)
    if len(article_body) > len(body_text) * 1.2:
        body_text = collapse_ws(html_lib.unescape(article_body))
        paragraphs = [body_text]
    page.paragraphs = paragraphs
    page.text = body_text
    page.links = links
    return page
