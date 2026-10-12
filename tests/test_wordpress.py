"""Areas 13 and 18: WordPress write guard, publishing failures, fingerprints."""

import pytest
from conftest import SITE, FakeWeb, FakeWordPress

from newsbot.wordpress import (
    DryRunViolation,
    PublishNotAllowed,
    WordPressClient,
    WordPressError,
    parse_post_fingerprint,
)


def client(web, mode, auth=True):
    return WordPressClient(SITE, "bot" if auth else "", "abcd efgh ijkl mnop" if auth else "", mode, "UA",
                           session=web.session(), sleep=lambda s: None)


def test_dry_run_refuses_every_write_before_any_request():
    web = FakeWeb()
    FakeWordPress(web)
    wp = client(web, "dry-run")
    for call in (lambda: wp.create_post({"title": "t"}), lambda: wp.update_post(1, {"status": "publish"}),
                 lambda: wp.create_tag("x"), lambda: wp.upload_media(b"x", "a.jpg", "image/jpeg", ""),
                 lambda: wp.update_rankmath(1, "t", "d", "k")):
        with pytest.raises(DryRunViolation):
            call()
    assert web.writes() == []


def test_draft_mode_can_never_publish():
    web = FakeWeb()
    fake = FakeWordPress(web)
    wp = client(web, "draft")
    post = wp.create_post({"title": "t", "content": "c", "status": "publish"})   # status is forced to draft
    assert fake.posts[post["id"]]["status"] == "draft"
    with pytest.raises(PublishNotAllowed):
        wp.update_post(post["id"], {"status": "publish"})
    assert fake.posts[post["id"]]["status"] == "draft"


def test_publish_mode_creates_draft_first():
    web = FakeWeb()
    fake = FakeWordPress(web)
    wp = client(web, "publish")
    post = wp.create_post({"title": "t", "content": "c"})
    assert fake.posts[post["id"]]["status"] == "draft"
    wp.update_post(post["id"], {"status": "publish"})
    assert fake.posts[post["id"]]["status"] == "publish"


def test_writes_require_credentials():
    web = FakeWeb()
    FakeWordPress(web)
    with pytest.raises(WordPressError):
        client(web, "publish", auth=False).create_post({"title": "t"})


def test_server_errors_are_reported_not_retried():
    web = FakeWeb()
    fake = FakeWordPress(web)
    fake.fail_create = (503, {"code": "unavailable"}, {})
    wp = client(web, "publish")
    with pytest.raises(WordPressError) as exc:
        wp.create_post({"title": "t"})
    assert exc.value.transient and exc.value.status == 503
    assert len([w for w in web.writes() if w.endswith("/wp/v2/posts")]) == 1   # POST never auto-retried
    fake.fail_create = (400, {"code": "rest_invalid_param"}, {})
    with pytest.raises(WordPressError) as exc:
        wp.create_post({"title": "t"})
    assert not exc.value.transient


def test_reads_retry_transient_errors():
    web = FakeWeb()
    calls = {"n": 0}

    @web.handler
    def flaky(request):
        if "/wp/v2/categories" in request.url:
            calls["n"] += 1
            return (502, "bad gateway", {}) if calls["n"] < 3 else (200, [{"id": 2}], {})
        return None
    assert client(web, "dry-run").categories() == [{"id": 2}]
    assert calls["n"] == 3


def test_rankmath_failure_is_non_fatal():
    web = FakeWeb()
    web.handler(lambda r: (404, {"code": "rest_no_route"}, {}) if "rankmath" in r.url else None)
    FakeWordPress(web)
    assert client(web, "publish").update_rankmath(5, "t", "d", "k") is False


def test_find_by_marker_and_fingerprint():
    web = FakeWeb()
    content = ('<p>متن</p><a href="https://gamingbolt.example/a">GB</a><a href="https://poormaz.test/x/">in</a>'
               "<!-- wp:html -->\n<!-- poormaz-newsbot v2 nbstory-0123456789abcdef -->\n<!-- /wp:html -->"
               "<p>۱۹ مارس ۲۰۲۷</p>")
    FakeWordPress(web, existing_posts=[{"id": 77, "title": "t", "content": content, "status": "draft", "slug": "s",
                                        "link": f"{SITE}/s/"}])
    wp = client(web, "publish")
    assert wp.find_by_marker("0123456789abcdef")["id"] == 77
    assert wp.find_by_marker("ffffffffffffffff") is None
    fp = parse_post_fingerprint({"content": content, "link": f"{SITE}/s/"})
    assert fp["story_id"] == "0123456789abcdef"
    assert fp["source_urls"] == ["https://gamingbolt.example/a"]       # own-site links excluded
    assert {"19", "2027"} <= set(fp["numbers"])                         # Persian digits normalised
