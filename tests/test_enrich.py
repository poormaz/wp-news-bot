"""Areas 11-12: internal-link validation, categories, tags, slugs, localization links."""

from conftest import SITE, FakeWeb, FakeWordPress, make_settings

from newsbot.compose import render_article, valid_anchor
from newsbot.enrich import (
    LinkBuilder,
    build_slug,
    find_localization,
    load_localizations,
    localization_sentence,
    pick_categories,
    resolve_tags,
    sanitize_tags,
)
from newsbot.facts import parse_fact_sheet
from newsbot.httpclient import HttpClient
from newsbot.models import Article, Entity, FeedItem, LinkCandidate, Story
from newsbot.state import StateStore
from newsbot.wordpress import WordPressClient


def story(display="Ironvale Chronicles", key="ironvale chronicles"):
    return Story("s1", [], primary_entity=key, primary_display=display, event_type="release_date")


def clients(repo, web, mode="dry-run"):
    s = make_settings(repo, mode)
    session = web.session()
    http = HttpClient(s.user_agent, 5, True, session=session)
    wp = WordPressClient(SITE, "bot", "abcd efgh ijkl mnop", mode, s.user_agent, session=session, sleep=lambda x: 0)
    return s, http, wp


def test_localization_map_rejects_foreign_and_insecure_urls(tmp_path):
    path = tmp_path / "loc.yaml"
    path.write_text(
        "localizations:\n"
        "  Good Game:\n    url: https://www.poormaz.test/good/\n    aliases: [GG Remastered]\n"
        "  Evil Game:\n    url: https://evil.example/page/\n"
        "  Http Game:\n    url: http://poormaz.test/http/\n"
        "  Broken: notamapping\n", encoding="utf-8")
    index = load_localizations(path, "https://poormaz.test")
    assert set(index) == {"good game", "gg remastered"}
    assert load_localizations(tmp_path / "missing.yaml", SITE) == {}


def test_repository_localization_map_is_on_poormaz():
    from conftest import ROOT
    index = load_localizations(ROOT / "localization_pages.yaml", "https://poormaz.com")
    assert index, "localization_pages.yaml must contain valid entries"
    assert all(e["url"].startswith("https://poormaz.com/") for e in index.values())


def test_find_localization_uses_entities(tmp_path):
    path = tmp_path / "loc.yaml"
    path.write_text("localizations:\n  Ironvale Chronicles:\n    url: https://poormaz.test/iv/\n", encoding="utf-8")
    index = load_localizations(path, SITE)
    assert find_localization(story(), index)["url"] == "https://poormaz.test/iv/"
    assert find_localization(story("Other", "other game"), index, ["Ironvale Chronicles"]) is not None
    assert find_localization(story("Other", "other game"), index) is None


def test_link_validation_rejects_redirects_404_and_offsite(repo):
    web = FakeWeb()
    web.add(f"{SITE}/ok/", "<html><h1>زیرنویس فارسی Ironvale Chronicles</h1></html>")
    web.add(f"{SITE}/moved/", "", status=301, headers={"Location": f"{SITE}/new/"})
    web.add(f"{SITE}/gone/", "", status=404)
    s, http, wp = clients(repo, web)
    store = StateStore(":memory:")
    builder = LinkBuilder(s, http, None, store)
    assert builder.validate(f"{SITE}/ok/")
    assert not builder.validate(f"{SITE}/moved/")
    assert not builder.validate(f"{SITE}/gone/")
    requests_before = len(web.requests)
    assert builder.validate(f"{SITE}/ok/")                 # cached: no second request
    assert len(web.requests) == requests_before
    st = story()
    st.parent_post = {"link": "https://evil.example/x", "title": "x"}
    cands = builder.candidates(st, {"url": f"{SITE}/ok/", "name": "Ironvale Chronicles", "kind": "subtitle"})
    assert [c.url for c in cands] == [f"{SITE}/ok/"]       # offsite parent link dropped


def test_localization_page_must_mention_the_game(repo):
    web = FakeWeb()
    web.add(f"{SITE}/right/", "<html><title>دانلود زیرنویس فارسی Ironvale Chronicles</title></html>")
    web.add(f"{SITE}/wrong/", "<html><title>دانلود زیرنویس فارسی Another Game</title></html>")
    s, http, wp = clients(repo, web)
    builder = LinkBuilder(s, http, None, StateStore(":memory:"))
    right = builder.candidates(story(), {"url": f"{SITE}/right/", "name": "Ironvale Chronicles"})
    wrong = builder.candidates(story(), {"url": f"{SITE}/wrong/", "name": "Ironvale Chronicles"})
    assert [c.url for c in right] == [f"{SITE}/right/"] and wrong == []


def test_related_coverage_requires_entity_match_and_no_duplicates(repo):
    web = FakeWeb()
    FakeWordPress(web, existing_posts=[
        {"id": 1, "title": "تریلر جدید Ironvale Chronicles", "content": "x", "status": "publish", "slug": "a",
         "link": f"{SITE}/a/"},
        {"id": 2, "title": "خبری درباره بازی دیگر Ironvale", "content": "Ironvale Chronicles", "status": "publish",
         "slug": "b", "link": f"{SITE}/b/"},
        {"id": 3, "title": "Ironvale Chronicles پیش‌نمایش", "content": "x", "status": "draft", "slug": "c",
         "link": f"{SITE}/c/"},
    ])
    web.add(f"{SITE}/a/", "ok")
    web.add(f"{SITE}/loc/", "<h1>زیرنویس فارسی Ironvale Chronicles</h1>")
    s, http, wp = clients(repo, web)
    cands = LinkBuilder(s, http, wp, None).candidates(story(), {"url": f"{SITE}/loc/", "name": "Ironvale Chronicles"},
                                                      exclude_urls=[f"{SITE}/self/"])
    assert [c.url for c in cands] == [f"{SITE}/loc/", f"{SITE}/a/"]   # unrelated title & draft excluded
    assert [c.id for c in cands] == ["L1", "L2"]


