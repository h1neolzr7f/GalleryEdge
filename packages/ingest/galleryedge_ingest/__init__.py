"""Ingest site-pack-v1 packs into the index DB (Turso/libSQL or SQLite)."""
from __future__ import annotations

import argparse
import os
import sys

from galleryedge_db import client as C
from galleryedge_db import dialect, schema
from galleryedge_pack import PackError, read_pack


def ingest_pack(db, path, stats: dict | None = None) -> dict:
    man, records, _media = read_pack(path, include_media=False)  # stream + verify media without loading it
    rows = []
    for r in records:
        prov = r["provenance"]
        rows.append({**r, "rights_note": r.get("rights_note") or f"via {prov['adapter']} at {prov['fetched_at']}", "pack_id": man["pack_id"]})
    st = {} if stats is None else stats
    schema.ingest(db, rows, stats=st)
    return {"pack_id": man["pack_id"], "records": len(rows), "rows_written": st.get("rows_written", 0)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ge-ingest", description="verify and ingest site-pack-v1 tars")
    ap.add_argument("--db", default="file:./var/demo.db")
    ap.add_argument("--panel-db", help="panel DB (file:...) to receive Turso row metering + ingest events")
    ap.add_argument("--table-prefix", default=os.environ.get("GE_TABLE_PREFIX", ""), help="namespace tables, e.g. ge_ (for a shared DB)")
    ap.add_argument("--token-file", help="read the libSQL auth token from this file (or a JSON file's rw_token/auth_token) instead of $GE_LIBSQL_TOKEN")
    ap.add_argument("--refresh-search-text", action="store_true", help="recompute search_text for existing rows (e.g. after installing jieba) and exit")
    ap.add_argument("packs", nargs="*")
    a = ap.parse_args(argv)
    token = os.environ.get("GE_LIBSQL_TOKEN")
    if a.token_file:
        raw = open(a.token_file, encoding="utf-8").read().strip()
        if raw.startswith("{"):
            import json as _j
            d = _j.loads(raw); token = d.get("rw_token") or d.get("auth_token")
        else:
            token = raw
    db = C.connect(a.db, token, prefix=a.table_prefix)
    schema.init(db, dialect.detect(db))
    if a.refresh_search_text:
        print(f"search_text refreshed on {schema.refresh_search_text(db)} rows"); return 0
    if not a.packs:
        ap.error("no packs given")
    conn = None
    if a.panel_db:
        from galleryedge_ops import db as pdb
        conn = pdb.connect(a.panel_db); pdb.migrate(conn)
    rc = 0
    for p in a.packs:
        st: dict = {}
        try:
            res = ingest_pack(db, p, st)
            print(res)
            if conn is not None:
                from galleryedge_ops.events import emit
                from galleryedge_storage import add_to_monthly_counter
                add_to_monthly_counter(conn, "turso.rows_written", st.get("rows_written", 0))
                if st.get("rows_read"):
                    add_to_monthly_counter(conn, "turso.rows_read", st["rows_read"])
                emit(conn, "ingest", "info", "ingest.pack", f"{res['pack_id']}: {res['records']} records, {res['rows_written']} rows written", res)
        except PackError as e:
            print(f"{p}: rejected: {e}", file=sys.stderr); rc = 2
            if conn is not None:
                from galleryedge_ops.events import emit
                emit(conn, "ingest", "error", "ingest.rejected", f"{p}: {e}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
