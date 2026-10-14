"""Structured events and audit log (always redacted)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .masking import redact, redact_obj


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def emit(conn, source: str, level: str, code: str, message: str, payload: dict | None = None) -> None:
    conn.execute("INSERT INTO events(ts,source,level,code,message,payload) VALUES (?,?,?,?,?,?)",
                 (now(), source, level, code, redact(message), json.dumps(redact_obj(payload or {}), ensure_ascii=False)))
    conn.commit()


def audit(conn, actor: str, action: str, change: dict | None = None) -> None:
    conn.execute("INSERT INTO audit(ts,actor,action,change) VALUES (?,?,?,?)",
                 (now(), actor, action, json.dumps(redact_obj(change or {}), ensure_ascii=False)))
    conn.commit()


def recent(conn, table: str = "events", limit: int = 50) -> list[tuple]:
    if table not in ("events", "audit"):
        raise ValueError(table)
    return conn.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
