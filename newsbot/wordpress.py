"""WordPress REST client with a hard write guard.

* dry-run: any write raises DryRunViolation before a request is made.
* draft:   posts can only be created/updated as drafts; status=publish is refused.
* publish: posts are created as drafts, verified, then switched to publish.
Writes are never retried automatically (a retried POST could duplicate a post);
idempotency comes from the story marker embedded in every post.
"""

from __future__ import annotations

import base64
import html as html_lib
import logging
import re
import time

import requests

from .compose import MARKER_PREFIX
from .textutil import clean_text, entity_key, to_ascii_digits

log = logging.getLogger("newsbot.wp")


class DryRunViolation(RuntimeError):
    """A WordPress write was attempted in dry-run mode."""


class PublishNotAllowed(RuntimeError):
    """status=publish was requested outside publish mode."""


class WordPressError(RuntimeError):
    def __init__(self, message: str, status: int = 0, transient: bool = False):
        super().__init__(message)
        self.status = status
        self.transient = transient


MARKER_RE = re.compile(re.escape(MARKER_PREFIX) + r"([0-9a-f]{8,40})")
HREF_RE = re.compile(r"""href=["'](https?://[^"'#]+)["']""", re.I)


class WordPressClient:
    def __init__(self, base_url: str, username: str, app_password: str, mode: str, user_agent: str,
                 timeout: float = 25.0, session: requests.Session | None = None, sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.mode = mode
        self.timeout = timeout
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self._auth = ""
        if username and app_password:
            token = base64.b64encode(f"{username}:{app_password}".encode()).decode()
            self._auth = f"Basic {token}"
        self.writes: list[str] = []
        self._sleep = sleep

    @property
    def authenticated(self) -> bool:
        return bool(self._auth)

    def _headers(self, json_body: bool = False, auth: bool = True) -> dict:
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if auth and self._auth:
            headers["Authorization"] = self._auth
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base_url}/wp-json/{path.lstrip('/')}"

    # -- reads ---------------------------------------------------------------
    def _get(self, path: str, params: dict | None = None, auth: bool = True, retries: int = 2):
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = self.session.get(self._url(path), params=params, headers=self._headers(auth=auth),
                                        timeout=self.timeout)
            except requests.RequestException as exc:
                last = exc
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code not in (429, 500, 502, 503, 504, 415):
                    raise WordPressError(f"GET {path} -> HTTP {resp.status_code}", resp.status_code)
                last = WordPressError(f"GET {path} -> HTTP {resp.status_code}", resp.status_code, transient=True)
            if attempt < retries:
                self._sleep(2.0 * (attempt + 1))
        if isinstance(last, WordPressError):
            raise last
        raise WordPressError(f"GET {path} failed: {type(last).__name__}", transient=True)

    def check_me(self) -> dict:
        return self._get("wp/v2/users/me", {"context": "edit", "_fields": "id,name,roles,capabilities"}).json()

    def recent_posts(self, count: int = 40) -> list[dict]:
        """Recent posts incl. drafts/scheduled (when authenticated) for duplicate protection."""
        fields = "id,date_gmt,status,slug,link,title,content"
        params = {"per_page": str(min(100, count)), "orderby": "date", "order": "desc", "_fields": fields}
        if self.authenticated:
            params.update({"status": "publish,future,draft,pending,private", "context": "edit"})
        resp = self._get("wp/v2/posts", params)
        posts = []
        for post in resp.json() or []:
            title = post.get("title") or {}
            content = post.get("content") or {}
            raw = content.get("raw") or content.get("rendered") or ""
            posts.append({
                "id": int(post.get("id") or 0),
                "date": (post.get("date_gmt") or "") + ("+00:00" if post.get("date_gmt") else ""),
                "status": post.get("status", ""),
                "slug": post.get("slug", ""),
                "link": post.get("link", ""),
                "title": clean_text(html_lib.unescape(title.get("raw") or title.get("rendered") or "")),
                "content": raw,
            })
        return posts

    def search_posts(self, query: str, count: int = 10) -> list[dict]:
        resp = self._get("wp/v2/posts", {"search": query, "per_page": str(count), "status": "publish",
                                         "_fields": "id,link,title,date_gmt,featured_media"}, auth=False)
        out = []
        for post in resp.json() or []:
            out.append({"id": int(post.get("id") or 0), "link": post.get("link", ""),
                        "title": clean_text(html_lib.unescape((post.get("title") or {}).get("rendered", ""))),
                        "date": post.get("date_gmt", ""), "featured_media": int(post.get("featured_media") or 0)})
        return out

    def find_by_marker(self, story_id: str) -> dict | None:
        token = f"nbstory-{story_id}"
        params = {"search": token, "per_page": "5", "_fields": "id,status,link,slug,content"}
        if self.authenticated:
            params.update({"status": "publish,future,draft,pending,private", "context": "edit"})
        for post in self._get("wp/v2/posts", params).json() or []:
            content = (post.get("content") or {})
            if token in (content.get("raw") or content.get("rendered") or ""):
                return {"id": int(post["id"]), "status": post.get("status"), "link": post.get("link", ""),
                        "slug": post.get("slug", "")}
        return None

    def get_post(self, post_id: int) -> dict:
        params = {"context": "edit"} if self.authenticated else {}
        return self._get(f"wp/v2/posts/{int(post_id)}", params).json()

    def find_tag(self, name: str) -> int | None:
        key = entity_key(name)
        resp = self._get("wp/v2/tags", {"search": name, "per_page": "50", "_fields": "id,name,count"}, auth=False)
        for term in resp.json() or []:
            if entity_key(html_lib.unescape(term.get("name", ""))) == key:
                return int(term["id"])
        return None

    def count_posts_mentioning(self, name: str) -> int:
        resp = self._get("wp/v2/posts", {"search": name, "per_page": "10", "status": "publish",
                                         "_fields": "id,title"}, auth=False)
        key = entity_key(name)
        return sum(1 for p in resp.json() or []
                   if key in entity_key(html_lib.unescape((p.get("title") or {}).get("rendered", ""))))

    def categories(self) -> list[dict]:
        resp = self._get("wp/v2/categories", {"per_page": "100", "_fields": "id,name,slug,count"}, auth=False)
        return resp.json() or []

    def get_media(self, media_id: int) -> dict | None:
        try:
            return self._get(f"wp/v2/media/{int(media_id)}", {"_fields": "id,source_url,media_type,alt_text"},
                             auth=False).json()
        except WordPressError:
            return None

    # -- writes ------------------------------------------------------------------
    def _guard(self, action: str, payload: dict | None = None) -> None:
        if self.mode == "dry-run":
            raise DryRunViolation(f"dry-run: refusing WordPress write ({action})")
        if not self.authenticated:
            raise WordPressError("WordPress credentials missing", 401)
        status = (payload or {}).get("status")
        if status and status != "draft" and self.mode != "publish":
            raise PublishNotAllowed(f"{self.mode} mode: refusing status={status}")
        self.writes.append(action)

    def _post(self, path: str, payload: dict | None = None, data: bytes | None = None, headers: dict | None = None):
        try:
            resp = self.session.post(self._url(path), json=payload if data is None else None, data=data,
                                     headers=headers or self._headers(json_body=True), timeout=self.timeout)
        except requests.RequestException as exc:
            raise WordPressError(f"POST {path} failed: {type(exc).__name__}", transient=True) from exc
        if resp.status_code >= 400:
            transient = resp.status_code in (429, 500, 502, 503, 504)
            raise WordPressError(f"POST {path} -> HTTP {resp.status_code}: {clean_text(resp.text)[:200]}",
                                 resp.status_code, transient)
        return resp.json()

    def create_post(self, payload: dict) -> dict:
        payload = dict(payload)
        payload["status"] = "draft"  # always created as draft; publishing is a separate verified step
        self._guard("create_post", payload)
        return self._post("wp/v2/posts", payload)

    def update_post(self, post_id: int, payload: dict) -> dict:
        self._guard(f"update_post:{post_id}", payload)
        return self._post(f"wp/v2/posts/{int(post_id)}", payload)

    def create_tag(self, name: str) -> int:
        self._guard("create_tag")
        data = self._post("wp/v2/tags", {"name": name})
        return int(data["id"])

    def upload_media(self, content: bytes, filename: str, mime: str, alt_text: str) -> dict:
        self._guard("upload_media")
        headers = self._headers()
        headers["Content-Type"] = mime
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        media = self._post("wp/v2/media", data=content, headers=headers)
        if alt_text:
            try:
                self._post(f"wp/v2/media/{int(media['id'])}", {"alt_text": alt_text})
            except WordPressError as exc:
                log.warning("Media alt text not set: %s", exc)
        return media

    def update_rankmath(self, post_id: int, title: str, description: str, focus_keyword: str) -> bool:
        self._guard("rankmath_update_meta")
        payload = {"objectType": "post", "objectID": int(post_id), "meta": {
            "rank_math_title": title, "rank_math_description": description, "rank_math_focus_keyword": focus_keyword}}
        try:
            self._post("rankmath/v1/updateMeta", payload)
            return True
        except WordPressError as exc:
            log.warning("Rank Math meta not updated (post kept): %s", exc)
            return False


def parse_post_fingerprint(post: dict) -> dict:
    """Story marker, cited source URLs and numbers of an existing post (for dedup/recovery)."""
    content = post.get("content", "")
    marker = MARKER_RE.search(content)
    site = ""
    link = post.get("link", "")
    if link:
        site = re.sub(r"^https?://(www\.)?", "", link).split("/")[0]
    urls = []
    for href in HREF_RE.findall(content):
        host = re.sub(r"^https?://(www\.)?", "", href).split("/")[0]
        if site and host == site:
            continue
        urls.append(href)
    text = to_ascii_digits(clean_text(content))
    return {
        "story_id": marker.group(1) if marker else "",
        "source_urls": urls,
        "numbers": sorted(set(re.findall(r"\d+(?:\.\d+)?", text)))[:200],
    }
