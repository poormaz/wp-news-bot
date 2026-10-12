"""Stages 6-7: structured fact extraction and cross-source verification.

The model proposes claims with verbatim quotes; this module then checks every quote
against the actual source text, checks that exact values (dates, prices, versions,
numbers) really appear in the cited sources, counts independent origins, applies
official-source precedence and detects contradictions. Claims that fail are kept in
the record (for the audit trail) but marked unusable for writing.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher

from .config import Settings
from .llm import LLMClient
from .models import Claim, FactSheet, SourceDoc, Story
from .prompts import EXTRACT_SYSTEM, FACTS_SCHEMA, PROMPT_VERSION, build_extract_input
from .research import ResearchResult
from .textutil import collapse_ws, entity_key, extract_numbers, fold_latin, headline_tokens, sha256

log = logging.getLogger("newsbot.facts")

HEDGE_RE = re.compile(r"\b(reportedly|rumou?red|rumou?rs?|leak(?:ed|s)?|allegedly|supposedly|could|may|might|"
                      r"expected to|is said to|according to (?:a |an )?(?:insider|source|leaker)s?|speculat\w*|"
                      r"datamine\w*|unconfirmed|possibly|likely)\b", re.I)
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
_MONTH_RE = re.compile(r"\b(" + "|".join(m[:3] for m in MONTHS) + r")[a-z]*\.?\b", re.I)
SLOT_CATEGORIES = {"release_date", "release_window", "price", "version", "delay"}


def _norm(text: str) -> str:
    text = fold_latin(text or "").casefold()
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[‐-―]", "-", text)
    return collapse_ws(text)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:[.,][0-9]+)*", _norm(text))


def quote_in_text(quote: str, doc_text: str, doc_tokens: list[str] | None = None) -> bool:
    quote_n, text_n = _norm(quote), _norm(doc_text)
    if len(quote_n) < 8:
        return False
    if quote_n in text_n:
        return True
    q_tokens = _tokens(quote)
    if len(q_tokens) < 4:
        return False
    d_tokens = doc_tokens if doc_tokens is not None else _tokens(doc_text)
    matcher = SequenceMatcher(None, d_tokens, q_tokens, autojunk=False)
    blocks = matcher.get_matching_blocks()
    matched_positions = {block.b + k for block in blocks for k in range(block.size)}
    # Numbers are never fuzzy: an altered date, price or version must not pass as "close enough".
    if any(any(ch.isdigit() for ch in tok) for i, tok in enumerate(q_tokens) if i not in matched_positions):
        return False
    matched = len(matched_positions)
    longest = max((block.size for block in blocks), default=0)
    return matched / len(q_tokens) >= 0.85 and longest >= min(6, len(q_tokens))


def value_in_text(value: str, text: str) -> bool:
    """True when the exact key value of a claim is present in the source text."""
    value = collapse_ws(value)
    if not value:
        return True
    text_n = _norm(text)
    numbers = extract_numbers(value)
    if numbers:
        text_numbers = set(extract_numbers(text_n))
        for number in numbers:
            if number in text_numbers:
                continue
            # 69.99 vs 69,99 or 1,000 vs 1000
            alt = number.replace(".", ",") if "." in number else number.replace(",", "")
            if alt not in text_numbers and number not in text_n:
                return False
    for month in _MONTH_RE.findall(value):
        if month.lower()[:3] not in {m[:3] for m in _MONTH_RE.findall(text_n)}:
            return False
    if numbers or _MONTH_RE.search(value):
        return True
    words = [w for w in re.findall(r"[a-z0-9]+", _norm(value)) if len(w) > 2]
    if not words:
        return True
    present = sum(1 for w in words if w in text_n)
    return present / len(words) >= 0.5


def parse_fact_sheet(raw: dict) -> FactSheet:
    claims: list[Claim] = []
    id_map: dict[str, str] = {}
    for index, row in enumerate(raw.get("claims") or [], start=1):
        new_id = f"C{index}"
        id_map[str(row.get("id") or new_id)] = new_id
        support = []
        for sup in row.get("support") or []:
            if isinstance(sup, dict) and sup.get("source_id") and sup.get("quote"):
                support.append({"source_id": str(sup["source_id"]).strip(), "quote": str(sup["quote"])[:600]})
        claims.append(Claim(
            id=new_id, text_en=collapse_ws(str(row.get("text_en", ""))), category=str(row.get("category", "other")),
            subject=collapse_ws(str(row.get("subject", ""))), value=collapse_ws(str(row.get("value", ""))),
            status=str(row.get("status", "reported")), confidence=str(row.get("confidence", "medium")),
            importance=str(row.get("importance", "supporting")), support=support,
        ))
    contradictions = []
    for row in raw.get("contradictions") or []:
        ids = [id_map.get(str(i), str(i)) for i in row.get("claim_ids") or []]
        contradictions.append({"topic": collapse_ws(str(row.get("topic", ""))), "claim_ids": ids,
                               "explanation": collapse_ws(str(row.get("explanation", ""))), "resolved": False,
                               "origin": "model"})
    entities = raw.get("entities") or {}
    return FactSheet(
        headline_en=collapse_ws(str(raw.get("headline_en", ""))),
        event_type=str(raw.get("event_type", "other")),
        kind=str(raw.get("kind", "news")),
        entities={k: [collapse_ws(str(v)) for v in (entities.get(k) or []) if str(v).strip()]
                  for k in ("games", "companies", "platforms", "products")},
        claims=claims,
        contradictions=contradictions,
        sources_meta=[dict(s) for s in raw.get("sources") or [] if isinstance(s, dict)],
        newsworthiness=dict(raw.get("newsworthiness") or {}),
        open_questions=[collapse_ws(str(q)) for q in raw.get("open_questions") or [] if str(q).strip()],
        injection_detected=bool(raw.get("injection_detected")),
    )


# Words that do not say what a claim is about ("the Steam listing lists ...").
_SUPPORT_STOP = {"steam", "listing", "list", "listed", "lists", "store", "page", "official", "game", "gam",
                 "name", "names", "named", "state", "stat", "says", "will", "now", "has", "ha"}
# Claims whose value IS the fact: a listing line "Platforms: Windows" or "Developer: X" states it.
_VALUE_IS_THE_FACT = {"platform", "business", "people", "price", "version", "release_date", "release_window"}


def name_words(names) -> set[str]:
    """Headline tokens of entity names (games, companies, platforms, products)."""
    out: set[str] = set()
    for name in names or []:
        out |= set(headline_tokens(str(name)))
    return out


def quote_supports_claim(claim: Claim, quote: str, names: set[str] | None = None) -> bool:
    """Does the quoted text itself state this claim?

    A specific value (date, price, version, number) found in the quote is enough. Otherwise -
    no value, or only a name such as "Steam" - the quote must also share what the claim says
    happened ("wishlist", "trailer"); names of the game or its developer do not count, so a
    listing line that merely names them confirms nothing else.
    """
    if claim.value and not value_in_text(claim.value, quote):
        return False
    if claim.value and (extract_numbers(claim.value) or claim.category in _VALUE_IS_THE_FACT):
        return True
    wanted = set(headline_tokens(claim.text_en)) - _SUPPORT_STOP - set(headline_tokens(claim.subject)) - (names or set())
    if not wanted:
        return bool(claim.value)
    found = wanted & set(headline_tokens(quote))
    # With its (textual) value in the quote, one shared word suffices; without a value, two are needed.
    return len(found) >= (1 if claim.value else min(2, len(wanted)))


def verify_fact_sheet(sheet: FactSheet, docs: list[SourceDoc]) -> FactSheet:
    by_id = {d.doc_id: d for d in docs}
    texts = {d.doc_id: f"{d.title}\n{d.text}" for d in docs}
    tokens = {k: _tokens(v) for k, v in texts.items()}
    _apply_origin_hints(sheet, docs)
    names = name_words(v for values in (sheet.entities or {}).values() for v in (values or []))

    for claim in sheet.claims:
        verified: list[str] = []
        quote_of: dict[str, str] = {}
        for sup in claim.support:
            sid = sup["source_id"]
            if sid not in by_id:
                claim.problems.append(f"cites unknown source {sid}")
                continue
            if not quote_in_text(sup["quote"], texts[sid], tokens[sid]):
                claim.problems.append(f"quote not found in {sid}")
                continue
            if by_id[sid].is_official and not quote_supports_claim(claim, sup["quote"], names):
                # An official page (e.g. a Steam listing) confirms only what its quoted text states: a quote
                # that merely names the game cannot make an unrelated claim "official".
                claim.problems.append(
                    f"value {claim.value!r} not present in the quote from official {sid}"
                    if claim.value and not value_in_text(claim.value, sup["quote"])
                    else f"quote from official {sid} does not state this claim")
                continue
            if sid not in verified:
                verified.append(sid)
                quote_of[sid] = sup["quote"]
        if claim.value and verified:
            kept = [sid for sid in verified if value_in_text(claim.value, texts[sid])]
            if not kept:
                claim.problems.append(f"value {claim.value!r} not present in cited sources")
            verified = kept
        quotes = [quote_of[sid] for sid in verified]
        claim.verified_sources = verified
        groups = {by_id[sid].origin_group for sid in verified}
        claim.independent_groups = len(groups)
        if not verified:
            claim.verification = "unverified"
        elif any(by_id[sid].is_official for sid in verified):
            claim.verification = "official"
        elif len(groups) >= 2:
            claim.verification = "corroborated"
        else:
            claim.verification = "single_source"
        if claim.status == "confirmed" and claim.verification != "official" and any(HEDGE_RE.search(q) for q in quotes):
            claim.status = "reported"
            claim.problems.append("status downgraded: source wording is hedged")
        if claim.status == "confirmed" and claim.verification not in ("official",) and \
                not any(re.search(r"\b(announc|confirm|official|revealed|press release|said|says|stated|told)",
                                  q, re.I) for q in quotes):
            # A publication stating something without attribution is its reporting, not confirmation.
            claim.status = "reported"
        official_quotes = [quote_of[sid] for sid in verified if by_id[sid].is_official]
        if claim.status == "reported" and official_quotes and not any(HEDGE_RE.search(q) for q in official_quotes):
            # The developer's/platform's own page states it verbatim: confirmed, not merely reported.
            claim.status = "confirmed"
            claim.problems.append("status upgraded: stated verbatim by an official source")
    _detect_value_conflicts(sheet, by_id)
    unverified = sum(1 for c in sheet.claims if not c.usable)
    if unverified:
        sheet.verification_notes.append(f"{unverified} claim(s) failed source verification and will not be used")
    return sheet


def _apply_origin_hints(sheet: FactSheet, docs: list[SourceDoc]) -> None:
    """Group sources that the model says are based on the same origin (same press release / same leak)."""
    by_id = {d.doc_id: d for d in docs}
    origin_of: dict[str, str] = {}
    for meta in sheet.sources_meta:
        sid = str(meta.get("source_id", ""))
        origin = entity_key(str(meta.get("origin", "")))
        if sid in by_id and origin and "own reporting" not in origin:
            origin_of[sid] = origin
    by_origin: dict[str, list[str]] = {}
    for sid, origin in origin_of.items():
        key = re.sub(r"\b(official|the|a|an|post|announcement|statement)\b", "", origin).strip()
        if key:
            by_origin.setdefault(key, []).append(sid)
    for sids in by_origin.values():
        if len(sids) > 1:
            target = min(by_id[s].origin_group for s in sids)
            for s in sids:
                by_id[s].origin_group = target
    for meta in sheet.sources_meta:
        sid = str(meta.get("source_id", ""))
        if meta.get("repeats_press_release") and sid in by_id:
            officials = [d for d in docs if d.is_official]
            if officials:
                by_id[sid].origin_group = officials[0].origin_group


def _detect_value_conflicts(sheet: FactSheet, by_id: dict[str, SourceDoc]) -> None:
    slots: dict[tuple[str, str], list[Claim]] = {}
    for claim in sheet.claims:
        if claim.usable and claim.category in SLOT_CATEGORIES and claim.value:
            subject = entity_key(claim.subject) or "story"
            slots.setdefault((claim.category, subject), []).append(claim)
    for (category, subject), claims in slots.items():
        values = {tuple(sorted(extract_numbers(c.value))) or (_norm(c.value),) for c in claims}
        if len(values) < 2:
            continue
        official = [c for c in claims if c.verification == "official"]
        if official:
            official_values = {tuple(sorted(extract_numbers(c.value))) or (_norm(c.value),) for c in official}
            for claim in claims:
                key = tuple(sorted(extract_numbers(claim.value))) or (_norm(claim.value),)
                if claim.verification != "official" and key not in official_values:
                    claim.verification = "unverified"
                    claim.problems.append("contradicted by an official source")
            sheet.contradictions.append({"topic": f"{category} of {subject}", "claim_ids": [c.id for c in claims],
                                         "explanation": "official source takes precedence", "resolved": True,
                                         "origin": "rules"})
        else:
            sheet.contradictions.append({"topic": f"{category} of {subject}", "claim_ids": [c.id for c in claims],
                                         "explanation": "sources give different values", "resolved": False,
                                         "origin": "rules"})


def fact_cache_key(docs: list[SourceDoc], model: str) -> str:
    material = "|".join(sorted(sha256(d.url + "\n" + d.text) for d in docs))
    return sha256(f"{PROMPT_VERSION}|{model}|{material}")


class FactDesk:
    def __init__(self, settings: Settings, llm: LLMClient, store=None):
        self.settings = settings
        self.llm = llm
        self.store = store

    def extract(self, story: Story, research: ResearchResult) -> FactSheet:
        docs = research.usable_docs
        key = fact_cache_key(docs, self.llm.model)
        raw = self.store.get_facts(key) if self.store is not None else None
        from_cache = raw is not None
        if raw is None:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            raw = self.llm.call_json("extract", EXTRACT_SYSTEM, build_extract_input(story.headline, docs, today),
                                     "fact_sheet", FACTS_SCHEMA, self.settings.max_output_tokens_extract,
                                     self.settings.reasoning_effort)
            if self.store is not None:
                self.store.put_facts(key, story.story_id, self.llm.model, PROMPT_VERSION, raw)
        sheet = verify_fact_sheet(parse_fact_sheet(raw), docs)
        sheet.from_cache = from_cache
        log.info("FACTS %s: %d claim(s), %d usable, %d contradiction(s)%s", story.story_id, len(sheet.claims),
                 len(sheet.usable_claims), len(sheet.contradictions), " (cached)" if from_cache else "")
        return sheet


def compose_view(sheet: FactSheet, docs: list[SourceDoc]) -> dict:
    """The verified, model-facing fact sheet: only usable claims, with provenance labels."""
    by_id = {d.doc_id: d for d in docs}
    claims = []
    for claim in sheet.usable_claims:
        claims.append({
            "id": claim.id,
            "text": claim.text_en,
            "category": claim.category,
            "value": claim.value,
            "status": claim.status,
            "importance": claim.importance,
            "verification": claim.verification,
            "sources": sorted({by_id[s].outlet for s in claim.verified_sources if s in by_id}),
        })
    usable_ids = {c["id"] for c in claims}
    contradictions = [c for c in sheet.contradictions
                      if not c.get("resolved") and any(i in usable_ids for i in c.get("claim_ids", []))]
    return {
        "headline_en": sheet.headline_en,
        "event_type": sheet.event_type,
        "story_status": story_status(sheet),
        "claims": claims,
        "contradictions": contradictions,
        "open_questions": sheet.open_questions[:5],
        "sources": [{"id": d.doc_id, "outlet": d.outlet, "type": d.source_type} for d in docs if d.usable],
    }


def story_status(sheet: FactSheet) -> str:
    core = sheet.core_claims or sheet.usable_claims
    if not core:
        return "unverified"
    statuses = {c.status for c in core}
    if statuses == {"confirmed"}:
        return "confirmed"
    if "speculative" in statuses and "confirmed" not in statuses:
        return "rumor"
    if statuses <= {"confirmed", "reported"}:
        return "reported"
    return "mixed"