def test_rendered_links_no_self_no_duplicates_and_anchor_rules(repo):
    s = make_settings(repo)
    links = [LinkCandidate("L1", f"{SITE}/loc/", "t", "localization")]
    sheet = parse_fact_sheet({"claims": []})
    art = Article("t", "m", "d", "k", "s", {"text": "لید [[L1|لینک در لید]]", "claim_ids": []},
                  [{"heading_fa": "", "paragraphs": [
                      {"text": "اول [[L1|زیرنویس فارسی بازی]] و دوباره [[L1|باز هم همان لینک]]", "claim_ids": []},
                      {"text": "لینک ناشناخته [[L7|ساختگی]]", "claim_ids": []}]}], [], [], "gaming", {})
    out = render_article(art, story(), sheet, [], links, s, {f"{SITE}/loc/": []})
    assert out.html.count(f'href="{SITE}/loc/"') == 1        # once, never in the lead
    assert "لینک در لید" in out.html and "ساختگی" in out.html
    assert valid_anchor("زیرنویس فارسی بازی", []) and not valid_anchor("زیرنویس فارسی بازی", ["زیرنویس فارسی بازی"])
    assert not valid_anchor("x " * 12, [])


def test_localization_sentence_varies_anchor():
    entry = {"name": "Ironvale Chronicles", "url": f"{SITE}/loc/", "anchor": "دانلود زیرنویس فارسی Ironvale Chronicles",
             "kind": "subtitle"}
    html, anchor = localization_sentence(entry, "story-1", [])
    assert f'href="{SITE}/loc/"' in html and anchor in html
    _, second = localization_sentence(entry, "story-1", [anchor])
    assert second != anchor                                  # avoids repeating a recent exact-match anchor


def test_categories(repo):
    s = make_settings(repo)
    assert pick_categories("gaming", s) == [2, 18]
    assert pick_categories("hardware", s) == [2, 19]
    assert pick_categories("general", s) == [2]
    s.cat_all = 0
    s.cat_default = 7
    assert pick_categories("general", s) == [7]


def test_content_type_from_entities(repo):
    from newsbot.enrich import content_type_for
    art = Article("t", "", "", "", "", {}, [], [], [], "general", {})
    item = FeedItem("i", "A", "publication", "medium", "primary", "u", "u", "t", "", __import__("conftest").NOW,
                    entities=[Entity("geforce rtx 5090", "GeForce RTX 5090", "hardware", 0.9)])
    st = Story("s", [item], primary_entity="geforce rtx 5090", primary_display="GeForce RTX 5090")
    assert content_type_for(art, st) == "hardware"


def test_tag_sanitizing_and_limits():
    assert sanitize_tags(["Gaming", "PC", "Ironvale Chronicles", "ironvale chronicles", "12", "Northlight Forge",
                          "Third"], 2) == ["Ironvale Chronicles", "Northlight Forge"]


def test_tag_policy_avoids_thin_archives(repo):
    web = FakeWeb()
    fake = FakeWordPress(web, existing_posts=[
        {"id": 1, "title": "Old Game خبر اول", "content": "", "status": "publish", "slug": "a", "link": f"{SITE}/a/"},
        {"id": 2, "title": "Old Game خبر دوم", "content": "", "status": "publish", "slug": "b", "link": f"{SITE}/b/"}])
    s, http, wp = clients(repo, web, mode="publish")
    ids, notes = resolve_tags(["Northlight Forge", "Brand New Game", "Old Game"], wp, s, dry_run=False)
    assert ids[0] == 1                                       # existing tag reused
    assert "Brand New Game" not in fake.tags.values()        # no archive for an uncovered entity
    assert "Old Game" not in fake.tags.values()              # max 2 tags (WP_TAGS_MAX default)
    s.tags_max = 3
    ids, notes = resolve_tags(["Old Game"], wp, s, dry_run=False)
    assert "Old Game" in fake.tags.values()                  # covered by >= 2 posts -> created
    s.tag_create_policy = "never"
    ids, notes = resolve_tags(["Another New"], wp, s, dry_run=False)
    assert ids == [] and "Another New" not in fake.tags.values()


def test_tags_in_dry_run_never_create(repo):
    web = FakeWeb()
    fake = FakeWordPress(web)
    s, http, wp = clients(repo, web)
    s.tag_create_policy = "always"
    ids, notes = resolve_tags(["Brand New"], wp, s, dry_run=True)
    assert ids == [] and any("would create" in n for n in notes) and len(fake.tags) == 1


def test_slug_contains_subject_and_is_ascii():
    art = Article("t", "", "", "", "release-date-march-2027", {}, [], [], [], "gaming", {})
    assert build_slug(art, story()) == "ironvale-chronicles-release-date-march-2027"
    art.slug_en = ""
    st = story()
    st.items = [FeedItem("i", "A", "publication", "medium", "primary", "u", "u",
                         "Ironvale Chronicles Launches March 19, 2027", "", __import__("conftest").NOW)]
    assert build_slug(art, st).startswith("ironvale-chronicles")
