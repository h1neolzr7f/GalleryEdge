"""ge-crawl: run a demo adapter into site-pack-v1 tars."""
from __future__ import annotations

import argparse
import json
from urllib.parse import urlsplit

from galleryedge_adapters import ADAPTERS, SyntheticFetcher

from . import CrawlConfig, Crawler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ge-crawl")
    ap.add_argument("--adapter", choices=sorted(ADAPTERS), required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8788", help="demo-selfhost base URL")
    ap.add_argument("--contact", required=True, help="your email or URL (goes into the User-Agent)")
    ap.add_argument("--out", default="./var/packs")
    ap.add_argument("--state", default="./var/crawl-state.db")
    ap.add_argument("--min-interval", type=float, default=1.0)
    ap.add_argument("--max-pages", type=int, default=1000)
    ap.add_argument("--pack-size", type=int, default=500)
    ap.add_argument("--panel-db", help="panel DB (file:...) to receive crawler.* events for the ops panel")
    ap.add_argument("--fetch-media", action="store_true", help="also download media (off by default: metadata only)")
    a = ap.parse_args(argv)
    if a.adapter == "synthetic":
        adapter, fetcher, hosts = ADAPTERS["synthetic"](), SyntheticFetcher(), ["demo-source.invalid"]
    else:
        adapter, fetcher, hosts = ADAPTERS["demo-selfhost"](a.base), None, [urlsplit(a.base).netloc]
    cfg = CrawlConfig(contact=a.contact, allowed_hosts=hosts, min_interval_s=a.min_interval, max_pages=a.max_pages,
                      fetch_media=a.fetch_media, pack_size=a.pack_size)
    on_event = None
    if a.panel_db:
        from galleryedge_ops import db as pdb
        from galleryedge_ops.events import emit
        conn = pdb.connect(a.panel_db); pdb.migrate(conn)
        on_event = lambda level, code, msg, payload: emit(conn, "crawler", level, code, msg, payload)  # noqa: E731
    print(json.dumps(Crawler(adapter, cfg, state_path=a.state, out_dir=a.out, fetcher=fetcher, on_event=on_event).run(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
