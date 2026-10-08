"""Stage 10: editorial fact checking (deterministic rules + optional model check)."""

from __future__ import annotations

import logging
import re

from .config import Settings
from .facts import story_status
from .llm import LLMClient
from .models import Article, CheckResult, FactSheet, SourceDoc, Story
from .prompts import CHECK_SCHEMA, CHECK_SYSTEM, build_check_input
from .textutil import (
    char_ngrams,
    collapse_ws,
    entity_key,
    extract_numbers,
    jaccard,
    latin_phrases,
    persian_ratio,
    split_sentences,
    to_ascii_digits,
)

log = logging.getLogger("newsbot.factcheck")

FABRICATION_PATTERNS = re.compile(
    r"(ما (?:بازی را )?(?:تست|تجربه|بازی) کردیم|در (?:تست|آزمایش|بررسی)(?:‌| )?های (?:ما|پورماز)|"
    r"تجربه (?:ما|شخصی)|به(?:‌| )صورت اختصاصی|گزارش اختصاصی|در (?:گفتگو|مصاحبه) با پورماز|"
    r"به پورماز گفت|بنچمارک(?:‌| )?های (?:ما|پورماز)|نگارنده|ما در پورماز (?:بازی|آن) را|"
    r"دست(?:‌| )?اول (?:تجربه|بررسی) کردیم|پس از ساعت‌ها بازی)"
)
CLICHE_PATTERNS = re.compile(
    r"(در دنیای (?:بازی‌های ویدیویی|گیم|بازی‌ها)|همان(?:‌| )طور که می‌دانید|همانطور که میدانید|در این مقاله|"
    r"خبر خوب برای (?:طرفداران|گیمرها)|خبری داغ|منتظر باشید|با ما همراه باشید|شگفت‌انگیز|باورنکردنی|"
    r"انقلابی در صنعت)"
)
HEDGE_FA = re.compile(r"(شایعه|گزارش|تأیید نشده|تایید نشده|ادعا|احتمال|ظاهراً|ظاهرا|به نظر می‌رسد|غیررسمی|"
                      r"لیک|فاش|هنوز رسمی|منابع آگاه|افشا)")
TITLE_HEDGE_FA = re.compile(r"(شایعه|گزارش|احتمال|ادعا|لیک|فاش|افشا|ظاهراً|ظاهرا)")
ATTRIBUTION_FA = re.compile(r"(به گزارش|طبق گزارش|بنا بر گزارش|به نقل از|گزارش داده|گزارش کرده|اعلام کرد|"
                            r"اعلام کرده|اعلام شد|منتشر کرده|به گفته|بر اساس|طبق اعلام|براساس)")
PERSIAN_QUOTE = re.compile(r"«([^»]{3,400})»")
ALLOWED_LATIN = {
    "pc", "dlc", "rpg", "fps", "aaa", "npc", "hdr", "4k", "8k", "vr", "ai", "dlss", "fsr", "ps5", "ps4", "xbox",
    "steam", "gpu", "cpu", "ram", "ssd", "mmo", "mmorpg", "moba", "pvp", "pve", "co op", "beta", "demo",
    "nvidia", "amd", "intel", "usd", "eur", "goty", "ui", "ux", "api", "id", "tv", "oled", "lcd", "qhd", "fhd",
    "rtx", "rx", "arm", "x", "s", "pro", "max", "ultra", "mini", "series",
}


NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14", "fifteen": "15",
    "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70", "eighty": "80", "ninety": "90",
    "hundred": "100", "thousand": "1000", "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
    "dozen": "12",
}


def evidence_numbers(evidence: str) -> set[str]:
    numbers = set(extract_numbers(evidence))
    for word in re.findall(r"[a-z]+", evidence.lower()):
        if word in NUMBER_WORDS:
            numbers.add(NUMBER_WORDS[word])
    # "1.5 million" may be written as ۱٫۵ میلیون; "2026-03-19" as separate parts.
    for number in list(numbers):
        numbers.update(p for p in re.split(r"[.,]", number) if p)
    return numbers


def _evidence_text(sheet: FactSheet, docs: list[SourceDoc]) -> str:
    parts = [sheet.headline_en]
    for claim in sheet.usable_claims:
        parts.extend([claim.text_en, claim.value, claim.subject])
        parts.extend(s["quote"] for s in claim.support)
    for doc in docs:
        parts.extend([doc.outlet, doc.title, doc.text])
    for values in sheet.entities.values():
        parts.extend(values)
    return "\n".join(p for p in parts if p)


