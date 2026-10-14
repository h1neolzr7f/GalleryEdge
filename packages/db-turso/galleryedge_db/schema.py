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


def init(client, dialect) -> None:
    for s in split_sql(BASE + dialect.ddl()):
        if s.strip():
            client.execute(s.strip())
    client.execute("INSERT OR REPLACE INTO meta VALUES ('fts_dialect', ?)", [dialect.name])


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
            items.append(("DELETE FROM works WHERE id=?", (w["id"],)))
            items.append(("DELETE FROM work_tags WHERE work_id=?", (w["id"],)))
            items.append(("INSERT INTO works(id,title,prompt,source_url,license,rights_note,rating,artist,tags_text,image_count,cover_key,created_at,retrieved_at,pack_id,synthetic,search_text,author,license_url,attribution,cover_url)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (w["id"], w["title"], w.get("prompt", ""), w["source_url"], w["license"], w.get("rights_note"), w["rating"],
                           w.get("artist"), " ".join(w.get("tags", [])), w.get("image_count", 1), w.get("cover_key"),
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
