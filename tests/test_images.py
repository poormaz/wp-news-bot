"""Featured images: only Poormaz-owned artwork, validated against the media library, never publisher images.

All four live samples (run 37851559690) ended with media_id = 0 because no fallback artwork was configured.
"""

from conftest import build, make_settings

from newsbot.config import parse_media_ids
from newsbot.images import choose_featured_image, fallback_candidates
from newsbot.models import SourceDoc


def test_fallback_ids_accept_a_plain_list_or_per_content_type():
    assert parse_media_ids("123, 456") == {"default": [123, 456]}
    assert parse_media_ids("gaming=11,12; hardware=21\ndefault=31", 31) == {
        "gaming": [11, 12], "hardware": [21], "default": [31]}
    assert parse_media_ids("", 77) == {"default": [77]}          # NEWSBOT_FALLBACK_MEDIA_ID still works
    assert parse_media_ids("garbage") == {}


def test_unknown_content_type_is_a_configuration_problem(repo):
    settings = make_settings(repo, fallback_featured_media_ids=parse_media_ids("gameing=5"))
    assert any("NEWSBOT_FALLBACK_MEDIA_IDS" in p for p in settings.validate())


def test_rotation_is_stable_per_story_and_varies_between_stories():
    ids = {"gaming": [11, 12, 13], "default": [31]}
    assert fallback_candidates(ids, "gaming", "story-a") == fallback_candidates(ids, "gaming", "story-a")
    firsts = {fallback_candidates(ids, "gaming", f"story-{n}")[0] for n in range(30)}
    assert firsts == {11, 12, 13}
    assert fallback_candidates(ids, "hardware", "x") == [31]       # falls back to "default"
    assert fallback_candidates({}, "gaming", "x") == []


def test_only_validated_library_images_are_used():
    ids = {"gaming": [11, 12]}
    doc = SourceDoc("S1", "https://outlet.example/a", "Outlet", "publication", "high", "t", "x" * 300)
    doc.image_url = "https://outlet.example/cover.jpg"             # publisher image: never copied
    choice = choose_featured_image("safe", [doc], None, ids, True, None, content_type="gaming", seed="s",
                                   media_ok=lambda i: i == 12)
    assert choice.media_id == 12 and choice.upload is None and "NEWSBOT_FALLBACK_MEDIA_IDS" in choice.basis
    none = choose_featured_image("safe", [doc], None, ids, True, None, seed="s", media_ok=lambda i: False)
    assert none.media_id == 0 and "skipped media" in none.basis


def test_localization_artwork_is_preferred_but_also_validated():
    loc = {"name": "Ironvale Chronicles", "featured_media": 44}
    assert choose_featured_image("safe", [], loc, {"default": [31]}, True, None,
                                 media_ok=lambda i: True).media_id == 44
    assert choose_featured_image("safe", [], loc, {"default": [31]}, True, None,
                                 media_ok=lambda i: i != 44).media_id == 31


def test_published_post_gets_a_validated_poormaz_image(repo):
    pipeline, web, fake_wp, _ = build(repo, "publish", fallback_featured_media_ids={"gaming": [501, 503, 502]})
    fake_wp.library = {502: "image", 503: "file"}                 # 501 does not exist, 503 is a PDF
    report = pipeline.run()
    post = next(iter(fake_wp.posts.values()))
    assert post["featured_media"] == 502
    story = next(s for s in report.stories if s["decision"] == "published")
    assert story["image"]["media_id"] == 502 and story["image"]["upload_from"] == ""
    assert not any("/wp-json/wp/v2/media" == w.split("poormaz.test")[-1] for w in web.writes())   # no upload
