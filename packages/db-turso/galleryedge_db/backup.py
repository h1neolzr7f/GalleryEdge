"""ge-db-backup: logical dump/restore of a GalleryEdge index DB (libSQL/Turso or sqlite).

dump: reads every GalleryEdge table (read-only token is enough) into a gzip'd JSONL file with a manifest
      (table, columns, row count, sha256). FTS shadow tables are skipped; they are rebuilt on restore.
restore: only into an EMPTY target (refuses if `works` has rows unless --force-into-empty-check is disabled);
      re-creates schema, inserts rows in batches, rebuilds the FTS index, verifies row counts.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from pathlib import Path

from . import dialect as D, schema
from .client import connect

TABLES = ("works", "work_tags", "meta")
BATCH = 200


def dump(db, out: str | Path, tables=TABLES) -> dict:
    """Stream the DB into gzip in bounded batches instead of materializing every row."""
    if not set(tables).issubset(TABLES):
        raise ValueError("unknown table in dump request")
    out = Path(out)
    man = {"format": "galleryedge-db-dump-v1", "tables": {}}
    h = hashlib.sha256()
    with gzip.open(out, "wt", encoding="utf-8") as f:
        for t in tables:
            last = 0
            count = 0
            while True:
                # All GalleryEdge logical tables have ordinary SQLite rowids.
                # Keyset pagination avoids O(N) OFFSET scans on large datasets.
                r = db.execute(f"SELECT rowid, * FROM {t} WHERE rowid>? ORDER BY rowid LIMIT ?", [last, BATCH])
                if t not in man["tables"]:
                    man["tables"][t] = {"columns": r.columns[1:], "rows": 0}
                for row in r.rows:
                    last = int(row[0])
                    line = json.dumps({"t": t, "v": list(row[1:])}, ensure_ascii=False, default=str)
                    h.update(line.encode())
                    f.write(line + "\n")
                    count += 1
                if len(r.rows) < BATCH:
                    break
            man["tables"][t]["rows"] = count
    man["sha256"] = h.hexdigest()
    Path(str(out) + ".manifest.json").write_text(json.dumps(man, indent=1))
    return man


def _verify_dump(src: Path, man: dict) -> None:
    """Check format, table/column identifiers, checksum and row counts *before* writing."""
    if man.get("format") != "galleryedge-db-dump-v1":
        raise ValueError("unknown dump format")
    tables = man.get("tables")
    if not isinstance(tables, dict) or not tables or not set(tables).issubset(TABLES):
        raise ValueError("invalid dump table list")
    for name, spec in tables.items():
        cols = spec.get("columns")
        if (not isinstance(cols, list) or not cols or
                any(not isinstance(c, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", c) for c in cols) or
                len(set(cols)) != len(cols) or
                type(spec.get("rows")) is not int or spec["rows"] < 0):
            raise ValueError(f"invalid dump columns/count for {name}")
    counts = {name: 0 for name in tables}
    h = hashlib.sha256()
    with gzip.open(src, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            h.update(line.encode())
            item = json.loads(line)
            t = item.get("t")
            if t not in tables or not isinstance(item.get("v"), list) or len(item["v"]) != len(tables[t]["columns"]):
                raise ValueError("invalid dump row")
            counts[t] += 1
    if h.hexdigest() != man.get("sha256"):
        raise ValueError("dump checksum mismatch")
    if any(counts[t] != tables[t]["rows"] for t in tables):
        raise ValueError("dump row count mismatch")


def restore(db, src: str | Path) -> dict:
    src = Path(src)
    man = json.loads(Path(str(src) + ".manifest.json").read_text())
    _verify_dump(src, man)
    d = D.detect(db)
    schema.init(db, d)
    if any(db.execute(f"SELECT count(*) FROM {t}").rows[0][0] for t in ("works", "work_tags")):
        raise ValueError("target DB is not empty; restore only into an empty database")
    h = hashlib.sha256(); pending: dict[str, list] = {}
    def flush(t):
        rows = pending.pop(t, [])
        if rows:
            cols = man["tables"][t]["columns"]
            verb = "INSERT OR REPLACE" if t == "meta" else "INSERT"
            db.batch([(f"{verb} INTO {t}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", tuple(r)) for r in rows])
    with gzip.open(src, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n"); h.update(line.encode())
            o = json.loads(line)
            pending.setdefault(o["t"], []).append(o["v"])
            if len(pending[o["t"]]) >= BATCH:
                flush(o["t"])
    for t in list(pending):
        flush(t)
    if h.hexdigest() != man["sha256"]:
        raise ValueError("dump checksum mismatch")
    if d.name == "fts5":
        schema.rebuild_fts(db, d)
    counts = {t: db.execute(f"SELECT count(*) FROM {t}").rows[0][0] for t in man["tables"]}
    bad = {t: (counts[t], man["tables"][t]["rows"]) for t in counts if t != "meta" and counts[t] != man["tables"][t]["rows"]}
    if bad:
        raise ValueError(f"row count mismatch: {bad}")
    return {"restored": counts}


def _tok(path):
    if not path:
        return None
    raw = Path(path).read_text().strip()
    return (json.loads(raw).get("ro_token") or json.loads(raw).get("rw_token")) if raw.startswith("{") else raw


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="ge-db-backup")
    sp = ap.add_subparsers(dest="cmd", required=True)
    d = sp.add_parser("dump"); d.add_argument("--db", required=True); d.add_argument("--token-file"); d.add_argument("--out", required=True); d.add_argument("--table-prefix", default="")
    r = sp.add_parser("restore"); r.add_argument("--db", required=True); r.add_argument("--token-file", help="needs a WRITE token for the target"); r.add_argument("--src", required=True); r.add_argument("--table-prefix", default="")
    a = ap.parse_args(argv)
    if a.cmd == "dump":
        print(json.dumps(dump(connect(a.db, _tok(a.token_file), prefix=a.table_prefix), a.out)["tables"]))
    else:
        raw = Path(a.token_file).read_text().strip() if a.token_file else ""
        tok = (json.loads(raw).get("rw_token") if raw.startswith("{") else raw) or None
        print(json.dumps(restore(connect(a.db, tok, prefix=a.table_prefix), a.src)))
    return 0
