"""Ingest site-pack-v1 packs into the index DB (Turso/libSQL or SQLite)."""
from __future__ import annotations

import argparse
import os
import sys

from galleryedge_db import client as C
from galleryedge_db import dialect, schema
from galleryedge_pack import PackError, read_pack


def ingest_pack(db, path) -> dict:
    man, records, _media = read_pack(path)  # verifies checksums + every record first
    rows = []
    for r in records:
        prov = r["provenance"]
        rows.append({**r, "rights_note": r.get("rights_note") or f"via {prov['adapter']} at {prov['fetched_at']}", "pack_id": man["pack_id"]})
    schema.ingest(db, rows)
    return {"pack_id": man["pack_id"], "records": len(rows)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ge-ingest", description="verify and ingest site-pack-v1 tars")
    ap.add_argument("--db", default="file:./var/demo.db")
    ap.add_argument("packs", nargs="+")
    a = ap.parse_args(argv)
    db = C.connect(a.db, os.environ.get("GE_LIBSQL_TOKEN"))
    schema.init(db, dialect.detect(db))
    rc = 0
    for p in a.packs:
        try:
            print(ingest_pack(db, p))
        except PackError as e:
            print(f"{p}: rejected: {e}", file=sys.stderr); rc = 2
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
