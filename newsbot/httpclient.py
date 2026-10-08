"""HTTP access for feeds and source pages.

* Only idempotent methods are retried automatically (a retried POST could duplicate a post).
* robots.txt is honoured for article fetches; access controls, logins and paywalls are
  never bypassed. Blocked, removed and anti-bot pages are classified, not worked around.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("newsbot.http")

ANTIBOT_MARKERS = re.compile(
    r"(cf-browser-verification|cf-chl-|challenge-platform|just a moment\.\.\.|attention required! \| cloudflare|"
    r"verify you are human|are you a robot|captcha|access denied|request unsuccessful\. incapsula|"
    r"px-captcha|perimeterx|ddos-guard|enable javascript and cookies to continue)",
    re.I,
)
PAYWALL_MARKERS = re.compile(r"(subscribe to continue|subscribers only|this article is for subscribers|"
                             r"paywall|log in to continue reading|create a free account to continue)", re.I)
SOFT_404 = re.compile(r"<title[^>]*>[^<]*(page not found|404|not found|no longer available)[^<]*</title>", re.I)


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int = 0
    ok: bool = False
    text: str = ""
    content: bytes = b""
    content_type: str = ""
    error: str = ""
    classification: str = "ok"   # ok | not_found | blocked | paywalled | robots | rate_limited | error
    elapsed_ms: int = 0


class HttpClient:
    def __init__(self, user_agent: str, timeout: float = 20.0, respect_robots: bool = True,
                 session: requests.Session | None = None, max_bytes: int = 4_000_000):
        self.user_agent = user_agent
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.max_bytes = max_bytes
        self.session = session or self._build_session()
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self.requests_made = 0

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=3, connect=3, read=2, backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    # ------------------------------------------------------------------
    def robots_allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
            try:
                self.requests_made += 1
                resp = self.session.get(origin + "/robots.txt", timeout=self.timeout,
                                        headers={"User-Agent": self.user_agent})
                if resp.status_code >= 500:
                    parser = None            # RFC 9309: server error => assume disallowed
                elif resp.status_code >= 400:
                    parser.parse([])         # no robots.txt => everything allowed
                else:
                    parser.parse(resp.text.splitlines())
            except requests.RequestException as exc:
                log.info("robots.txt unavailable for %s (%s); treating as disallowed", origin, type(exc).__name__)
                parser = None
            self._robots[origin] = parser
        parser = self._robots[origin]
        if parser is None:
            return False
        # RobotFileParser matches on the token before the first "/", so pass the bot's
        # product token; rules for "*" apply when no specific group exists.
        return parser.can_fetch("PoormazNewsBot", url)

    def get(self, url: str, *, accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
            check_robots: bool = False, allow_redirects: bool = True, params: dict | None = None,
            headers: dict | None = None, binary: bool = False) -> FetchResult:
        result = FetchResult(url=url)
        if check_robots and not self.robots_allowed(url):
            result.classification = "robots"
            result.error = "disallowed by robots.txt"
            return result
        hdrs = {"User-Agent": self.user_agent, "Accept": accept}
        hdrs.update(headers or {})
        started = time.monotonic()
        try:
            self.requests_made += 1
            resp = self.session.get(url, headers=hdrs, timeout=self.timeout, allow_redirects=allow_redirects,
                                    params=params, stream=True)
            chunks, size = [], 0
            for chunk in resp.iter_content(65536):
                chunks.append(chunk)
                size += len(chunk)
                if size > self.max_bytes:
                    break
            body = b"".join(chunks)[: self.max_bytes]
            resp.close()
        except requests.RequestException as exc:
            result.error = f"{type(exc).__name__}"
            result.classification = "error"
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            return result
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        result.status = resp.status_code
        result.final_url = resp.url or url
        result.content_type = (resp.headers.get("Content-Type") or "").lower()
        result.content = body if binary else b""
        if not binary:
            encoding = resp.encoding or "utf-8"
            if encoding.lower() == "iso-8859-1" and "charset" not in result.content_type:
                encoding = "utf-8"
            result.text = body.decode(encoding, errors="replace")
        result.classification = classify_response(resp.status_code, result.text)
        result.ok = result.classification in ("ok", "paywalled")
        if 300 <= resp.status_code < 400 and not allow_redirects:
            result.final_url = resp.headers.get("Location", "")
            result.classification = "redirect"
            result.ok = False
        return result


def classify_response(status: int, text: str) -> str:
    if status in (404, 410):
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status in (401, 402, 403, 451):
        return "blocked"
    if status >= 500:
        return "error"
    if status >= 400:
        return "error"
    head = (text or "")[:20000]
    if ANTIBOT_MARKERS.search(head) and len(text or "") < 60000:
        return "blocked"
    if SOFT_404.search(head):
        return "not_found"
    if PAYWALL_MARKERS.search(text or ""):
        return "paywalled"
    return "ok"
