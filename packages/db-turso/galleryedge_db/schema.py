"""Index schema (Turso primary). Media bytes never live in SQL rows."""
from __future__ import annotations

import json

from .client import split_sql
from .text import search_text

BASE = """
CREATE TABLE IF NOT EXISTS works(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, prompt TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL,
  license TEXT NOT NULL, rights_note TEXT, rating TEXT NOT NULL, artist TEXT, tags_text TEXT NOT NULL DEFAULT '',
  image_count INTEGER NOT NULL DEFAULT 1, cover_key TEXT, created_at TEXT, retrieved_at TEXT, pack_id TEXT,
  synthetic INTEGER NOT NULL DEFAULT 0, search_text TEXT NOT NULL DEFAULT '',
  author TEXT, license_url TEXT, attribution TEXT, cover_url TEXT);
CREATE TABLE IF NOT EXISTS work_tags(work_id TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY(work_id, tag));
CREATE INDEX IF NOT EXISTS idx_work_tags_tag ON work_tags(tag);
CREATE INDEX IF NOT EXISTS idx_works_artist ON works(artist);
CREATE TABLE IF NOT EXISTS api_plans(name TEXT PRIMARY KEY, rpm INTEGER NOT NULL, rpd INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS api_keys(prefix TEXT PRIMARY KEY, hash TEXT NOT NULL, plan TEXT NOT NULL, label TEXT,
  status TEXT NOT NULL DEFAULT 'active', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS usage_daily(prefix TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(prefix, day));
CREATE TABLE IF NOT EXISTS usage_minute(prefix TEXT NOT NULL, minute TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(prefix, minute));
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
INSERT OR IGNORE INTO api_plans VALUES ('demo', 30, 1000);
"""


# Enforced inside the database transaction, not after a racy SELECT.
# Each INSERT/UPSERT is subject to these guards, including concurrent requests.
USAGE_GUARDS = """
CREATE TRIGGER IF NOT EXISTS quota_daily_insert BEFORE INSERT ON usage_daily
WHEN EXISTS (SELECT 1 FROM api_keys WHERE prefix=NEW.prefix) AND
 NEW.count > (SELECT p.rpd FROM api_keys k JOIN api_plans p ON k.plan=p.name WHERE k.prefix=NEW.prefix)
BEGIN SELECT RAISE(ABORT, 'ge_daily_quota'); END;
CREATE TRIGGER IF NOT EXISTS quota_daily_update BEFORE UPDATE ON usage_daily
WHEN EXISTS (SELECT 1 FROM api_keys WHERE prefix=NEW.prefix) AND
 NEW.count > (SELECT p.rpd FROM api_keys k JOIN api_plans p ON k.plan=p.name WHERE k.prefix=NEW.prefix)
BEGIN SELECT RAISE(ABORT, 'ge_daily_quota'); END;
CREATE TRIGGER IF NOT EXISTS quota_minute_insert BEFORE INSERT ON usage_minute
WHEN EXISTS (SELECT 1 FROM api_keys WHERE prefix=NEW.prefix) AND
 NEW.count > (SELECT p.rpm FROM api_keys k JOIN api_plans p ON k.plan=p.name WHERE k.prefix=NEW.prefix)
BEGIN SELECT RAISE(ABORT, 'ge_minute_quota'); END;
CREATE TRIGGER IF NOT EXISTS quota_minute_update BEFORE UPDATE ON usage_minute
WHEN EXISTS (SELECT 1 FROM api_keys WHERE prefix=NEW.prefix) AND
 NEW.count > (SELECT p.rpm FROM api_keys k JOIN api_plans p ON k.plan=p.name WHERE k.prefix=NEW.prefix)
BEGIN SELECT RAISE(ABORT, 'ge_minute_quota'); END;
"""

def init(client, dialect) -> None:
    for s in split_sql(BASE + USAGE_GUARDS + dialect.ddl()):
        if s.strip():
            client.execute(s.strip())
    client.execute("INSERT OR REPLACE INTO meta VALUES ('fts_dialect', ?)", [dialect.name])


