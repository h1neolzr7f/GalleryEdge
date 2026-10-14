"""ge-crawl: run a demo adapter into site-pack-v1 tars."""
from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlsplit

from galleryedge_adapters import ADAPTERS, SyntheticFetcher

from . import CrawlConfig, Crawler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ge-crawl")
    ap.add_argument("--adapter", choices=sorted(ADAPTERS), required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8788", help="demo-selfhost base URL")
    ap.add_argument("--contact", default="", help="your email or URL (goes into the User-Agent)")
    ap.add_argument("--out", default="./var/packs")
    ap.add_argument("--state", default="./var/crawl-state.db")
    ap.add_argument("--min-interval", type=float, default=1.0)
    ap.add_argument("--max-pages", type=int, default=1000)
    ap.add_argument("--pack-size", type=int, default=500)
    ap.add_argument("--panel-db", help="panel DB (file:...) to receive crawler.* events for the ops panel")
    ap.add_argument("--fetch-media", action="store_true", help="also download media (off by default: metadata only)")
    ap.add_argument("--categories", help="wikimedia-commons: comma-separated subset of the built-in SFW allow-list")
    ap.add_argument("--list-categories", action="store_true", help="wikimedia-commons: print the allow-list and exit")
    ap.add_argument("--category-pages", type=int, default=1, help="wikimedia-commons: follow up to N listing pages (200 files each) per category")
    ap.add_argument("--queries", help="openverse: comma-separated search terms (default: built-in neutral list)")
    ap.add_argument("--concurrency", type=int, default=1, help="parallel requests across DIFFERENT hosts (max 1 in flight per host)")
    ap.add_argument("--recrawl", action="store_true", help="incremental: re-fetch seen pages with ETag/Last-Modified, emit only changes")
    ap.add_argument("--since", help="with --recrawl: only pages last seen before this ISO time")
    ap.add_argument("--seed-file", help="crawl exactly these URLs (one per line), e.g. pages skipped under an older filter")
    ap.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
    ap.add_argument("--progress-every", type=int, default=50, help="print a progress line every N pages (stderr)")
    a = ap.parse_args(argv)
    if a.adapter == "synthetic":
        adapter, fetcher, hosts = ADAPTERS["synthetic"](), SyntheticFetcher(), ["demo-source.invalid"]
    elif a.adapter == "wikimedia-commons":
        if a.min_interval < 1.0:
            ap.error("wikimedia-commons: --min-interval must be >= 1.0 (Wikimedia etiquette)")
        from galleryedge_adapters.commons import SFW_CATEGORIES
        if a.list_categories:
            print("\n".join(SFW_CATEGORIES)); return 0
        cats = [c.strip() for c in a.categories.split(",") if c.strip()] if a.categories else list(SFW_CATEGORIES)
        try:
            adapter = ADAPTERS["wikimedia-commons"](cats, max_category_pages=a.category_pages)
        except ValueError as e:
            ap.error(f"{e} (see --list-categories)")
        fetcher, hosts = None, ["commons.wikimedia.org"]
    elif a.adapter == "openverse":
        if a.min_interval < 3.0:
            ap.error("openverse: --min-interval must be >= 3.0 (anonymous limit 20/min, 200/day)")
        qs = [q.strip() for q in a.queries.split(",") if q.strip()] if a.queries else None
        adapter = ADAPTERS["openverse"](qs) if qs else ADAPTERS["openverse"]()
        fetcher, hosts = None, ["api.openverse.org"]
    else:
        adapter, fetcher, hosts = ADAPTERS["demo-selfhost"](a.base), None, [urlsplit(a.base).netloc]
    if a.list_categories:
        from galleryedge_adapters.commons import SFW_CATEGORIES
        print("\n".join(SFW_CATEGORIES)); return 0
    try:
        CrawlConfig(contact=a.contact, allowed_hosts=[]).user_agent()
    except ValueError:
        ap.error("--contact must be a real email address or http(s) URL (it is sent in the User-Agent so site operators can reach you)")
    cfg = CrawlConfig(contact=a.contact, allowed_hosts=hosts, min_interval_s=a.min_interval, max_pages=a.max_pages,
                      fetch_media=a.fetch_media, pack_size=a.pack_size, concurrency=a.concurrency)
    on_event = None
    if a.panel_db:
        from galleryedge_ops import db as pdb
        from galleryedge_ops.events import emit
        conn = pdb.connect(a.panel_db); pdb.migrate(conn)
        on_event = lambda level, code, msg, payload: emit(conn, "crawler", level, code, msg, payload)  # noqa: E731
    c = Crawler(adapter, cfg, state_path=a.state, out_dir=a.out, fetcher=fetcher, on_event=on_event,
                progress=None if a.quiet else (lambda m: print(m, file=sys.stderr, flush=True)), progress_every=a.progress_every)
    if a.seed_file:
        n = c.seed(open(a.seed_file, encoding="utf-8").read().split())
        if not a.quiet:
            print(f"[{adapter.name}] seeded {n} URLs from {a.seed_file}", file=sys.stderr)
    print(json.dumps(c.run_incremental(a.since) if a.recrawl else c.run(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
