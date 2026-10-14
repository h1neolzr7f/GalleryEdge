"""API keys (only SHA-256 hashes stored) + per-minute / per-day quota."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone


def _h(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_key(db, plan: str = "demo", label: str = "") -> str:
    if not db.execute("SELECT 1 FROM api_plans WHERE name=?", [plan]).rows:
        raise ValueError(f"unknown plan {plan}")
    prefix = secrets.token_hex(4)
    key = f"ge_{prefix}_{secrets.token_urlsafe(24)}"
    db.execute("INSERT INTO api_keys(prefix,hash,plan,label) VALUES (?,?,?,?)", [prefix, _h(key), plan, label])
    return key


def revoke(db, prefix: str) -> None:
    db.execute("UPDATE api_keys SET status='revoked' WHERE prefix=?", [prefix])


def check(db, key: str | None, now: datetime | None = None) -> tuple[int, str, dict]:
    """Return (http_status, reason, headers). Counts the request when allowed."""
    if not key or not key.startswith("ge_") or key.count("_") < 2:
        return 401, "missing or malformed API key", {}
    prefix = key.split("_", 2)[1]
    r = db.execute("SELECT k.hash,k.status,p.rpm,p.rpd FROM api_keys k JOIN api_plans p ON p.name=k.plan WHERE k.prefix=?", [prefix]).rows
    if not r or not hmac.compare_digest(r[0][0], _h(key)):
        return 401, "invalid API key", {}
    _, status, rpm, rpd = r[0]
    if status != "active":
        return 403, "API key revoked", {}
    now = now or datetime.now(timezone.utc)
    day, minute = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%dT%H:%M")
    d = db.execute("SELECT count FROM usage_daily WHERE prefix=? AND day=?", [prefix, day]).rows
    m = db.execute("SELECT count FROM usage_minute WHERE prefix=? AND minute=?", [prefix, minute]).rows
    used_d, used_m = (d[0][0] if d else 0), (m[0][0] if m else 0)
    if used_d >= rpd:
        return 429, "daily quota exceeded", {"Retry-After": "3600", "X-RateLimit-Remaining-Day": "0"}
    if used_m >= rpm:
        return 429, "rate limit exceeded", {"Retry-After": str(60 - now.second), "X-RateLimit-Remaining-Day": str(rpd - used_d)}
    db.batch([("INSERT INTO usage_daily VALUES (?,?,1) ON CONFLICT(prefix,day) DO UPDATE SET count=count+1", (prefix, day)),
              ("INSERT INTO usage_minute VALUES (?,?,1) ON CONFLICT(prefix,minute) DO UPDATE SET count=count+1", (prefix, minute))])
    return 200, "ok", {"X-RateLimit-Remaining-Day": str(rpd - used_d - 1), "X-RateLimit-Remaining-Minute": str(rpm - used_m - 1)}
