"""Stage 10: editorial fact checking (deterministic rules + optional model check)."""

from __future__ import annotations

import logging
import re

from .compose import ungrounded_links
from .config import Settings
from .facts import story_status
from .llm import LLMClient
from .models import Article, CheckResult, FactSheet, LinkCandidate, SourceDoc, Story
from .prompts import CHECK_SCHEMA, CHECK_SYSTEM, build_check_input
from .textutil import (
    char_ngrams,
    collapse_ws,
    entity_key,
    extract_numbers,
    fa_similarity,
    jaccard,
    latin_phrases,
    persian_ratio,
    split_sentences,
    strip_link_markup,
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
ABSOLUTE_MIN_WORDS = 60
PERSIAN_QUOTE = re.compile(r"«([^»]{3,400})»")
# Sentences that only point readers elsewhere ("see our earlier coverage"); links belong on names instead.
POINTER_FA = re.compile(
    r"(را (?:هم |نیز )?(?:ببینید|ببینند|بخوانید|بخوانند|دنبال کنید|مطالعه کنید)|سر بزنید|سر بزنند|"
    r"مراجعه کنید|مراجعه کنند|(?:پوشش|جزئیات|اخبار|خبرهای|گزارش|گزارش‌های|مطالب|مطلب)(?: ما)? "
    r"(?:پیشین|قبلی|مرتبط)|خبر مرتبط|مطلب مرتبط|برای مطالعه بیشتر|برای آشنایی بیشتر|برای اطلاعات بیشتر|"
    r"پورماز)"
)
# Comments about the information itself instead of news ("the available information does not say").
META_FILLER_FA = re.compile(
    r"((?:اطلاعات|جزئیات|داده‌های|منابع)(?:ی)? (?:موجود|اعلام‌شده|اعلام شده|منتشرشده|منتشر شده|تأییدشده|"
    r"تاییدشده|تأیید شده|ارائه‌شده|فعلی|در دسترس)|فکت ?شیت|فکت‌شیت|برگه(?:ٔ|‌ی| ی)? (?:اطلاعات|حقایق)|"
    r"در این (?:خبر|گزارش|مطلب|مقاله))"
)
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


def _model_sentences(article: Article) -> list[str]:
    texts = [strip_link_markup(p.get("text", "")) for p in article.paragraphs()] + list(article.uncertainties)
    return [collapse_ws(s) for t in texts for s in split_sentences(t) if s.strip()]


def near_duplicates(sentences: list[str]) -> list[tuple[str, str]]:
    pairs = []
    for i, a in enumerate(sentences):
        for b in sentences[i + 1:]:
            ratio, shared = fa_similarity(a, b)
            if shared >= 3 and ratio >= 0.75 and a != b:
                pairs.append((a, b))
    return pairs


def presentation_checks(article: Article, links: list[LinkCandidate] | None) -> list[CheckResult]:
    """Filler, "see also" sentences, restated points and links on anything but a name."""
    sentences = _model_sentences(article)
    pointers = [s for s in sentences if POINTER_FA.search(s)]
    meta = [s for s in sentences if META_FILLER_FA.search(s)]
    body = [collapse_ws(s) for p in article.paragraphs() for s in split_sentences(strip_link_markup(p.get("text", "")))]
    dupes = near_duplicates(body)
    bad_links = ungrounded_links(article, links or [])
    return [
        CheckResult("links.no_pointer_sentences", not pointers, True,
                    "; ".join(f"«{s[:120]}»" for s in pointers[:3])),
        CheckResult("style.no_meta_filler", not meta, True, "; ".join(f"«{s[:120]}»" for s in meta[:3])),
        CheckResult("persian.no_near_duplicates", not dupes, True,
                    "; ".join(f"«{a[:80]}» ≈ «{b[:80]}»" for a, b in dupes[:2])),
        CheckResult("links.anchor_grounded", not bad_links, True,
                    "link markup not on a name in the target page title: " + ", ".join(bad_links[:4])
                    if bad_links else ""),
    ]


def rule_checks(article: Article, story: Story, sheet: FactSheet, docs: list[SourceDoc], settings: Settings,
                min_words: int, max_words: int, recent_openings: list[str], platform_names: set[str],
                links: list[LinkCandidate] | None = None) -> list[CheckResult]:
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
    # No word quota: a short article that states the verified facts once is the goal. Only output too short
    # to be a news article at all (truncated or empty) is blocking; the soft target is a warning.
    add(CheckResult("length.minimum", article.word_count >= ABSOLUTE_MIN_WORDS, True,
                    f"{article.word_count} words < {ABSOLUTE_MIN_WORDS}" if article.word_count < ABSOLUTE_MIN_WORDS
                    else f"{article.word_count} words"))
    add(CheckResult("length.target", article.word_count >= min_words, False,
                    f"{article.word_count} words (soft target {min_words})" if article.word_count < min_words else ""))
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
    # Core news that rests on ONE publication's own reporting (the balanced single-source policy) must
    # be attributed in the text to that outlet and signalled in the headline; otherwise this stays a hint.
    by_doc = {d.doc_id: d for d in docs}
    single_reported = [c for c in sheet.core_claims
                       if c.id in used and c.verification == "single_source" and c.status == "reported"]
    if single_reported:
        outlets = sorted({by_doc[sid].outlet for c in single_reported for sid in c.verified_sources if sid in by_doc})
        named = [o for o in outlets if o and o.lower() in text.lower()]
        ok = bool(ATTRIBUTION_FA.search(text)) and len(named) == len(outlets)
        add(CheckResult("status.reported_attributed", ok, True,
                        "" if ok else f"single-source reporting must be attributed in the text to {', '.join(outlets)}"))
        title_ok = bool(TITLE_HEDGE_FA.search(article.title_fa)) or any(
            o.lower() in article.title_fa.lower() for o in outlets if o)
        add(CheckResult("status.title_attributed", title_ok, True,
                        "" if title_ok else "the headline presents one outlet's report as established fact"))
    elif reported_used:
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
    checks.extend(presentation_checks(article, links))
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


MODEL_BLOCKING_SEVERITIES = ("high", "medium")
# A "reject" verdict is final: no revision is attempted (see pipeline.process_story).
NO_REVISION_CHECKS = {"factcheck.model_rejected"}


def _issue_line(issue: dict) -> str:
    excerpt = collapse_ws(str(issue.get("excerpt_fa", "")))[:160]
    explanation = collapse_ws(str(issue.get("explanation", "")))[:200]
    where = f"paragraph {issue.get('paragraph')}" if issue.get("paragraph") is not None else "article"
    text = f"[{issue.get('problem', 'other')}/{issue.get('severity', '?')}] {where}"
    if excerpt:
        text += f": «{excerpt}»"
    if explanation:
        text += f" — {explanation}"
    return text


def model_check_results(data: dict) -> list[CheckResult]:
    """Turn the model fact check into blocking gate checks.

    Only verdict "pass" with no high/medium issue and fluent Persian passes. "revise" fails
    (and is fixable by a revision whose result is checked again); "reject" fails and is final.
    A missing or unknown verdict counts as a failure, never as a pass.
    """
    data = data if isinstance(data, dict) else {}
    issues = [i for i in data.get("issues") or [] if isinstance(i, dict)]
    blocking = [i for i in issues if str(i.get("severity", "")).lower() in MODEL_BLOCKING_SEVERITIES]
    verdict = str(data.get("verdict") or "").strip().lower()
    language = data.get("language") or {}
    fluent = bool(language.get("fluent", True)) and not language.get("translationese")
    # When the model asks for a revision, every issue it lists (also "low") is worth fixing.
    to_fix = issues if verdict != "pass" else blocking
    return [
        CheckResult("factcheck.model_rejected", verdict != "reject", True,
                    "fact checker rejected the article: " + "; ".join(_issue_line(i) for i in issues[:4])
                    if verdict == "reject" else ""),
        CheckResult("factcheck.model_issues", not blocking, True,
                    "; ".join(_issue_line(i) for i in blocking[:5])),
        CheckResult("language.fluent", fluent, True, "; ".join(str(p) for p in language.get("problems") or [])[:300]),
        CheckResult("factcheck.model_verdict", verdict == "pass", True,
                    "" if verdict == "pass" else
                    f"verdict={verdict or 'missing'}: " + ("; ".join(_issue_line(i) for i in to_fix[:5])
                                                           or "no issue listed")),
    ]


def llm_check(article: Article, sheet_view: dict, llm: LLMClient, settings: Settings,
              links: list[dict] | None = None) -> tuple[list[CheckResult], dict]:
    paragraphs = [p.get("text", "") for p in article.paragraphs()] + list(article.uncertainties)
    data = llm.call_json("factcheck", CHECK_SYSTEM, build_check_input(sheet_view, paragraphs, article.title_fa, links),
                         "fact_check", CHECK_SCHEMA, settings.max_output_tokens_check, settings.reasoning_effort)
    return model_check_results(data), data


FIXABLE = {
    "length.minimum": "The article is too short to be a news article; state the verified core claims "
                      "(never invent anything or pad).",
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
    "status.reported_attributed": "Attribute the reported news in the lead to the outlet that reported it "
                                  "(for example «به گزارش DSOGaming»).",
    "status.title_attributed": "The headline must show this is one outlet's report (for example start with "
                               "«گزارش:» or name the outlet); do not present it as established fact.",
    "persian.untranslated_english": "Translate leftover English sentences into Persian.",
    "persian.no_cliche_lead": "Rewrite the lead without clichés; state the news directly.",
    "persian.no_repetition": "Remove repeated sentences.",
    "persian.no_near_duplicates": "State each point once; delete sentences that repeat an earlier one: ",
    "links.no_pointer_sentences": "Delete sentences that only point readers to other pages or earlier coverage "
                                  "(and never mention Poormaz); a link may only wrap a name inside a factual "
                                  "sentence: ",
    "style.no_meta_filler": "Delete comments about the available information or the article itself; state "
                            "what is known, and put a genuinely open question once in uncertainties_fa: ",
    "links.anchor_grounded": "Put [[Lx|...]] only around a game, product or company name that already appears "
                             "in the sentence and in that candidate's page_title, or use no link. ",
    "persian.fresh_opening": "Use a different opening sentence than recent Poormaz articles.",
    "persian.script_ratio": "Write in Persian; keep only names in English.",
    "seo.title_length": "Write a clear headline of 30 to 80 characters.",
    "seo.title_no_clickbait": "Remove exclamation marks and hype from the title.",
    "seo.no_keyword_stuffing": "Use the focus keyword naturally, not repeatedly.",
    "factcheck.model_issues": "Fix every one of these fact-check findings (rewrite or delete the sentence; "
                              "never keep an unsupported statement): ",
    "factcheck.model_verdict": "The fact checker asked for a revision. Fix every one of these findings "
                               "(rewrite or delete the sentence): ",
    "language.fluent": "Rewrite in natural, fluent Persian newsroom prose (not translationese): ",
}


def revision_feedback(failed: list[CheckResult]) -> list[str]:
    out: list[str] = []
    names = {c.name for c in failed}
    for check in failed:
        if check.name == "factcheck.model_issues" and "factcheck.model_verdict" in names:
            continue  # the verdict line already lists every finding
        hint = FIXABLE.get(check.name)
        if hint:
            line = hint + (check.detail if hint.endswith(": ") else f" ({check.detail})" if check.detail else "")
            if line not in out:
                out.append(line)
    return out


def revisable(failed: list[CheckResult]) -> bool:
    """True when every failure can be addressed by rewriting (none is a final rejection)."""
    return bool(failed) and not any(c.name in NO_REVISION_CHECKS for c in failed) and bool(revision_feedback(failed))
