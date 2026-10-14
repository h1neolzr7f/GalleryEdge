"""ge-demo: build a synthetic demo DB and serve the edge app locally."""
from __future__ import annotations

import argparse
import os
import sys
from wsgiref.simple_server import make_server

from galleryedge_db import client as C
from galleryedge_db import dialect, schema
from galleryedge_demo.generate import generate

from . import keys
from .app import App


def build(url: str, n: int, seed: int, token: str | None = None):
    db = C.connect(url, token)
    d = dialect.detect(db)
    schema.init(db, d)
    schema.ingest(db, generate(n, seed))
    return db, d


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ge-demo")
    ap.add_argument("--db", default="file:./var/demo.db", help="file:path, :memory:, http://127.0.0.1:8080 (sqld) or libsql://...")
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("seed"); s.add_argument("-n", type=int, default=500); s.add_argument("--seed", type=int, default=42)
    sv = sp.add_parser("serve"); sv.add_argument("--host", default="127.0.0.1"); sv.add_argument("--port", type=int, default=8787)
    k = sp.add_parser("key"); k.add_argument("--plan", default="demo"); k.add_argument("--label", default="")
    a = ap.parse_args(argv)
    token = os.environ.get("GE_LIBSQL_TOKEN")  # never on the command line
    if a.db.startswith("file:"):
        os.makedirs(os.path.dirname(os.path.abspath(a.db[5:])), exist_ok=True)
    if a.cmd == "seed":
        _, d = build(a.db, a.n, a.seed, token)
        print(f"seeded {a.n} synthetic works into {a.db.split('?')[0]} (fts={d.name})")
        return 0
    db = C.connect(a.db, token)
    if a.cmd == "key":
        key = keys.create_key(db, a.plan, a.label)
        print("new API key (shown once, only its hash is stored):", file=sys.stderr)
        print(key)
        return 0
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: serving on a non-local interface", file=sys.stderr)
    with make_server(a.host, a.port, App(db)) as srv:
        print(f"GalleryEdge demo on http://{a.host}:{a.port}  (Ctrl+C to stop)")
        srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
