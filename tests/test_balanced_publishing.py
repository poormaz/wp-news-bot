"""News Bot 2.1 - balanced editorial publishing.

Regressions from the live dry-run 38272781935 (tests/fixtures/dryrun_38272781935.json): 51 eligible stories,
only 3 researched, all rejected - two of them solely because one trusted medium-authority outlet reported them,
one (Crimson Desert) because its column format was labelled "opinion", and the Witcher 3 technical interview
was dropped by triage for its format.
"""

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

from conftest import NOW, FakeOpenAI, FakeWeb, build, docs_in, make_settings, rss

from newsbot.facts import parse_fact_sheet, verify_fact_sheet
from newsbot.models import CheckResult, FeedItem, SourceDoc, Story
from newsbot.newsworthiness import apply_triage, assess
from newsbot.prompts import EXTRACT_SYSTEM, TRIAGE_SYSTEM
from newsbot.quality import decide, policy_version, source_checks, story_risk
from newsbot.research import ResearchResult

DRYRUN = json.loads((Path(__file__).parent / "fixtures" / "dryrun_38272781935.json").read_text(encoding="utf-8"))

FEED = "https://medium-outlet.example/feed"


def use_sources(repo, name="Gamingbolt", authority="medium"):
    (repo / "sources.yaml").write_text(
        f"sources:\n  - {{name: {name}, feed: '{FEED}', authority: {authority}}}\n", encoding="utf-8")


def para(*texts):
    return "".join(f"<p>{t}</p>" for t in texts)


def sheet_claim(cid, text, quote, docs, value="", status="reported", importance="supporting", category="other"):
    return {"id": cid, "text_en": text, "category": category, "subject": "", "value": value, "status": status,
            "confidence": "high", "importance": importance,
            "support": [{"source_id": sid, "quote": quote} for sid, body in docs.items() if quote in body]}


def fact_sheet(user_input, claims_spec, kind="news", entities=None, is_news=True, event="patch_update"):
    docs = docs_in(user_input)
    return {"headline_en": "h", "event_type": event, "kind": kind,
            "entities": entities or {"games": [], "companies": [], "platforms": [], "products": []},
            "claims": [sheet_claim(*spec[:3], docs, **(spec[3] if len(spec) > 3 else {})) for spec in claims_spec],
            "contradictions": [], "sources": [{"source_id": sid, "origin": "own reporting",
                                               "repeats_press_release": False, "is_primary_source": False}
                                              for sid in docs],
            "newsworthiness": {"is_news": is_news, "significance": "medium", "reader_value": "useful",
                               "reasons": ["new development"]},
            "open_questions": [], "injection_detected": False}


def article(title, lead, paragraphs, tags):
    return {"title_fa": title, "meta_title_fa": title[:60],
            "meta_description_fa": (lead + " " + paragraphs[0][0])[:150], "focus_keyword_fa": tags[0],
            "slug_en": "news-story", "lead": {"text": lead, "claim_ids": ["C1"]},
            "sections": [{"heading_fa": "", "paragraphs": [{"text": t, "claim_ids": ids} for t, ids in paragraphs]}],
            "uncertainties_fa": [], "entity_tags": tags, "content_type": "gaming",
            "coverage": {"what_happened": True, "why_it_matters": False, "who_is_affected": True,
                         "background": False, "uncertainty": False}}


# ---------------------------------------------------------------------------
# 1. A DSOGaming single-source PC-port story (live: rejected as "only DSOGaming, authority 2 < 3")
# ---------------------------------------------------------------------------
SPLINTER_TITLE = DRYRUN["candidates"][2]["headline"]
SPLINTER_TEXT = para(
    "A modder has released an unofficial native PC port of the Xbox version of Splinter Cell: Double Agent. The "
    "port is based on the original Xbox 360 release and runs natively on Windows without an emulator.",
    "According to the release notes, the port supports resolutions up to 4K and unlocked frame rates, and it "
    "adds support for modern gamepads alongside keyboard and mouse controls.",
    "Players need their own copy of the Xbox 360 game files to use the port. The project is available for free "
    "on GitHub, and the modder says some cutscenes may still have audio sync issues that will be addressed in "
    "later builds. The port does not include any of the original game's copyrighted assets, which have to be "
    "extracted from the player's own disc or digital copy before the first launch.")
