# site-pack-v1

Uncompressed tar, written atomically (`.partial` then rename):

| member | content |
|---|---|
| `manifest.json` | `format`, `pack_id`, `adapter`, `record_count`, `created_at`, `files{name:{sha256,size}}` |
| `works.jsonl` | one record per line |
| `media/<id>/<n>.<ext>` | only when crawled with `--fetch-media` |

Record required fields: `id`, `title`, `source_url` (http/s), `license`, `rating`, and
`provenance{adapter, fetched_at, fetch_url, robots: "allowed"|"not-applicable", content_sha256}`.
Records without a license are not collected. `ge-ingest` re-verifies every checksum and record before writing.
Implementation + validator: `packages/pack-format/galleryedge_pack`.
