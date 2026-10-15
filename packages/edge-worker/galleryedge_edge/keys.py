"""API keys (only SHA-256 hashes stored) + per-minute / per-day quota."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone


def _h(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_plans(name TEXT PRIMARY KEY, rpm INTEGER NOT NULL, rpd INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS api_keys(prefix TEXT PRIMARY KEY, hash TEXT NOT NULL, plan TEXT NOT NULL, label TEXT,
  status TEXT NOT NULL DEFAULT 'active', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS usage_daily(prefix TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(prefix, day));
CREATE TABLE IF NOT EXISTS usage_minute(prefix TEXT NOT NULL, minute TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(prefix, minute));
INSERT OR IGNORE INTO api_plans VALUES ('demo', 30, 1000);
"""


def init_usage(db) -> None:
    """Create only the key/usage tables (for a dedicated writable usage DB)."""
    from galleryedge_db.client import split_sql
    from galleryedge_db.schema import USAGE_GUARDS
    for s in split_sql(USAGE_SCHEMA + USAGE_GUARDS):
        if s.strip():
            db.execute(s.strip())


def prune(db, now: datetime | None = None, keep_days: int = 35) -> None:
    """Keep the usage store small: drop minute buckets older than 1h and day buckets older than keep_days."""
    now = now or datetime.now(timezone.utc)
    from datetime import timedelta
    db.execute("DELETE FROM usage_minute WHERE minute < ?", [(now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")])
    db.execute("DELETE FROM usage_daily WHERE day < ?", [(now - timedelta(days=keep_days)).strftime("%Y-%m-%d")])


def main(argv=None) -> int:
    """ge-keys: manage API keys in the usage DB (token from --token-file; the new key is written to --out, never printed)."""
    import argparse, json, os
    from galleryedge_db.client import connect
    ap = argparse.ArgumentParser(prog="ge-keys")
    ap.add_argument("--db", required=True); ap.add_argument("--token-file")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("init")
    c = sp.add_parser("create"); c.add_argument("--plan", default="demo"); c.add_argument("--label", default=""); c.add_argument("--out", required=True)
    r = sp.add_parser("revoke"); r.add_argument("prefix")
    sp.add_parser("prune")
    a = ap.parse_args(argv)
    tok = None
    if a.token_file:
        raw = open(a.token_file, encoding="utf-8").read().strip()
        tok = (json.loads(raw).get("usage_rw_token") or json.loads(raw).get("rw_token")) if raw.startswith("{") else raw
    db = connect(a.db, tok)
    if a.cmd == "init":
        init_usage(db); print("usage tables ready")
    elif a.cmd == "create":
        key = create_key(db, a.plan, a.label)
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key + "\n")
        print(f"created key prefix {key.split('_')[1]} -> {a.out} (0600)")
    elif a.cmd == "revoke":
        revoke(db, a.prefix); print("revoked")
    else:
        prune(db); print("pruned")
    return 0


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
    from galleryedge_db.client import DBError
    try:
        db.batch([("INSERT INTO usage_daily VALUES (?,?,1) ON CONFLICT(prefix,day) DO UPDATE SET count=count+1", (prefix, day)),
                  ("INSERT INTO usage_minute VALUES (?,?,1) ON CONFLICT(prefix,minute) DO UPDATE SET count=count+1", (prefix, minute))])
    except DBError as exc:
        # Trigger failure rolls the *entire* transactional batch back. A second
        # request with a stale pre-check sees HTTP 429 instead of exceeding quota.
        if "ge_daily_quota" in str(exc):
            return 429, "daily quota exceeded", {"Retry-After": "3600", "X-RateLimit-Remaining-Day": "0"}
        if "ge_minute_quota" in str(exc):
            return 429, "rate limit exceeded", {"Retry-After": str(60 - now.second), "X-RateLimit-Remaining-Day": str(max(0, rpd - used_d))}
        raise
    return 200, "ok", {"X-RateLimit-Remaining-Day": str(rpd - used_d - 1), "X-RateLimit-Remaining-Minute": str(rpm - used_m - 1)}