def text_findings(title: str, text: str, sheet: FactSheet, docs: list[SourceDoc], platform_names: set[str]) -> dict:
    """Numbers and Latin-script names not found in the evidence, plus fabrication phrases."""
    evidence = _evidence_text(sheet, docs)
    supported_numbers = evidence_numbers(evidence)
    article_numbers = extract_numbers(to_ascii_digits(f"{title}\n{text}"))
    unsupported_numbers = sorted({n for n in article_numbers if n not in supported_numbers
                                  and n.replace(".", ",") not in supported_numbers})
    evidence_key = " " + entity_key(evidence) + " "
    allowed = ALLOWED_LATIN | {entity_key(p) for p in platform_names}
    unsupported_names = []
    for phrase in latin_phrases(f"{title}\n{text}"):
        key = entity_key(phrase)
        if not key or key in allowed or len(key) < 2:
            continue
        parts = [w for w in key.split() if w not in allowed]
        if not parts or f" {key} " in evidence_key or all(f" {w} " in evidence_key for w in parts):
            continue
        unsupported_names.append(phrase)
    return {"unsupported_numbers": unsupported_numbers, "unsupported_names": unsupported_names,
            "fabrication": FABRICATION_PATTERNS.findall(text)}


def _used_claim_ids(article: Article) -> list[str]:
    ids: list[str] = []
    for para in article.paragraphs():
        ids.extend(para.get("claim_ids", []))
    return ids


