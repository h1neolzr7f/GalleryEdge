"""Read-only bridge from the existing aitag-nai packs to GalleryEdge site-pack-v1.

* ``inspect_manifest`` checks an ``aitag-nai-site-pack-v1`` MANIFEST.json and reports
  what is missing for site-pack-v1 (the formats differ: aitag packs are a raw
  works/ + images/ tree with no per-record provenance or license).
* ``iter_records`` reads an aitag ``import.sqlite`` strictly read-only
  (``mode=ro&immutable=1`` — never creates -wal/-shm, never writes).
* ``convert`` writes a NEW site-pack-v1 tar (metadata only; images stay in R2).
  It refuses to write next to / over the source, and never modifies originals.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from galleryedge_pack import PackError, validate_record, write_pack

AITAG_FORMAT = "aitag-nai-site-pack-v1"
AITAG_MANIFEST_FIELDS = ("format", "name", "created_at", "work_count", "image_count", "tar", "tar_bytes", "sha256")
LICENSE = "unknown-all-rights-reserved"
PROTECTED_NAMES = {"done.txt", "seen.txt", "packed_works.txt", "prompts.jsonl"}


def inspect_manifest(path: str | Path) -> dict:
    m = json.loads(Path(path).read_text("utf-8"))
    problems = [f"missing {k}" for k in AITAG_MANIFEST_FIELDS if k not in m]
    if m.get("format") != AITAG_FORMAT:
        problems.append(f"unexpected format {m.get('format')!r}")
    if m.get("sha256") and len(str(m["sha256"])) != 64:
        problems.append("sha256 is not 64 hex")
    gaps = ["layout: works/ + images/ tree instead of manifest.json + works.jsonl",
            "no per-record license (records get license=%s)" % LICENSE,
            "no per-record provenance (bridge adds adapter=aitag-bridge, robots=not-applicable)",
            "media lives in R2, not in the tar (site-pack-v1 media/ stays empty)"]
    return {"pack": m.get("name"), "format": m.get("format"), "works": m.get("work_count"), "images": m.get("image_count"),
            "ok": not problems, "problems": problems, "site_pack_v1_gaps": gaps}


def _ro(path: str | Path) -> sqlite3.Connection:
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(p)
    c = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _artist(s: str | None) -> str | None:
    s = (s or "").split(",")[0].strip()
    for pre in ("artist:", "by "):
        if s.lower().startswith(pre):
            s = s[len(pre):].strip()
    return s or None


def to_record(row: dict, pack_id: str, fetched_at: str) -> dict:
    wid = int(row["id"])
    url = f"https://www.pixiv.net/artworks/{wid}"
    try:
        tags = [t["name"] for t in json.loads(row.get("tags_json") or "[]") if t.get("name")]
    except (ValueError, TypeError):
        tags = []
    tags = list(dict.fromkeys(t.strip() for t in tags if t.strip()))[:64]
    sha = row.get("cover_sha256") or hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
    return {
        "id": f"pixiv-{wid}", "title": (row.get("title") or "").strip() or f"pixiv {wid}", "prompt": row.get("prompt") or "",
        "source_url": url, "license": LICENSE,
        "rights_note": f"Pixiv work {wid} (user {row.get('user_id') or '?'}); rights remain with the author. Indexed from {pack_id}.",
        "rating": row.get("rating") or "unknown", "artist": _artist(row.get("artist_names")), "tags": tags,
        "image_count": int(row.get("image_count") or 1), "cover_key": row.get("cover_r2_key"), "created_at": row.get("created_at"),
        "provenance": {"adapter": "aitag-bridge", "fetched_at": fetched_at, "fetch_url": url, "robots": "not-applicable", "content_sha256": sha},
    }


def iter_records(import_sqlite: str | Path, pack_id: str, *, limit: int | None = None, fetched_at: str | None = None):
    fa = fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    c = _ro(import_sqlite)
    try:
        sql = "SELECT * FROM works ORDER BY id" + (" LIMIT ?" if limit else "")
        for r in c.execute(sql, (limit,) if limit else ()):
            yield to_record(dict(r), pack_id, fa)
    finally:
        c.close()


def convert(import_sqlite: str | Path, out: str | Path, pack_id: str, *, limit: int | None = None) -> dict:
    src, out = Path(import_sqlite).resolve(), Path(out).resolve()
    if out.exists():
        raise PackError(f"refusing to overwrite {out}")
    if out.parent == src.parent or out.name in PROTECTED_NAMES:
        raise PackError("output must be written outside the source directory")
    recs, bad = [], 0
    for r in iter_records(src, pack_id, limit=limit):
        if validate_record(r):
            bad += 1
            continue
        recs.append(r)
    man = write_pack(out, f"ge-{pack_id}", "aitag-bridge", recs)
    return {"out": str(out), "records": len(recs), "skipped_invalid": bad, "pack_id": man["pack_id"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ge-aitag", description="read-only bridge: aitag-nai packs -> site-pack-v1")
    sp = ap.add_subparsers(dest="cmd", required=True)
    i = sp.add_parser("inspect"); i.add_argument("manifests", nargs="+")
    c = sp.add_parser("convert"); c.add_argument("import_sqlite"); c.add_argument("--pack-id", required=True); c.add_argument("--out", required=True); c.add_argument("--limit", type=int)
    a = ap.parse_args(argv)
    if a.cmd == "inspect":
        rep = [inspect_manifest(m) for m in a.manifests]
        print(json.dumps({"ok": sum(r["ok"] for r in rep), "total": len(rep), "problems": {r["pack"]: r["problems"] for r in rep if r["problems"]},
                          "site_pack_v1_gaps": rep[0]["site_pack_v1_gaps"] if rep else []}, ensure_ascii=False, indent=1))
        return 0 if all(r["ok"] for r in rep) else 1
    try:
        print(json.dumps(convert(a.import_sqlite, a.out, a.pack_id, limit=a.limit), ensure_ascii=False))
    except (PackError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr); return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
