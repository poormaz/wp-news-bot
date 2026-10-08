"""Stage 9: independent Persian article composition + Gutenberg rendering.

The model returns plain Persian text (no HTML) with [[L1|anchor]] placeholders for
internal links. All markup is produced here: text is escaped, links are resolved only
from the validated candidate list, and the source list is built from the verified
documents. Model output therefore cannot inject markup, URLs or instructions.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re

from .config import Settings
from .llm import LLMClient
from .models import Article, FactSheet, LinkCandidate, SourceDoc, Story
from .prompts import COMPOSE_SCHEMA, COMPOSE_SYSTEM, build_compose_input
from .textutil import clean_text, collapse_ws, count_words, make_latin_slug, normalize_persian

log = logging.getLogger("newsbot.compose")

LINK_RE = re.compile(r"\[\[\s*(L\d+)\s*\|\s*([^\]\|]{1,80})\]\]")
MARKER_PREFIX = "poormaz-newsbot v2 nbstory-"


def marker_for(story_id: str) -> str:
    return f"{MARKER_PREFIX}{story_id}"


def word_budget(sheet: FactSheet, settings: Settings) -> tuple[int, int]:
    claims = len(sheet.usable_claims)
    max_words = min(settings.max_words, 120 + settings.words_per_claim * claims)
    min_words = min(settings.min_words, max(60, max_words - 120))
    return min_words, max_words


def _clean_model_text(text: str) -> str:
    text = re.sub(r"(?s)<[^>]+>", " ", str(text or ""))       # the model must not emit HTML
    text = html_lib.unescape(text)
    text = re.sub(r"https?://\S+", "", text)                     # nor raw URLs
    return normalize_persian(collapse_ws(text))


def parse_article(data: dict) -> Article:
    def para(row) -> dict:
        row = row if isinstance(row, dict) else {}
        return {"text": _clean_model_text(row.get("text", "")),
                "claim_ids": [str(c).strip() for c in row.get("claim_ids") or [] if str(c).strip()]}

    sections = []
    for section in data.get("sections") or []:
        if not isinstance(section, dict):
            continue
        paragraphs = [p for p in (para(p) for p in section.get("paragraphs") or []) if p["text"]]
        if paragraphs:
            sections.append({"heading_fa": _clean_model_text(section.get("heading_fa", "")), "paragraphs": paragraphs})
    return Article(
        title_fa=_clean_model_text(data.get("title_fa", "")),
        meta_title_fa=_clean_model_text(data.get("meta_title_fa", "")),
        meta_description_fa=_clean_model_text(data.get("meta_description_fa", "")),
        focus_keyword_fa=_clean_model_text(data.get("focus_keyword_fa", "")),
        slug_en=make_latin_slug(str(data.get("slug_en", "")), max_words=7),
        lead=para(data.get("lead")),
        sections=sections,
        uncertainties=[t for t in (_clean_model_text(u) for u in data.get("uncertainties_fa") or []) if t],
        entity_tags=[collapse_ws(str(t)) for t in data.get("entity_tags") or [] if str(t).strip()][:5],
        content_type=str(data.get("content_type", "gaming")),
        coverage=dict(data.get("coverage") or {}),
    )


class Composer:
    def __init__(self, settings: Settings, llm: LLMClient, store=None):
        self.settings = settings
        self.llm = llm
        self.store = store

    def compose(self, story: Story, sheet: FactSheet, view: dict, links: list[LinkCandidate],
                feedback: list[str] | None = None, revision: int = 0) -> Article:
        min_words, max_words = word_budget(sheet, self.settings)
        avoid_anchors: list[str] = []
        avoid_openings: list[str] = []
        if self.store is not None:
            for link in links:
                avoid_anchors.extend(self.store.recent_anchors(link.url, 5))
            avoid_openings = [o[:90] for o in self.store.recent_openings(8)]
        editorial = {
            "min_words": min_words,
            "max_words": max_words,
            "story_status": view.get("story_status"),
            "is_development_of_earlier_story": story.is_development,
            "earlier_poormaz_coverage": (story.parent_post or {}).get("title", ""),
            "avoid_anchors": sorted(set(avoid_anchors))[:12],
            "do_not_start_like": avoid_openings,
        }
        if feedback:
            editorial["revision_instructions"] = feedback[:12]
        link_rows = [{"id": c.id, "kind": c.kind, "page_title": c.title, "about": c.entity} for c in links]
        system = COMPOSE_SYSTEM.replace("{min_words}", str(min_words)).replace("{max_words}", str(max_words))
        data = self.llm.call_json("compose" if not revision else f"compose_r{revision}", system,
                                  build_compose_input(view, link_rows, editorial), "article", COMPOSE_SCHEMA,
                                  self.settings.max_output_tokens_compose, self.settings.compose_reasoning_effort)
        article = parse_article(data)
        article.revision = revision
        return article


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _p(inner: str) -> str:
    return f"<!-- wp:paragraph -->\n<p>{inner}</p>\n<!-- /wp:paragraph -->" if inner.strip() else ""


def _h2(text: str) -> str:
    return ('<!-- wp:heading -->\n<h2 class="wp-block-heading">' + html_lib.escape(text)
            + "</h2>\n<!-- /wp:heading -->")


def _list(items: list[str]) -> str:
    lis = "\n".join(f"<!-- wp:list-item -->\n<li>{i}</li>\n<!-- /wp:list-item -->" for i in items if i)
    return f'<!-- wp:list -->\n<ul class="wp-block-list">\n{lis}\n</ul>\n<!-- /wp:list -->' if lis else ""


def valid_anchor(anchor: str, recent: list[str]) -> bool:
    anchor = collapse_ws(anchor)
    words = len(anchor.split())
    return 1 <= words <= 8 and len(anchor) <= 70 and anchor not in recent and "http" not in anchor


def render_text(text: str, links: dict[str, LinkCandidate], used: dict[str, str], recent: dict[str, list[str]],
                allow_links: bool, max_links: int) -> str:
    out: list[str] = []
    last = 0
    for match in LINK_RE.finditer(text):
        out.append(html_lib.escape(text[last:match.start()]))
        link_id, anchor = match.group(1), collapse_ws(match.group(2))
        cand = links.get(link_id)
        if (allow_links and cand and link_id not in used and len(used) < max_links
                and valid_anchor(anchor, recent.get(cand.url, []))):
            used[link_id] = anchor
            out.append(f'<a href="{html_lib.escape(cand.url, quote=True)}">{html_lib.escape(anchor)}</a>')
        else:
            out.append(html_lib.escape(anchor))
        last = match.end()
    out.append(html_lib.escape(text[last:]))
    # Any leftover bracket syntax the model mangled is shown as plain text, never markup.
    return re.sub(r"\[\[|\]\]", "", "".join(out)).strip()


def strip_placeholders(text: str) -> str:
    return re.sub(r"\[\[|\]\]", "", LINK_RE.sub(lambda m: m.group(2), text or ""))


def source_section(sheet: FactSheet, docs: list[SourceDoc], used_claims: set[str], nofollow: bool) -> tuple[str, list[dict]]:
    by_id = {d.doc_id: d for d in docs}
    cited: list[str] = []
    for claim in sheet.usable_claims:
        if claim.id in used_claims:
            for sid in claim.verified_sources:
                if sid not in cited:
                    cited.append(sid)
    if not cited:
        return "", []
    ordered = sorted((by_id[s] for s in cited if s in by_id), key=lambda d: (not d.is_official, d.doc_id))
    rel = "nofollow noopener noreferrer" if nofollow else "noopener noreferrer"
    items, rows = [], []
    for doc in ordered:
        label = clean_text(doc.title) or doc.outlet
        kind = "منبع رسمی" if doc.is_official else "گزارش"
        items.append(f'{html_lib.escape(kind)}: <a href="{html_lib.escape(doc.url, quote=True)}" target="_blank" '
                     f'rel="{rel}">{html_lib.escape(doc.outlet)}</a> — {html_lib.escape(label[:140])}')
        rows.append({"outlet": doc.outlet, "url": doc.url, "official": doc.is_official})
    return _h2("منابع خبر") + "\n\n" + _list(items), rows


def render_article(article: Article, story: Story, sheet: FactSheet, docs: list[SourceDoc],
                   links: list[LinkCandidate], settings: Settings, recent_anchors: dict[str, list[str]] | None = None,
                   fallback_sentence: str = "") -> Article:
    link_map = {c.id: c for c in links}
    used: dict[str, str] = {}
    recent = recent_anchors or {}
    blocks: list[str] = []
    plain: list[str] = []

    lead = article.lead.get("text", "")
    blocks.append(_p(render_text(lead, link_map, used, recent, False, settings.max_internal_links)))
    plain.append(strip_placeholders(lead))
    for section in article.sections:
        if section.get("heading_fa"):
            blocks.append(_h2(section["heading_fa"]))
        for para in section["paragraphs"]:
            blocks.append(_p(render_text(para["text"], link_map, used, recent, True, settings.max_internal_links)))
            plain.append(strip_placeholders(para["text"]))
    if fallback_sentence:
        blocks.append(_p(fallback_sentence))
        plain.append(clean_text(fallback_sentence))
    if article.uncertainties:
        if len(article.uncertainties) == 1:
            blocks.append(_p(html_lib.escape(article.uncertainties[0])))
        else:
            blocks.append(_h2("آنچه هنوز مشخص نیست"))
            blocks.append(_list([html_lib.escape(u) for u in article.uncertainties]))
        plain.extend(article.uncertainties)

    used_claims = {cid for p in article.paragraphs() for cid in p.get("claim_ids", [])}
    sources_html, _rows = source_section(sheet, docs, used_claims, settings.source_links_nofollow)
    if sources_html:
        blocks.append(sources_html)
    blocks.append(f"<!-- wp:html -->\n<!-- {marker_for(story.story_id)} -->\n<!-- /wp:html -->")

    article.links_used = [{"id": k, "url": link_map[k].url, "anchor": v, "kind": link_map[k].kind}
                          for k, v in used.items()]
    article.html = "\n\n".join(b for b in blocks if b).strip()
    article.plain_text = "\n".join(p for p in plain if p)
    article.word_count = count_words(article.plain_text)
    return article


def article_debug(article: Article) -> str:
    return json.dumps({"title": article.title_fa, "words": article.word_count, "links": article.links_used},
                      ensure_ascii=False)