SPLINTER_CLAIMS = [
    ("C1", "An unofficial native PC port of the Xbox version of Splinter Cell: Double Agent has been released.",
     "has released an unofficial native PC port of the Xbox version of Splinter Cell: Double Agent",
     {"importance": "core", "category": "availability"}),
    ("C2", "The port runs natively on Windows without an emulator.", "runs natively on Windows without an emulator"),
    ("C3", "The port supports resolutions up to 4K and unlocked frame rates.",
     "the port supports resolutions up to 4K and unlocked frame rates", {"value": "4K", "category": "spec"}),
    ("C4", "Players need their own copy of the Xbox 360 game files.",
     "Players need their own copy of the Xbox 360 game files"),
    ("C5", "The project is available for free on GitHub.", "The project is available for free on GitHub"),
]
SPLINTER_ARTICLE = article(
    "گزارش: پورت غیررسمی و بومی Splinter Cell: Double Agent برای PC منتشر شد",
    "به گزارش DSOGaming، یک پورت غیررسمی و بومی از نسخه Xbox بازی Splinter Cell: Double Agent برای PC منتشر شده "
    "است.",
    [("این پورت بدون نیاز به شبیه‌ساز و به‌صورت بومی روی Windows اجرا می‌شود. طبق یادداشت‌های انتشار، پورت از وضوح "
      "تصویر تا 4K و نرخ فریم آزاد پشتیبانی می‌کند.", ["C2", "C3"]),
     ("بازیکنان برای استفاده از این پورت باید فایل‌های نسخه Xbox 360 بازی را که خودشان در اختیار دارند فراهم کنند. "
      "پروژه به‌صورت رایگان در GitHub در دسترس است.", ["C4", "C5"])],
    ["Splinter Cell: Double Agent"])


def splinter_web():
    web = FakeWeb()
    web.add(FEED, rss([{"title": SPLINTER_TITLE, "link": "https://dsogaming.example/splinter-cell-port",
                        "content": SPLINTER_TEXT, "published": NOW - timedelta(hours=2)}]))
    web.add("https://dsogaming.example/splinter-cell-port", f"<html><body><article>{SPLINTER_TEXT}</article></body></html>")
    return web


def splinter_ai(article_data=None):
    return FakeOpenAI({
        "fact_sheet": lambda kw: fact_sheet(kw["input"], SPLINTER_CLAIMS, event="port_platform",
                                            entities={"games": ["Splinter Cell: Double Agent"], "companies": [],
                                                      "platforms": ["Xbox 360", "Windows", "PC"], "products": []}),
        "article": lambda kw: article_data or SPLINTER_ARTICLE})


def test_dsogaming_single_source_pc_port_is_published_with_attribution(repo):
    use_sources(repo, "DSOGaming")
    pipeline, *_ = build(repo, "dry-run", web=splinter_web(), openai_client=splinter_ai())
    report = pipeline.run()
    story = next(s for s in report.stories if s["headline"] == SPLINTER_TITLE)
    assert story["decision"] == "dry_run_pass", story["reasons"]
    checks = {c["name"]: c for c in story["gate"]["checks"]}
    assert checks["sources.reliability"]["passed"]
    assert "single source with sufficient authority (DSOGaming)" in checks["sources.reliability"]["detail"]
    assert checks["sources.risk"]["detail"].startswith("ordinary news")
    assert checks["status.reported_attributed"]["passed"] and checks["status.title_attributed"]["passed"]
    assert checks["factcheck.model_verdict"]["passed"]                       # the fact checker still decides


