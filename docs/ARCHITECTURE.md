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
