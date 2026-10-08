"""Areas 8-9: Persian article validation, unsupported and fabricated claims."""

import copy

import pytest
from conftest import IRONVALE_ARTICLE, OFFICIAL_PAGE, make_settings

from newsbot.compose import parse_article, render_article, strip_placeholders
from newsbot.factcheck import revision_feedback, rule_checks
from newsbot.facts import parse_fact_sheet, verify_fact_sheet
from newsbot.htmlextract import extract_page
from newsbot.models import LinkCandidate, SourceDoc, Story

PLATFORMS = {"PC", "PlayStation 5", "Xbox Series X|S", "Nintendo Switch 2"}


@pytest.fixture
def ctx(repo):
    settings = make_settings(repo)
    official = extract_page(OFFICIAL_PAGE, "https://northlightforge.com/news")
    doc = SourceDoc("S1", "https://northlightforge.com/news", "Northlight Forge", "official", "high", official.title,
                    official.text + " Ironvale Chronicles was first announced in 2024.", origin_group=1)
    pcg = SourceDoc("S2", "https://pcgamer.example/ironvale", "PC Gamer", "publication", "high", "Ironvale dated",
                    "Ironvale Chronicles finally has a release date. Reportedly, a Nintendo Switch 2 port could follow "
                    "later, but the studio has not commented on it.", origin_group=2)
    docs = [doc, pcg]

    def claim(cid, text, value, status, importance, quote, category="other"):
        return {"id": cid, "text_en": text, "category": category, "subject": "Ironvale Chronicles", "value": value,
                "status": status, "confidence": "high", "importance": importance,
                "support": [{"source_id": d.doc_id, "quote": quote} for d in docs if quote in d.text]}

    sheet = parse_fact_sheet({
        "headline_en": "Ironvale Chronicles launches March 19, 2027", "event_type": "release_date", "kind": "news",
        "entities": {"games": ["Ironvale Chronicles"], "companies": ["Northlight Forge"], "platforms": [],
                     "products": []},
        "claims": [
            claim("C1", "Launch on March 19, 2027", "March 19, 2027", "confirmed", "core",
                  "Ironvale Chronicles will launch on March 19, 2027", "release_date"),
            claim("C2", "Platforms", "PC, PlayStation 5, Xbox Series X|S", "confirmed", "core",
                  "for PC, PlayStation 5 and Xbox Series X|S", "platform"),
            claim("C3", "Price $59.99", "$59.99", "confirmed", "supporting", "The standard edition is priced at $59.99",
                  "price"),
            claim("C4", "Trailer shows Ashmere", "", "confirmed", "supporting", "showcasing the city of Ashmere"),
            claim("C5", "Announced in 2024", "2024", "reported", "background",
                  "Ironvale Chronicles was first announced in 2024"),
            claim("C6", "Switch 2 port rumored", "", "speculative", "supporting",
                  "a Nintendo Switch 2 port could follow later"),
        ],
        "contradictions": [], "sources": [], "newsworthiness": {"is_news": True, "significance": "high"},
        "open_questions": [], "injection_detected": False})
    verify_fact_sheet(sheet, docs)
    story = Story("abc123", [], primary_entity="ironvale chronicles", primary_display="Ironvale Chronicles")
    links = [LinkCandidate("L1", "https://poormaz.test/ironvale-chronicles-persian-subtitles/", "loc", "localization")]

    def check(data, recent_openings=None, max_words=570):
        article = render_article(parse_article(data), story, sheet, docs, links, settings)
        return article, {c.name: c for c in rule_checks(article, story, sheet, docs, settings, 130, max_words,
                                                        recent_openings or [], PLATFORMS)}
    return check


def failed(results):
    return sorted(name for name, c in results.items() if c.blocking and not c.passed)


def test_reference_article_passes(ctx):
    article, results = ctx(IRONVALE_ARTICLE)
    assert failed(results) == []
    assert article.word_count >= 130
    assert article.links_used and article.links_used[0]["anchor"] == "زیرنویس فارسی این بازی"


def variant(**changes):
    data = copy.deepcopy(IRONVALE_ARTICLE)
    for key, value in changes.items():
        data[key] = value
    return data


def with_paragraph(text, claim_ids=("C1",)):
    data = copy.deepcopy(IRONVALE_ARTICLE)
    data["sections"][0]["paragraphs"].append({"text": text, "claim_ids": list(claim_ids)})
    return data


def test_invented_number_is_caught(ctx):
    _, results = ctx(with_paragraph("طبق اعلام سازنده، بازی بیش از ۱۲۰ ساعت محتوای داستانی دارد."))
    assert "facts.numbers_supported" in failed(results)
    assert "120" in results["facts.numbers_supported"].detail


def test_altered_date_is_caught(ctx):
    data = variant(lead={"text": "Ironvale Chronicles روز ۲۶ مارس ۲۰۲۷ برای PC منتشر می‌شود.", "claim_ids": ["C1"]})
    assert "facts.numbers_supported" in failed(ctx(data)[1])


def test_invented_name_is_caught(ctx):
    _, results = ctx(with_paragraph("این بازی توسط Bandai Namco توزیع خواهد شد و نسخه Ironvale Online هم دارد."))
    assert "facts.names_supported" in failed(results)


@pytest.mark.parametrize("text", [
    "ما در پورماز بازی را تست کردیم و از اجرای روان آن راضی بودیم.",
    "در تست‌های ما بازی روی PC با نرخ فریم بالا اجرا شد.",
    "به‌صورت اختصاصی مطلع شدیم که نسخه دیگری هم در راه است.",
    "مدیر استودیو در مصاحبه با پورماز درباره این بازی صحبت کرد.",
])
def test_fabricated_first_hand_claims(ctx, text):
    assert "fabrication.first_hand" in failed(ctx(with_paragraph(text))[1])


