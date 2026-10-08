"""Logging with secret redaction.

All log records pass through RedactingFilter, which replaces any configured secret
value and any Authorization/Bearer/API-key looking token with "***" before output.
"""

from __future__ import annotations

import logging
import re
import sys

_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)(basic|bearer)\s+[A-Za-z0-9+/=._\-]+"),
    re.compile(r"\b(?:Basic|Bearer)\s+[A-Za-z0-9+/._\-]{8,}={0,2}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(app[_-]?password\s*[:=]\s*)\S+"),
]


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None):
        super().__init__()
        self.secrets: list[str] = []
        self.add_secrets(secrets or [])

    def add_secrets(self, secrets: list[str]) -> None:
        for secret in secrets:
            if secret and len(secret) >= 6 and secret not in self.secrets:
                self.secrets.append(secret)
                # WordPress application passwords are often shown with spaces removed.
                compact = secret.replace(" ", "")
                if compact != secret and len(compact) >= 6:
                    self.secrets.append(compact)
        self.secrets.sort(key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            if secret in text:
                text = text.replace(secret, "***")
        for pattern in _PATTERNS:
            if pattern.groups >= 1:
                text = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "***", text)
            else:
                text = pattern.sub("***", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - malformed log call must not break logging
            return True
        redacted = self.redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            text = self.redact(str(exc))
            if text != str(exc):
                record.exc_text = f"{type(exc).__name__}: {text}"
                record.exc_info = None
        return True


_FILTER = RedactingFilter()


def setup_logging(secrets: list[str] | None = None, level: int = logging.INFO) -> RedactingFilter:
    _FILTER.add_secrets(secrets or [])
    root = logging.getLogger()
    if not any(getattr(h, "_newsbot", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        handler._newsbot = True  # type: ignore[attr-defined]
        handler.addFilter(_FILTER)
        root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("httpx", "httpx2", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return _FILTER


def redact(text: str) -> str:
    return _FILTER.redact(text)
