"""Secret masking for logs, events and CLI output."""
from __future__ import annotations

import logging
import re
import threading

_lock = threading.Lock()
_known: set[str] = set()

_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passphrase|authorization)\s*[=:]\s*\"?)[^\s\"&,;]{4,}"),
    re.compile(r"(gh[pousr]_)[A-Za-z0-9]{20,}"),
    re.compile(r"(eyJ)[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"),
]


def mask_value(v: str) -> str:
    if not v:
        return ""
    if len(v) <= 8:
        return "****"
    return f"{v[:2]}****{v[-2:]}"


def register_secret(v: str) -> None:
    if v and len(v) >= 4:
        with _lock:
            _known.add(v)


def redact(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    with _lock:
        known = sorted(_known, key=len, reverse=True)
    for s in known:
        text = text.replace(s, "****")
    for p in _PATTERNS:
        text = p.sub(lambda m: m.group(1) + "****", text)
    return text


def redact_obj(obj):
    if isinstance(obj, dict):
        return {k: ("****" if re.search(r"(?i)secret|token|password|api_?key|passphrase", str(k)) and v else redact_obj(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def install(logger: logging.Logger | None = None) -> None:
    lg = logger or logging.getLogger()
    for h in lg.handlers or []:
        h.addFilter(RedactingFilter())
    lg.addFilter(RedactingFilter())