def test_unverified_quotation_is_caught(ctx):
    text = "کارگردان بازی گفت: «ما می‌خواهیم بزرگ‌ترین بازی نقش‌آفرینی سال را بسازیم و همه را شگفت‌زده کنیم»."
    assert "fabrication.quotes" in failed(ctx(with_paragraph(text))[1])


def test_citing_unverified_or_unknown_claims(ctx):
    _, results = ctx(with_paragraph("متن تازه درباره بازی و پلتفرم‌های آن.", claim_ids=["C9"]))
    assert "citations.verified_only" in failed(results)
    _, results = ctx(with_paragraph("یک پاراگراف بدون ارجاع به هیچ ادعایی درباره بازی.", claim_ids=[]))
    data = copy.deepcopy(IRONVALE_ARTICLE)
    for para in data["sections"][0]["paragraphs"]:
        para["claim_ids"] = []
    assert "citations.every_paragraph" in failed(ctx(data)[1])


def test_rumor_must_be_framed(ctx):
    unframed = {
        **copy.deepcopy(IRONVALE_ARTICLE),
        "uncertainties_fa": [],
        "sections": [{"heading_fa": "", "paragraphs": [
            {"text": "پیش‌فروش بازی آغاز شده و قیمت نسخه استاندارد ۵۹٫۹۹ دلار است.", "claim_ids": ["C3"]},
            {"text": "نسخه Nintendo Switch 2 هم بعدها منتشر می‌شود.", "claim_ids": ["C6"]},
            {"text": "Ironvale Chronicles نخستین بار در سال ۲۰۲۴ معرفی شد و تریلر تازه آن شهر Ashmere را نشان "
                     "می‌دهد؛ بازی برای هر سه پلتفرم اعلام‌شده عرضه خواهد شد و پیش‌فروش آن آغاز شده است.",
             "claim_ids": ["C5", "C4", "C2"]}]}],
    }
    article, results = ctx(unframed)
    assert "گزارش" not in article.plain_text and "شایعه" not in article.plain_text
    assert "status.rumor_framed_in_text" in failed(results)
    framed = copy.deepcopy(unframed)
    framed["sections"][0]["paragraphs"][1]["text"] = ("به گزارش PC Gamer، شایعه شده نسخه Nintendo Switch 2 هم "
                                                      "بعدها منتشر شود، اما سازنده آن را تأیید نکرده است.")
    assert "status.rumor_framed_in_text" not in failed(ctx(framed)[1])


def test_untranslated_english_and_ratio(ctx):
    text = "This is an untranslated English sentence that slipped into the Persian article body."
    assert "persian.untranslated_english" in failed(ctx(with_paragraph(text))[1])


def test_cliche_lead_and_repetition(ctx):
    data = variant(lead={"text": "در دنیای بازی‌های ویدیویی خبر تازه‌ای رسید: Ironvale Chronicles روز ۱۹ مارس ۲۰۲۷ "
                                 "منتشر می‌شود.", "claim_ids": ["C1"]})
    assert "persian.no_cliche_lead" in failed(ctx(data)[1])
    repeated = IRONVALE_ARTICLE["sections"][0]["paragraphs"][0]["text"]
    assert "persian.no_repetition" in failed(ctx(with_paragraph(repeated, ["C3"]))[1])


def test_repetitive_opening_across_articles(ctx):
    opening = IRONVALE_ARTICLE["lead"]["text"]
    assert "persian.fresh_opening" in failed(ctx(IRONVALE_ARTICLE, recent_openings=[opening])[1])


def test_padding_is_rejected(ctx):
    article, results = ctx(IRONVALE_ARTICLE, max_words=120)
    assert article.word_count > 138
    assert "length.no_padding" in failed(results)


def test_too_short_is_rejected(ctx):
    data = variant(sections=[{"heading_fa": "", "paragraphs": [
        {"text": "پیش‌فروش بازی آغاز شده است.", "claim_ids": ["C3"]}]}], uncertainties_fa=[])
    assert "length.minimum" in failed(ctx(data)[1])


def test_keyword_stuffing(ctx):
    kw = IRONVALE_ARTICLE["focus_keyword_fa"]
    data = with_paragraph(f"{kw} مهم است. {kw} اعلام شد. {kw} قطعی شد. {kw} را ببینید.", ["C1"])
    assert "seo.no_keyword_stuffing" in failed(ctx(data)[1])


def test_model_html_and_urls_are_neutralised(ctx):
    data = with_paragraph('<script>alert(1)</script> متن <a href="https://evil.example">لینک</a> '
                          'https://evil.example/x [[L9|لینک ساختگی]] درباره Ironvale Chronicles.', ["C1"])
    article, _ = ctx(data)
    assert "<script" not in article.html and "evil.example" not in article.html
    assert "L9" not in article.html


def test_revision_feedback_is_actionable(ctx):
    _, results = ctx(with_paragraph("بازی بیش از ۱۲۰ ساعت محتوا دارد.", ["C1"]))
    feedback = revision_feedback([c for c in results.values() if c.blocking and not c.passed])
    assert any("numbers" in f.lower() for f in feedback)


def test_strip_placeholders():
    assert strip_placeholders("متن [[L1|زیرنویس فارسی]] تمام") == "متن زیرنویس فارسی تمام"
