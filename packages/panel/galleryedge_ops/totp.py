"""RFC 6238 TOTP (SHA-1, 30 s, 6 digits) — stdlib only, compatible with Google Authenticator / 1Password / Aegis."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP, DIGITS = 30, 6


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret: str, step: int) -> str:
    d = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    o = d[-1] & 0x0F
    return str((struct.unpack(">I", d[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** DIGITS).zfill(DIGITS)


def now_step(t: float | None = None) -> int:
    return int((time.time() if t is None else t) // STEP)


def verify(secret: str, code: str, *, t: float | None = None, window: int = 1, last_step: int = -1) -> int | None:
    """Return the matched time step (to store as last_step => no replay), or None."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    cur = now_step(t)
    for st in range(cur - window, cur + window + 1):
        if st > last_step and hmac.compare_digest(code_at(secret, st), code):
            return st
    return None


def uri(secret: str, username: str, issuer: str = "GalleryEdge") -> str:
    return f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret}&issuer={quote(issuer)}&period={STEP}&digits={DIGITS}"