def test_single_source_story_must_be_attributed(repo):
    use_sources(repo, "DSOGaming")
    unattributed = json.loads(json.dumps(SPLINTER_ARTICLE))
    unattributed["title_fa"] = "پورت غیررسمی و بومی Splinter Cell: Double Agent برای PC منتشر شد"
    unattributed["lead"]["text"] = "یک پورت غیررسمی و بومی از نسخه Xbox بازی Splinter Cell: Double Agent برای PC منتشر شده است."
    pipeline, *_ = build(repo, "dry-run", web=splinter_web(), openai_client=splinter_ai(unattributed))
    story = next(s for s in pipeline.run().stories if s["headline"] == SPLINTER_TITLE)
    assert story["decision"] == "rejected"                                   # revisions kept the same text
    assert any(r.startswith("status.title_attributed") for r in story["reasons"])
    assert any(r.startswith("status.reported_attributed") for r in story["reasons"])


# ---------------------------------------------------------------------------
# 2. Medium-authority trusted reporting vs. high-risk stories and contradictions
# ---------------------------------------------------------------------------
def _story(title, authority="medium"):
    item = FeedItem("i", "Gamingbolt", "publication", authority, "primary", "u", "u", title, "",
                    NOW - timedelta(hours=2))
    return Story("s", [item], primary_entity="duskwind tactics", primary_display="Duskwind Tactics")


DOC_TEXT = ("Emberforge has released patch 1.2 for Duskwind Tactics, adding a two-player co-op mode. The update "
            "also adds a new difficulty level called Ironclad and fixes crashes when loading saved games. "
            "Reportedly, a PlayStation 5 version could follow later, but the studio has not commented on it.")


def _sheet(docs, statuses=("reported", "reported", "reported"), contradictions=()):
    quotes = ["has released patch 1.2 for Duskwind Tactics", "adding a two-player co-op mode",
              "adds a new difficulty level called Ironclad"]
    claims = [{"id": f"C{i}", "text_en": f"claim {i}", "category": "other", "subject": "Duskwind Tactics",
               "value": "", "status": st, "confidence": "high", "importance": "core" if i < 3 else "supporting",
               "support": [{"source_id": d.doc_id, "quote": q} for d in docs]}
              for i, (st, q) in enumerate(zip(statuses, quotes, strict=True), start=1)]
    return verify_fact_sheet(parse_fact_sheet(
        {"headline_en": "h", "event_type": "patch_update", "kind": "news", "entities": {}, "claims": claims,
         "contradictions": list(contradictions), "sources": [], "newsworthiness": {"is_news": True},
         "open_questions": [], "injection_detected": False}), docs)


def _gate(repo, sheet, docs, story, **settings):
    return decide(source_checks(story, sheet, ResearchResult(docs=docs), make_settings(repo, **settings)))


def test_medium_authority_trusted_reporting_passes_for_ordinary_news(repo):
    docs = [SourceDoc("S1", "https://gb.example/a", "Gamingbolt", "publication", "medium", "t", DOC_TEXT,
                      origin_group=1)]
    decision = _gate(repo, _sheet(docs), docs, _story("Duskwind Tactics Patch 1.2 Adds Co-op Mode"))
    assert decision.passed, decision.reasons
    # Not a blanket promotion: a low-authority outlet alone is still not enough.
    low = [SourceDoc("S1", "https://low.example/a", "Low", "publication", "low", "t", DOC_TEXT, origin_group=1)]
    assert not _gate(repo, _sheet(low), low, _story("Duskwind Tactics Patch 1.2 Adds Co-op Mode")).passed


