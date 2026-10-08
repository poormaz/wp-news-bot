"""Featured-image policy.

Publisher images are NOT copied just because they appear in a feed. Order of choice
under the default "safe" policy:
  1. a press-kit image from a source explicitly marked `images: press_kit` in sources.yaml
     (official press material intended for media use), downloaded and size-checked;
  2. the featured image of Poormaz's own localization page for the same game (an asset
     already in the Poormaz media library, reused by ID - nothing is copied);
  3. NEWSBOT_FALLBACK_MEDIA_ID (a site-owned generic news image);
  4. no featured image (the theme's default applies).
The "legacy" policy reproduces bot v1 (copy the source page's og:image) and exists
only for an explicit, informed opt-in.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

from .httpclient import HttpClient
from .models import SourceDoc

log = logging.getLogger("newsbot.images")


@dataclass
class ImageChoice:
    media_id: int = 0
    upload: tuple[bytes, str, str] | None = None   # (bytes, extension, mime)
    source_url: str = ""
    basis: str = "none"


def image_dimensions(data: bytes) -> tuple[int, int]:
    """PNG/JPEG/WebP dimensions without Pillow (ported from bot v1)."""
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return struct.unpack(">II", data[16:24])
    if len(data) >= 10 and data[:2] == b"\xff\xd8":
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            while index < len(data) and data[index] == 0xFF:
                index += 1
            if index >= len(data):
                break
            marker = data[index]
            index += 1
            if marker in (0xD8, 0xD9):
                continue
            if index + 2 > len(data):
                break
            length = struct.unpack(">H", data[index:index + 2])[0]
            if length < 2 or index + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} \
                    and index + 7 <= len(data):
                height, width = struct.unpack(">HH", data[index + 3:index + 7])
                return width, height
            index += length
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X":
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return 0, 0


def guess_ext_and_mime(content_type: str) -> tuple[str | None, str | None]:
    ct = (content_type or "").lower()
    for token, ext, mime in (("png", "png", "image/png"), ("webp", "webp", "image/webp"),
                             ("jpeg", "jpg", "image/jpeg"), ("jpg", "jpg", "image/jpeg")):
        if token in ct:
            return ext, mime
    return None, None


def download_image(http: HttpClient, url: str) -> tuple[bytes, str, str] | None:
    result = http.get(url, accept="image/webp,image/png,image/jpeg;q=0.9", binary=True)
    if not result.ok or not result.content:
        return None
    ext, mime = guess_ext_and_mime(result.content_type)
    if not ext:
        return None
    width, height = image_dimensions(result.content)
    if not width or width < 700 or height < 350:
        log.info("Image rejected (%sx%s): %s", width, height, url)
        return None
    return result.content, ext, mime


def choose_featured_image(policy: str, docs: list[SourceDoc], localization: dict | None, fallback_media_id: int,
                          reuse_localization: bool, http: HttpClient | None) -> ImageChoice:
    if policy == "none":
        return ImageChoice(basis="disabled by policy")
    if policy == "legacy":
        for doc in docs:
            if doc.image_url and http is not None:
                data = download_image(http, doc.image_url)
                if data:
                    return ImageChoice(upload=data, source_url=doc.image_url,
                                       basis=f"legacy policy: copied og:image from {doc.outlet}")
    if policy in ("safe", "legacy"):
        for doc in docs:
            if doc.image_rights == "press_kit" and doc.image_url and http is not None:
                data = download_image(http, doc.image_url)
                if data:
                    return ImageChoice(upload=data, source_url=doc.image_url,
                                       basis=f"press-kit image from {doc.outlet} (sources.yaml images: press_kit)")
        if reuse_localization and localization and int(localization.get("featured_media") or 0):
            return ImageChoice(media_id=int(localization["featured_media"]),
                               basis=f"Poormaz localization page artwork ({localization.get('name')})")
        if fallback_media_id:
            return ImageChoice(media_id=fallback_media_id, basis="site fallback image (NEWSBOT_FALLBACK_MEDIA_ID)")
    return ImageChoice(basis="no licensed image available; theme default")