def rebuild_fts(client, dialect) -> int:
    """Drop and recreate the FTS index + triggers from the current DDL, then re-index all rows.
    Used to migrate an existing DB when the FTS definition changes (the works table is untouched)."""
    if dialect.name != "fts5":
        raise ValueError("rebuild_fts supports the fts5 dialect only")
    for t in ("works_ai", "works_ad", "works_au"):
        client.execute(f"DROP TRIGGER IF EXISTS {t}")
    client.execute("DROP TABLE IF EXISTS works_fts")
    for s in split_sql(dialect.ddl()):
        if s.strip():
            client.execute(s.strip())
    client.execute("INSERT INTO works_fts(works_fts) VALUES ('rebuild')")
    return client.execute("SELECT count(*) FROM works_fts").rows[0][0]


def ingest(client, works: list[dict], batch_size: int = 200, stats: dict | None = None) -> int:
    """Upsert works in transactional batches (one pipeline round-trip per batch).
    ``stats`` (optional) accumulates rows_written / rows_read as reported by the server
    (Turso/libSQL); for local SQLite it falls back to the number of statements."""
    n = 0
    for i in range(0, len(works), batch_size):
        items = []
        for w in works[i:i + batch_size]:
            for k in ("id", "title", "source_url", "license", "rating"):
                if not w.get(k):
                    raise ValueError(f"work {w.get('id')!r} missing required field {k}")
            count = w.get("image_count", 1)
            if type(count) is not int or not 0 <= count <= 1_000_000:
                raise ValueError(f"work {w.get('id')!r} invalid image_count")
            items.append(("DELETE FROM works WHERE id=?", (w["id"],)))
            items.append(("DELETE FROM work_tags WHERE work_id=?", (w["id"],)))
            items.append(("INSERT INTO works(id,title,prompt,source_url,license,rights_note,rating,artist,tags_text,image_count,cover_key,created_at,retrieved_at,pack_id,synthetic,search_text,author,license_url,attribution,cover_url)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (w["id"], w["title"], w.get("prompt", ""), w["source_url"], w["license"], w.get("rights_note"), w["rating"],
                           w.get("artist"), " | ".join(w.get("tags", [])), w.get("image_count", 1), w.get("cover_key"),
                           w.get("created_at"), w.get("retrieved_at"), w.get("pack_id"), int(bool(w.get("synthetic"))), search_text(w),
                           w.get("author"), w.get("license_url"), w.get("attribution"), w.get("cover_url"))))
            for t in w.get("tags", []):
                items.append(("INSERT OR IGNORE INTO work_tags VALUES (?,?)", (w["id"], t)))
        res = client.batch(items)
        if stats is not None:
            rw = sum(r.rows_written for r in res)
            stats["rows_written"] = stats.get("rows_written", 0) + (rw if rw else len(items))
            stats["rows_read"] = stats.get("rows_read", 0) + sum(r.rows_read for r in res)
        n += len(items) and len(works[i:i + batch_size])
    return n


def load_jsonl(path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def refresh_search_text(client, batch: int = 100) -> int:
    """Recompute works.search_text (e.g. after enabling jieba); only changed rows are written (FTS follows via trigger)."""
    from .text import search_text
    rows = client.execute("SELECT id, title, prompt, tags_text, artist, search_text FROM works").rows
    todo = []
    for rid, title, prompt, tags_text, artist, old in rows:
        tags = [t for t in (tags_text or "").split(" | ") if t] if " | " in (tags_text or "") else None
        w = {"title": title, "prompt": prompt, "artist": artist, **({"tags": tags} if tags is not None else {"tags_text": tags_text})}
        new = search_text(w)
        if new != old:
            todo.append(("UPDATE works SET search_text=? WHERE id=?", (new, rid)))
    for i in range(0, len(todo), batch):
        client.batch(todo[i:i + batch])
    return len(todo)
