# Architecture / 架构

```
collect (crawler-core + adapters) -> site-pack-v1 -> ingest -> Turso/libSQL (index + FTS)
                                                           -> storage-router (R2 / S3 / local, hot/cold)
edge-worker (Turso over HTTP) -> site / API
panel (127.0.0.1 only): health, alerts, vault, backup/restore, cleanup, config center, audit
```

- **Primary DB: Turso (libSQL).** D1 is optional and only for tiny, rebuildable side data.
- Tests use local SQLite/libSQL files — never real accounts.
- Layout: `packages/*` libraries, `apps/*` demos. Phase 1 code lives in `packages/panel/galleryedge_ops` (Python) and `packages/config-schema`.

## Phase 2 notes
- `packages/db-turso/galleryedge_db`: pure-Python Hrana v2 (`/v2/pipeline`) client — the same protocol a Worker uses — plus a SQLite client with an identical interface. Transactional batches use Hrana conditional steps with rollback.
- `SearchDialect`: `fts5` (verified on SQLite and on `libsql-server` in CI) and `turso-fts` (`CREATE INDEX ... USING fts`; SQL generation only — must be probed on a real empty Turso DB, `detect()` falls back to FTS5).
- `packages/edge-worker/galleryedge_edge`: WSGI reference implementation of the Worker routes (HTML + JSON API, key hash + quota). A JS Worker port against the same schema is a remaining item.