def test_rumors_and_allegations_keep_strict_verification(repo):
    docs = [SourceDoc("S1", "https://gb.example/a", "Gamingbolt", "publication", "medium", "t", DOC_TEXT,
                      origin_group=1)]
    rumor = _sheet(docs, statuses=("speculative", "reported", "reported"))
    decision = _gate(repo, rumor, docs, _story("Duskwind Tactics Patch 1.2 Adds Co-op Mode"))
    assert not decision.passed
    assert any(r.startswith("policy.rumors") for r in decision.reasons)
    allegation = _story("Duskwind Tactics studio accused of crunch, lawsuit filed by former staff")
    assert story_risk(allegation, _sheet(docs), make_settings(repo))
    assert not _gate(repo, _sheet(docs), docs, allegation).passed


def test_factual_contradictions_block_single_source_and_core_conflicts(repo):
    docs = [SourceDoc("S1", "https://gb.example/a", "Gamingbolt", "publication", "medium", "t", DOC_TEXT,
                      origin_group=1)]
    side = [{"topic": "difficulty name", "claim_ids": ["C3"], "explanation": "two names reported"}]
    decision = _gate(repo, _sheet(docs, contradictions=side), docs, _story("Duskwind Tactics Patch 1.2"))
    assert not decision.passed and any("authority" in r for r in decision.reasons)   # conflicting reports = high risk
    core = [{"topic": "version", "claim_ids": ["C1"], "explanation": "1.2 vs 1.3"}]
    decision = _gate(repo, _sheet(docs, contradictions=core), docs, _story("Duskwind Tactics Patch 1.2"),
                     single_source_min_authority="medium")
    assert any(r.startswith("claims.no_unresolved_core_contradiction") for r in decision.reasons)


# ---------------------------------------------------------------------------
# 3. Backfilling: three rejected candidates, then a valid fourth
# ---------------------------------------------------------------------------
PATCH_STORIES = [  # (title, hours old) - fresher ranks higher, so the valid story is ranked fourth
    ("Ashfall Legion Gets Patch 2.1 With Performance Fixes", 1),
    ("Brightwater Saga Gets Patch 1.4 With New Quests", 2),
    ("Cinder Vale Gets Patch 3.0 With Raid Content", 3),
    ("Duskwind Tactics Gets Patch 1.2 With Co-op Mode", 4),
]
DUSKWIND_TEXT = para(
    "Developer Emberforge has released patch 1.2 for Duskwind Tactics, adding a two-player co-op mode to the "
    "campaign.",
    "The update also adds a new difficulty level called Ironclad and fixes several crashes that occurred when "
    "loading saved games.",
    "Patch 1.2 is available now on PC and will reach consoles next week, according to the patch notes. The "
    "studio thanked players for the feedback that shaped the update and said further balance changes are "
    "being tested on the public test branch before they ship to everyone in a later release.")
DUSKWIND_CLAIMS = [
    ("C1", "Emberforge released patch 1.2 for Duskwind Tactics.", "has released patch 1.2 for Duskwind Tactics",
     {"importance": "core", "value": "1.2", "category": "version"}),
    ("C2", "The patch adds a two-player co-op mode to the campaign.", "adding a two-player co-op mode to the campaign",
     {"importance": "core", "category": "content"}),
    ("C3", "The update adds a new difficulty level called Ironclad.", "adds a new difficulty level called Ironclad"),
    ("C4", "The patch fixes crashes when loading saved games.",
     "fixes several crashes that occurred when loading saved games"),
    ("C5", "Patch 1.2 is available now on PC and reaches consoles next week.",
     "Patch 1.2 is available now on PC and will reach consoles next week", {"value": "1.2"}),
]
DUSKWIND_ARTICLE = article(
    "گزارش: به‌روزرسانی 1.2 بازی Duskwind Tactics حالت همکاری دونفره اضافه کرد",
    "به گزارش Gamingbolt، استودیو Emberforge به‌روزرسانی 1.2 بازی Duskwind Tactics را منتشر کرده و حالت همکاری "
    "دونفره را به بخش داستانی آن افزوده است.",
    [("این به‌روزرسانی سطح دشواری تازه‌ای به نام Ironclad هم به بازی اضافه می‌کند و چند مشکل را که هنگام بارگذاری "
      "فایل‌های ذخیره باعث از کار افتادن بازی می‌شد برطرف کرده است.", ["C3", "C4"]),
     ("طبق یادداشت‌های به‌روزرسانی، نسخه 1.2 هم‌اکنون روی PC در دسترس است و هفته آینده به کنسول‌ها می‌رسد.", ["C5"])],
    ["Duskwind Tactics"])


