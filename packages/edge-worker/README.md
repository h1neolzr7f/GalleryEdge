# edge-worker

- `worker/` — Cloudflare Worker (TypeScript). Talks to Turso/libSQL with plain `fetch` over the Hrana v2 HTTP protocol, same schema and routes as the Python reference. Tests (vitest) run against a real local `sqld` / `libsql-server`, seeded by the Python pipeline, and assert byte-for-byte API parity with the Python app.
  `cd worker && npm ci && npm test` · deploy yourself: copy `wrangler.toml.example`, `wrangler secret put TURSO_URL` / `TURSO_TOKEN`. Nothing here is deployed by CI.
- `galleryedge_edge/` — Python WSGI reference (`ge-demo serve`).