def rule_checks(article: Article, story: Story, sheet: FactSheet, docs: list[SourceDoc], settings: Settings,
                min_words: int, max_words: int, recent_openings: list[str], platform_names: set[str]) -> list[CheckResult]:
    checks: list[CheckResult] = []
    add = checks.append
    usable = {c.id: c for c in sheet.usable_claims}
    all_ids = {c.id for c in sheet.claims}
    text = article.plain_text
    body_paragraphs = article.paragraphs()

    # Structure and length ------------------------------------------------------
    add(CheckResult("structure.lead", bool(article.lead.get("text")), True, "lead paragraph missing"
                    if not article.lead.get("text") else ""))
    add(CheckResult("structure.paragraphs", len(body_paragraphs) >= 2, True, f"{len(body_paragraphs)} paragraph(s)"))
    add(CheckResult("length.minimum", article.word_count >= min_words, True,
                    f"{article.word_count} words < {min_words}" if article.word_count < min_words else
                    f"{article.word_count} words"))
    add(CheckResult("length.no_padding", article.word_count <= int(max_words * 1.15), True,
                    f"{article.word_count} words > budget {max_words} for {len(usable)} verified claims"
                    if article.word_count > int(max_words * 1.15) else ""))
    headings = sum(1 for s in article.sections if s.get("heading_fa"))
    add(CheckResult("structure.headings", headings <= 5 and (headings == 0 or len(article.sections) >= 2), False,
                    f"{headings} heading(s) for {len(article.sections)} section(s)"))

    # Citations / provenance -----------------------------------------------------
    used = _used_claim_ids(article)
    invalid = sorted({i for i in used if i not in usable})
    unverified_used = sorted({i for i in invalid if i in all_ids})
    add(CheckResult("citations.verified_only", not invalid, True,
                    (f"cites unverified claim(s) {', '.join(unverified_used)}" if unverified_used else
                     f"cites unknown id(s) {', '.join(invalid)}") if invalid else ""))
    uncited = [i for i, p in enumerate(body_paragraphs) if not p.get("claim_ids")]
    add(CheckResult("citations.every_paragraph", len(uncited) <= 1, True,
                    f"paragraph(s) {uncited} cite no claim" if uncited else ""))
    core = {c.id for c in sheet.core_claims}
    add(CheckResult("citations.core_claim_used", not core or bool(core & set(used)), True,
                    "" if (not core or core & set(used)) else "none of the core claims is used"))

    # Exact facts ------------------------------------------------------------------
    findings = text_findings(article.title_fa, text, sheet, docs, platform_names)
    unsupported_numbers = findings["unsupported_numbers"]
    unsupported_names = findings["unsupported_names"]
    add(CheckResult("facts.numbers_supported", not unsupported_numbers, True,
                    f"numbers not in verified sources: {', '.join(unsupported_numbers[:8])}" if unsupported_numbers else ""))
    add(CheckResult("facts.names_supported", not unsupported_names, True,
                    f"names not found in sources: {', '.join(unsupported_names[:6])}" if unsupported_names else ""))

    # Fabrication / quotations -----------------------------------------------------
    fabricated = findings["fabrication"]
    add(CheckResult("fabrication.first_hand", not fabricated, True,
                    f"claims first-hand testing/exclusivity: {fabricated[:3]}" if fabricated else ""))
    quote_claims = [c for c in sheet.usable_claims if c.category == "quote" and c.id in used]
    long_quotes = [q for q in PERSIAN_QUOTE.findall(text) if len(q.split()) >= 6 and persian_ratio(q) > 0.6]
    add(CheckResult("fabrication.quotes", not long_quotes or bool(quote_claims), True,
                    f"{len(long_quotes)} quotation(s) without a verified quote claim" if long_quotes and not quote_claims
                    else ""))

    # Claim status framing ----------------------------------------------------------
    status = story_status(sheet)
    speculative_used = [usable[i] for i in used if i in usable and usable[i].status == "speculative"]
    reported_used = [usable[i] for i in used if i in usable and usable[i].status == "reported"]
    if status == "rumor" or speculative_used:
        add(CheckResult("status.rumor_framed_in_text", bool(HEDGE_FA.search(text)), True,
                        "speculative claims are not framed as unconfirmed"))
    if status == "rumor":
        add(CheckResult("status.rumor_framed_in_title", bool(TITLE_HEDGE_FA.search(article.title_fa)), True,
                        "title presents a rumor as confirmed"))
    if reported_used:
        add(CheckResult("status.reported_attributed", bool(ATTRIBUTION_FA.search(text)), False,
                        "reported claims used without explicit attribution"))

    # Persian quality ------------------------------------------------------------------
    ratio = persian_ratio(text)
    add(CheckResult("persian.script_ratio", ratio >= 0.55, True, f"Persian letter ratio {ratio:.2f}"))
    long_latin = [p for p in latin_phrases(text) if len(p.split()) >= 7]
    add(CheckResult("persian.untranslated_english", not long_latin, True,
                    f"untranslated English: {long_latin[0][:80]}" if long_latin else ""))
    lead_cliche = CLICHE_PATTERNS.search(article.lead.get("text", ""))
    add(CheckResult("persian.no_cliche_lead", not lead_cliche, True,
                    f"clichéd opening: {lead_cliche.group(0)}" if lead_cliche else ""))
    cliches = CLICHE_PATTERNS.findall(text)
    add(CheckResult("persian.no_cliches", not cliches, False, f"clichés: {cliches[:3]}" if cliches else ""))
    sentences = [collapse_ws(s) for s in split_sentences(text) if len(s) > 25]
    dupes = len(sentences) - len(set(sentences))
    add(CheckResult("persian.no_repetition", dupes == 0, True, f"{dupes} repeated sentence(s)" if dupes else ""))
    excl = text.count("!")
    add(CheckResult("persian.no_exclamations", excl == 0, False, f"{excl} exclamation mark(s)" if excl else ""))
    lead_first = split_sentences(article.lead.get("text", ""))[:1]
    if lead_first and recent_openings:
        grams = set(char_ngrams(lead_first[0]))
        similar = max((jaccard(grams, set(char_ngrams(o))) for o in recent_openings), default=0.0)
        add(CheckResult("persian.fresh_opening", similar < 0.6, True,
                        f"opening resembles a recent article ({similar:.2f})" if similar >= 0.6 else ""))

    # SEO -------------------------------------------------------------------------------
    title = article.title_fa
    add(CheckResult("seo.title_length", 15 <= len(title) <= 95, True, f"title length {len(title)}"))
    add(CheckResult("seo.title_no_clickbait", "!" not in title and not CLICHE_PATTERNS.search(title), True,
                    "clickbait title" if ("!" in title or CLICHE_PATTERNS.search(title)) else ""))
    if story.primary_display and story.primary_entity:
        present = entity_key(story.primary_display) in entity_key(title) or \
            story.primary_entity in entity_key(title)
        add(CheckResult("seo.title_names_subject", present, False,
                        "" if present else f"title does not name {story.primary_display}"))
    add(CheckResult("seo.meta_description", 70 <= len(article.meta_description_fa) <= 170, False,
                    f"meta description length {len(article.meta_description_fa)}"))
    keyword = collapse_ws(article.focus_keyword_fa)
    if keyword:
        count = text.count(keyword)
        limit = max(3, article.word_count // 150)
        add(CheckResult("seo.no_keyword_stuffing", count <= limit, True,
                        f"focus keyword repeated {count} times (limit {limit})" if count > limit else ""))
    return checks


def llm_check(article: Article, sheet_view: dict, llm: LLMClient, settings: Settings) -> tuple[list[CheckResult], dict]:
    paragraphs = [p.get("text", "") for p in article.paragraphs()] + list(article.uncertainties)
    data = llm.call_json("factcheck", CHECK_SYSTEM, build_check_input(sheet_view, paragraphs, article.title_fa),
                         "fact_check", CHECK_SCHEMA, settings.max_output_tokens_check, settings.reasoning_effort)
    issues = [i for i in data.get("issues") or [] if isinstance(i, dict)]
    high = [i for i in issues if i.get("severity") == "high"]
    medium = [i for i in issues if i.get("severity") == "medium"]
    language = data.get("language") or {}
    checks = [
        CheckResult("factcheck.model_high_severity", not high, True,
                    "; ".join(f"[{i.get('problem')}] {i.get('explanation', '')[:140]}" for i in high[:3])),
        CheckResult("factcheck.model_medium_severity", len(medium) <= 1, True,
                    "; ".join(f"[{i.get('problem')}] {i.get('explanation', '')[:140]}" for i in medium[:3])),
        CheckResult("language.fluent", bool(language.get("fluent", True)) and not language.get("translationese"),
                    True, "; ".join(language.get("problems") or [])[:300]),
        CheckResult("factcheck.model_verdict", data.get("verdict") != "reject", True,
                    f"verdict={data.get('verdict')}"),
    ]
    return checks, data


FIXABLE = {
    "length.minimum": "The article is too short; use more of the verified claims (do not invent anything).",
    "length.no_padding": "The article is too long for the available facts; remove repetition and filler.",
    "citations.every_paragraph": "Every paragraph must list the claim ids it relies on.",
    "citations.verified_only": "Only cite claim ids that exist in the fact sheet.",
    "citations.core_claim_used": "State the core news (core claims) in the lead.",
    "facts.numbers_supported": "Remove or correct numbers that are not in the fact sheet.",
    "facts.names_supported": "Remove names that are not in the fact sheet.",
    "fabrication.first_hand": "Remove any claim of first-hand testing, interviews or exclusivity.",
    "fabrication.quotes": "Remove quotations that are not verified quote claims.",
    "status.rumor_framed_in_text": "Frame speculative claims clearly as unconfirmed rumors.",
    "status.rumor_framed_in_title": "The title must signal that the story is a rumor or report.",
    "persian.untranslated_english": "Translate leftover English sentences into Persian.",
    "persian.no_cliche_lead": "Rewrite the lead without clichés; state the news directly.",
    "persian.no_repetition": "Remove repeated sentences.",
    "persian.fresh_opening": "Use a different opening sentence than recent Poormaz articles.",
    "persian.script_ratio": "Write in Persian; keep only names in English.",
    "seo.title_length": "Write a clear headline of 30 to 80 characters.",
    "seo.title_no_clickbait": "Remove exclamation marks and hype from the title.",
    "seo.no_keyword_stuffing": "Use the focus keyword naturally, not repeatedly.",
    "factcheck.model_high_severity": "Fix these fact-check findings: ",
    "factcheck.model_medium_severity": "Fix these fact-check findings: ",
    "language.fluent": "Rewrite in natural, fluent Persian newsroom prose (not translationese): ",
}


def revision_feedback(failed: list[CheckResult]) -> list[str]:
    out = []
    for check in failed:
        hint = FIXABLE.get(check.name)
        if hint:
            out.append(hint + (check.detail if hint.endswith(": ") else f" ({check.detail})" if check.detail else ""))
    return out