def patch_web():
    web = FakeWeb()
    items = []
    for title, hours in PATCH_STORIES:
        slug = title.split()[0].lower()
        text = DUSKWIND_TEXT if slug == "duskwind" else para(f"{title}. " * 40)
        items.append({"title": title, "link": f"https://gb.example/{slug}", "content": text,
                      "published": NOW - timedelta(hours=hours)})
        web.add(f"https://gb.example/{slug}", f"<html><body><article>{text}</article></body></html>")
    web.add(FEED, rss(items))
    return web


def patch_ai():
    def sheet(kw):
        if "Duskwind" in kw["input"]:
            return fact_sheet(kw["input"], DUSKWIND_CLAIMS,
                              entities={"games": ["Duskwind Tactics"], "companies": ["Emberforge"],
                                        "platforms": ["PC"], "products": []})
        # The other three: the model's quotes are not in the article -> nothing verifies -> early rejection.
        return fact_sheet(kw["input"], [("C1", "x", "a quote that is not in the source", {"importance": "core"}),
                                        ("C2", "y", "another invented quote"), ("C3", "z", "third invented quote")])
    return FakeOpenAI({"fact_sheet": sheet, "article": lambda kw: DUSKWIND_ARTICLE})


def test_rejected_candidates_are_followed_by_a_valid_fourth(repo):
    use_sources(repo)
    pipeline, *_ = build(repo, "dry-run", web=patch_web(), openai_client=patch_ai())
    report = pipeline.run()
    decisions = [(s["headline"].split()[0], s["decision"]) for s in report.stories
                 if s["decision"] in ("rejected", "dry_run_pass")]
    assert decisions == [("Ashfall", "rejected"), ("Brightwater", "rejected"), ("Cinder", "rejected"),
                         ("Duskwind", "dry_run_pass")]
    assert report.counts["candidates"] == 4
    assert all("claims.enough_verified" in s["reasons"][0] for s in report.stories if s["decision"] == "rejected")


def test_candidate_limit_is_configurable_and_reported(repo):
    use_sources(repo)
    pipeline, *_ = build(repo, "dry-run", web=patch_web(), openai_client=patch_ai(), max_candidates_per_run=3)
    report = pipeline.run()
    assert not any(s["decision"] == "dry_run_pass" for s in report.stories)
    assert report.counts["candidates"] == 3 and report.counts["candidates_not_evaluated"] == 1
    assert any("backfill stopped: candidate limit reached (3)" in w for w in report.warnings)


def test_backfilling_stops_within_the_request_budget(repo):
    use_sources(repo)
    # Requests: triage 1 + extraction 1 per failed candidate; a new candidate needs 3 requests left.
    pipeline, _, _, ai = build(repo, "dry-run", web=patch_web(), openai_client=patch_ai(),
                               max_llm_requests_per_run=5)
    report = pipeline.run()
    assert report.counts["candidates"] == 2 and len(ai.calls) == 3
    assert any("backfill stopped: request budget too low" in w for w in report.warnings)
    assert not any(s["decision"] == "budget" for s in report.stories)      # nothing was cut off mid-story


def test_backfilling_stops_within_the_cost_budget(repo):
    use_sources(repo)
    # A candidate is only started while the run budget still covers the worst case of its first call
    # (the ledger's own pre-call check); each fake call really costs $0.00042.
    pipeline, _, _, ai = build(repo, "dry-run", web=patch_web(), openai_client=patch_ai(), max_run_cost_usd=0.0062)
    report = pipeline.run()
    assert 1 <= report.counts["candidates"] < 4
    assert any("backfill stopped: run budget too low" in w for w in report.warnings)
    assert not any(s["decision"] == "budget" for s in report.stories)
    assert report.llm["estimated_cost_usd"] <= 0.0062


