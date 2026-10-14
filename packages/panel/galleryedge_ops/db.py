"""Panel DB. libSQL/Turso compatible SQL; local sqlite file for dev/tests.

URLs: ``file:/path.db``, a plain path, ``:memory:``, or ``libsql://...`` (needs the
optional ``libsql-experimental`` package and an auth token from the vault).
"""
from __future__ import annotations

import sqlite3

MIGRATIONS: list[tuple[int, str]] = [
    (1, """
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT NOT NULL, source TEXT NOT NULL,
        level TEXT NOT NULL, code TEXT NOT NULL, message TEXT NOT NULL, payload TEXT);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL,
        action TEXT NOT NULL, change TEXT);
    CREATE TABLE IF NOT EXISTS backend_status(id TEXT PRIMARY KEY, type TEXT, status TEXT,
        detail TEXT, last_probe_at TEXT, fail_count INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, ts TEXT NOT NULL, key TEXT NOT NULL,
        level TEXT NOT NULL, message TEXT NOT NULL, acked INTEGER DEFAULT 0, delivered TEXT);
    CREATE INDEX IF NOT EXISTS idx_alerts_key ON alerts(key, ts);
    """),
    (2, """
    CREATE TABLE IF NOT EXISTS packs(pack_id TEXT PRIMARY KEY, stage TEXT NOT NULL, status TEXT NOT NULL,
        progress REAL NOT NULL DEFAULT 0, backend_id TEXT, bytes_local INTEGER DEFAULT 0, bytes_remote INTEGER DEFAULT 0,
        checksum TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0, checkpoint_json TEXT,
        started_at TEXT, updated_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_packs_status ON packs(status);
    """),
]


def connect(url: str, auth_token: str | None = None):
    if url.startswith(("libsql://", "https://", "wss://")):
        try:
            import libsql_experimental as libsql  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("install galleryedge[libsql] for remote Turso URLs") from e
        return libsql.connect(database=url, auth_token=auth_token or "")
    path = url[5:] if url.startswith("file:") else url
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL") if path != ":memory:" else None
    return conn


def migrate(conn) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    for v, sql in MIGRATIONS:
        if v not in done:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (v,))
    conn.commit()
    return max(v for v, _ in MIGRATIONS)