def test_backfilling_respects_the_24h_budget(repo):
    use_sources(repo)
    pipeline, _, _, ai = build(repo, "dry-run", web=patch_web(), openai_client=patch_ai(), max_daily_cost_usd=0.001)
    report = pipeline.run()
    assert report.counts["candidates"] == 0 and ai.calls == []
    assert any("backfill stopped: 24h budget too low" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# 4. Interviews and technical features with real news; opinion stays out
# ---------------------------------------------------------------------------
WITCHER_LOG = DRYRUN["triage_rejections"][1]
# Stand-in headline: the dry-run log records only the story id and the triage reason for this item.
WITCHER_TITLE = "The Witcher 3 Interview: CD Projekt RED Engineers Detail Ray Tracing Performance Gains"
WITCHER_TEXT = para(
    "In an interview with Gamingbolt, CD Projekt RED engineers said the latest update for The Witcher 3 cuts "
    "the GPU cost of ray-traced global illumination by 20 percent on PC.",
    "The engineers said the update also adds a new performance mode for PlayStation 5 and Xbox Series X that "
    "targets 60 frames per second with ray tracing disabled.",
    "According to the team, the changes come from a rewritten lighting cache. The engineers added that further "
    "optimisations for handheld PCs are being evaluated, but no release date was given for them, and the "
    "interview closes with the team describing how player reports about stuttering guided the work.")
WITCHER_CLAIMS = [
    ("C1", "CD Projekt RED engineers said the latest update cuts the GPU cost of ray-traced global "
           "illumination by 20 percent on PC.",
     "the latest update for The Witcher 3 cuts the GPU cost of ray-traced global illumination by 20 percent",
     {"importance": "core", "value": "20 percent", "category": "spec"}),
    ("C2", "The update adds a 60 fps performance mode for PlayStation 5 and Xbox Series X.",
     "adds a new performance mode for PlayStation 5 and Xbox Series X that targets 60 frames per second",
     {"importance": "core", "value": "60", "category": "feature"}),
    ("C3", "The engineers said the changes come from a rewritten lighting cache.",
     "the changes come from a rewritten lighting cache"),
]
WITCHER_ARTICLE = article(
    "در گفت‌وگو با Gamingbolt: به‌روزرسانی The Witcher 3 هزینه Ray Tracing را ۲۰ درصد کم کرده است",
    "به گزارش Gamingbolt، مهندسان CD Projekt RED می‌گویند به‌روزرسانی تازه The Witcher 3 هزینه پردازش نورپردازی "
    "سراسری با ray tracing را روی PC تا ۲۰ درصد کاهش داده است.",
    [("به گفته این مهندسان، به‌روزرسانی یک حالت عملکرد تازه برای PlayStation 5 و Xbox Series X هم اضافه می‌کند که "
      "بدون ray tracing نرخ ۶۰ فریم بر ثانیه را هدف می‌گیرد.", ["C2"]),
     ("آن‌ها توضیح دادند این تغییرات حاصل بازنویسی حافظه نورپردازی بازی است.", ["C3"])],
    ["The Witcher 3"])


def test_technical_interview_with_new_information_is_reported(repo):
    assert "technical interview" in WITCHER_LOG["reason"]                    # the live rejection
    assert "Judge the information, not the format" in TRIAGE_SYSTEM and "interview" in TRIAGE_SYSTEM
    use_sources(repo)
    web = FakeWeb()
    web.add(FEED, rss([{"title": WITCHER_TITLE, "link": "https://gb.example/witcher-interview",
                        "content": WITCHER_TEXT, "published": NOW - timedelta(hours=2)}]))
    web.add("https://gb.example/witcher-interview", f"<html><body><article>{WITCHER_TEXT}</article></body></html>")
    ai = FakeOpenAI({
        "triage": lambda kw: {"stories": [{"id": sid, "is_news": True, "kind": "interview", "significance": "medium",
                                           "relevance": "high", "reason": "engineers reveal performance changes"}
                                          for sid in __import__("re").findall(r'"id": "([0-9a-f]{16})"', kw["input"])]},
        "fact_sheet": lambda kw: fact_sheet(kw["input"], WITCHER_CLAIMS, kind="interview", event="patch_update",
                                            entities={"games": ["The Witcher 3"], "companies": ["CD Projekt RED"],
                                                      "platforms": ["PC", "PlayStation 5", "Xbox Series X"],
                                                      "products": []}),
        "article": lambda kw: WITCHER_ARTICLE})
    pipeline, *_ = build(repo, "dry-run", web=web, openai_client=ai)
    story = next(s for s in pipeline.run().stories if s["headline"] == WITCHER_TITLE)
    assert story["decision"] == "dry_run_pass", story["reasons"]
    checks = {c["name"]: c for c in story["gate"]["checks"]}
    assert checks["newsworthiness.is_news"]["detail"].startswith("news development reported in a interview")


def _story_kind(sheet_kind, statuses, is_news=True, reasons=("new development",)):
    claims = [{"id": f"C{i}", "text_en": "t", "category": "other", "subject": "", "value": "", "status": st,
               "confidence": "high", "importance": "core", "support": [], "verification": "single_source"}
              for i, st in enumerate(statuses, start=1)]
    sheet = parse_fact_sheet({"headline_en": "h", "event_type": "other", "kind": sheet_kind, "entities": {},
                              "claims": claims, "contradictions": [], "sources": [],
                              "newsworthiness": {"is_news": is_news, "significance": "medium",
                                                 "reasons": list(reasons)},
                              "open_questions": [], "injection_detected": False})
    for c in sheet.claims:
        c.verification, c.verified_sources = "single_source", ["S1"]
    return sheet


def test_crimson_desert_column_with_a_developer_statement_is_news(repo):
    live = DRYRUN["candidates"][0]
    assert "kind=opinion" in live["decision"]                               # the live rejection
    reasons = live["decision"].split("(kind=opinion): ")[1].split("; ")
    sheet = _story_kind("opinion", ("reported", "confirmed"), reasons=reasons)
    check = next(c for c in assess(sheet, _story("Pearl Abyss goes easy on us critics"), make_settings(repo))
                 if c.name == "newsworthiness.is_news")
    assert check.passed and "news development reported in a opinion" in check.detail
    assert "Never turn the writer's opinions" in EXTRACT_SYSTEM


def test_speculative_opinion_is_still_rejected(repo):
    s = make_settings(repo)
    speculative = _story_kind("opinion", ("speculative", "speculative"))
    assert not next(c for c in assess(speculative, _story("Why GTA 6 might slip again"), s)
                    if c.name == "newsworthiness.is_news").passed
    not_news = _story_kind("opinion", ("reported",), is_news=False)
    assert not next(c for c in assess(not_news, _story("My favourite RPGs of the year"), s)
                    if c.name == "newsworthiness.is_news").passed
    story = _story("Why GTA 6 might slip again")
    assert not apply_triage(story, {"is_news": False, "kind": "opinion", "reason": "column"})
    assert not apply_triage(_story("x"), {"is_news": True, "kind": "opinion", "reason": "column"})
    assert apply_triage(_story("x"), {"is_news": True, "kind": "interview", "significance": "medium",
                                      "relevance": "high", "reason": "new details"})


# ---------------------------------------------------------------------------
# 5. State: policy-change reconsideration; dry-run decisions never starve production
# ---------------------------------------------------------------------------
def _seed(repo, status, reasons, policy="", mode="publish", attempts=1, sources=1):
    from newsbot.state import StateStore
    store = StateStore(repo / "newsbot_state.db")
    conn = store.conn
    conn.execute("INSERT OR REPLACE INTO stories(story_id, status, last_reasons_json, policy_version, "
                 "decision_mode, attempts, source_count_at_decision, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
                 ("seed01", status, json.dumps(reasons), policy, mode, attempts, sources, NOW.isoformat()))
    conn.commit()
    store.close()


def _eligibility(repo, mode="publish"):
    pipeline, *_ = build(repo, mode)
    pipeline._setup()
    pipeline.gazetteer = None
    story = Story("seed01", [FeedItem("i", "Gamingbolt", "publication", "medium", "primary", "https://gb.example/s",
                                      "gb.example/s", "Duskwind Tactics Patch 1.2", "", NOW - timedelta(hours=2))],
                  primary_entity="duskwind tactics", primary_display="Duskwind Tactics")
    verdict = pipeline._eligibility(story)
    pipeline.store.close()
    return verdict, story


def test_policy_change_reconsiders_policy_rejections_once(repo):
    reason = DRYRUN["candidates"][2]["decision"].split("| ")[1]
    _seed(repo, "rejected", [reason], policy="2.0+2.1.0", attempts=3)
    verdict, story = _eligibility(repo)
    assert verdict == "" and any("reconsidered" in r for r in story.score_reasons)
    _seed(repo, "rejected", [reason], policy=policy_version())           # already decided under this policy
    assert _eligibility(repo)[0] == "rejected earlier; no new sources since"


def test_permanent_rejections_and_published_stories_are_not_reconsidered(repo):
    _seed(repo, "rejected", ["duplicate: WordPress post 7044 already covers Duskwind Tactics"], policy="2.0")
    assert _eligibility(repo)[0] == "rejected earlier; no new sources since"
    _seed(repo, "published", ["published"], policy="2.0")
    assert _eligibility(repo)[0].startswith("duplicate: story already published")


def test_dry_run_rejections_do_not_starve_production(repo):
    _seed(repo, "dry_run_rejected", ["sources.reliability: x"], policy=policy_version(), mode="dry-run")
    assert _eligibility(repo, "publish")[0] == ""                          # production evaluates it afresh
    assert _eligibility(repo, "dry-run")[0] == "rejected earlier; no new sources since"   # no repeat spend


def test_dry_run_records_rejections_separately(repo):
    use_sources(repo)
    pipeline, *_ = build(repo, "dry-run", web=patch_web(), openai_client=patch_ai(), max_candidates_per_run=1)
    pipeline.run()
    conn = sqlite3.connect(repo / "newsbot_state.db")
    rows = conn.execute("SELECT status, policy_version, decision_mode FROM stories WHERE status LIKE '%rejected%'")
    rows = rows.fetchall()
    conn.close()
    assert rows and all(r == ("dry_run_rejected", policy_version(), "dry-run") for r in rows)


def test_gate_check_names_are_stable():
    assert CheckResult("sources.risk", True, False).blocking is False


def test_ordinary_titles_are_not_mistaken_for_high_risk(repo):
    docs = [SourceDoc("S1", "https://gb.example/a", "Gamingbolt", "publication", "medium", "t", DOC_TEXT,
                      origin_group=1)]
    settings = make_settings(repo)
    for title in ("Death Stranding 2 Gets PC Release Date", "Counter-Strike 2 Adds New Map in Latest Update",
                  "Hacker Simulator Leaves Early Access", "Dying Light: The Beast Gets Photo Mode"):
        assert story_risk(_story(title), _sheet(docs), settings) == [], title
    for title in ("Studio Behind Duskwind Tactics Hit by Layoffs", "Duskwind Tactics Cancelled",
                  "Duskwind Tactics Sequel Leaked Ahead of Reveal", "Publisher Acquired by Holding Group"):
        assert story_risk(_story(title), _sheet(docs), settings), title
