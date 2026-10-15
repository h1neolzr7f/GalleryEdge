import json
import tarfile
import threading
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest
from galleryedge_adapters import DemoSelfhostAdapter, SyntheticAdapter, SyntheticFetcher
from galleryedge_crawl import CrawlConfig, Crawler, Response
from galleryedge_db import client as C
from galleryedge_demosource import DemoSource
from galleryedge_edge.app import App
from galleryedge_ingest import ingest_pack
from galleryedge_pack import PackError, read_pack, write_pack


class Quiet(WSGIRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture
def source():
    app = DemoSource(n=45, seed=7)
    srv = make_server("127.0.0.1", 0, app, handler_class=Quiet)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield app, f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def cfg(base, **kw):
    from urllib.parse import urlsplit
    return CrawlConfig(contact="ops@example.com", allowed_hosts=[urlsplit(base).netloc], min_interval_s=0, **kw)


def test_end_to_end_crawl_pack_ingest_search(source, tmp_path):
    app, base = source
    c = Crawler(DemoSelfhostAdapter(base), cfg(base, pack_size=20), state_path=str(tmp_path / "s.db"), out_dir=str(tmp_path / "packs"))
    stats = c.run()
    assert stats["records"] == 45 and len(stats["packs"]) == 3
    paths = [p for p, _ in app.hits]
    assert not any(p.startswith("/private/") for p in paths)          # robots obeyed
    assert not any(p.startswith("/media/") for p in paths)            # metadata-only default
    assert all(ua.startswith("GalleryEdge-Crawler/") and "ops@example.com" in ua for _, ua in app.hits)
    skips = dict(c.st.db.execute("SELECT url, reason FROM skips").fetchall())
    reasons = sorted(skips.values())
    assert reasons == ["login-wall", "login-wall", "no-license", "paywall", "robots-disallow"]
    man, recs, media = read_pack(stats["packs"][0])
    assert man["record_count"] == 20 and media == {}
    r = recs[0]
    assert r["license"] and r["provenance"]["robots"] == "allowed" and r["provenance"]["media_fetched"] is False
    assert len(r["provenance"]["content_sha256"]) == 64
    db = C.SqliteClient(":memory:")
    from galleryedge_db import dialect, schema
    schema.init(db, dialect.detect(db))
    assert sum(ingest_pack(db, p)["records"] for p in stats["packs"]) == 45
    site = App(db)
    w = app.works[3]
    db.execute("UPDATE works SET title='zzcrawled harbor' WHERE id=?", [w["id"]])
    assert [x["id"] for x in site.search(q="zzcrawled")["items"]] == [w["id"]]
    tag = w["tags"][0]
    assert site.search(tag=tag)["total"] == sum(tag in x["tags"] for x in app.works)
    assert "via demo-selfhost" in site.work(w["id"])["rights_note"]


def test_resume_dedupe_and_budget(source, tmp_path):
    app, base = source
    st, out = str(tmp_path / "s.db"), str(tmp_path / "p")
    s1 = Crawler(DemoSelfhostAdapter(base), cfg(base, max_pages=10), state_path=st, out_dir=out).run()
    assert s1["pages_total"] == 10
    n_hits = len(app.hits)
    s2 = Crawler(DemoSelfhostAdapter(base), cfg(base), state_path=st, out_dir=out).run()
    seen = [p for p, _ in app.hits[n_hits:]]
    assert seen[0] == "/robots.txt" and seen[1].startswith("/works/")  # resumed from the frontier, not from the start page
    total = s1["records"] + s2["records"]
    assert total == 45
    ids = [r["id"] for p in s1["packs"] + s2["packs"] for r in read_pack(p)[1]]
    assert len(ids) == len(set(ids)) == 45
    s3 = Crawler(DemoSelfhostAdapter(base), cfg(base), state_path=st, out_dir=out).run()
    assert s3["records"] == 0 and s3["packs"] == []


def test_fetch_media_opt_in(source, tmp_path):
    app, base = source
    stats = Crawler(DemoSelfhostAdapter(base), cfg(base, fetch_media=True, max_pages=6), state_path=str(tmp_path / "s.db"), out_dir=str(tmp_path / "p")).run()
    _, recs, media = read_pack(stats["packs"][0])
    assert media and all(m["name"] in media for r in recs for m in r["media"])


def test_rate_limit_and_crawl_delay(tmp_path):
    t = [0.0]; sleeps = []
    class F:
        ua = "GalleryEdge-Crawler/0.4 (x)"
        def get(self, url):
            if url.endswith("/robots.txt"):
                return Response(url, 200, {}, b"User-agent: *\nCrawl-delay: 5\n")
            return Response(url, 200, {}, b"<x>")
    class A:
        name = "t"
        def start_urls(self):
            return ["https://a.test/1", "https://a.test/2", "https://a.test/3"]
        def parse(self, r):
            return []
    c = Crawler(A(), CrawlConfig(contact="x@example.com", allowed_hosts=["a.test"], min_interval_s=1), state_path=str(tmp_path / "s"), out_dir=str(tmp_path),
                fetcher=F(), clock=lambda: t[0], sleep=lambda d: (sleeps.append(d), t.__setitem__(0, t[0] + d)))
    c.run()
    assert sleeps == [5.0, 5.0, 5.0]  # crawl-delay (5s) beats min interval (1s) after robots and between pages


def test_robots_unreachable_disallows_and_offhost(tmp_path):
    class F:
        ua = "GalleryEdge-Crawler/0.4 (x)"
        def get(self, url):
            if url.endswith("/robots.txt"):
                return Response(url, 503, {}, b"")
            raise AssertionError("must not fetch pages when robots is unreachable")
    class A:
        name = "t"
        def start_urls(self):
            return ["https://a.test/1", "https://elsewhere.test/x", "ftp://a.test/y"]
        def parse(self, r):
            return []
    c = Crawler(A(), CrawlConfig(contact="x@example.com", allowed_hosts=["a.test"], min_interval_s=0), state_path=str(tmp_path / "s"), out_dir=str(tmp_path), fetcher=F())
    c.run()
    assert sorted(r for (r,) in c.st.db.execute("SELECT reason FROM skips")) == ["host-not-allowed", "robots-unreachable", "scheme"]


def test_contact_required(tmp_path):
    with pytest.raises(ValueError):
        Crawler(SyntheticAdapter(3), CrawlConfig(contact="", allowed_hosts=[]), state_path=str(tmp_path / "s"), out_dir=str(tmp_path), fetcher=SyntheticFetcher())


def test_synthetic_adapter_offline(tmp_path):
    s = Crawler(SyntheticAdapter(30), CrawlConfig(contact="x@example.com", allowed_hosts=["demo-source.invalid"], min_interval_s=0, pack_size=50),
                state_path=str(tmp_path / "s"), out_dir=str(tmp_path / "p"), fetcher=SyntheticFetcher()).run()
    assert s["records"] == 30 and len(s["packs"]) == 1


def test_pack_contract_rejects_tampering_and_bad_records(tmp_path):
    rec = {"id": "a1", "title": "t", "source_url": "https://x.test/a1", "license": "CC0-1.0", "rating": "general",
           "provenance": {"adapter": "t", "fetched_at": "now", "fetch_url": "https://x.test/a1", "robots": "allowed", "content_sha256": "0" * 64}}
    p = tmp_path / "p.tar"
    write_pack(p, "t-00001", "t", [rec])
    assert read_pack(p)[0]["record_count"] == 1
    with pytest.raises(PackError):
        write_pack(tmp_path / "q.tar", "t-2", "t", [{**rec, "license": ""}])
    with pytest.raises(PackError):
        write_pack(tmp_path / "q.tar", "t-2", "t", [{**rec, "provenance": {**rec["provenance"], "robots": "ignored"}}])
    bad = tmp_path / "bad.tar"
    with tarfile.open(p) as src, tarfile.open(bad, "w") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read()
            if m.name == "works.jsonl":
                data = data.replace(b'"t"', b'"T"')
            m.size = len(data)
            import io
            dst.addfile(m, io.BytesIO(data))
    with pytest.raises(PackError, match="checksum"):
        read_pack(bad)
    assert not list(tmp_path.glob("*.partial"))


def test_crawler_events_reach_panel_status(source, tmp_path):
    from fastapi.testclient import TestClient
    from galleryedge_ops import db as pdb
    from galleryedge_ops.events import emit
    from galleryedge_panel.server import create_app
    app, base = source
    conn = pdb.connect(":memory:"); pdb.migrate(conn)
    ev = lambda level, code, msg, payload: emit(conn, "crawler", level, code, msg, payload)  # noqa: E731
    Crawler(DemoSelfhostAdapter(base), cfg(base), state_path=str(tmp_path / "s.db"), out_dir=str(tmp_path / "p"), on_event=ev, heartbeat_every=10).run()
    codes = [c for (c,) in conn.execute("SELECT code FROM events WHERE source='crawler' ORDER BY id")]
    assert codes[0] == "crawler.start" and "crawler.heartbeat" in codes and codes[-2:] == ["crawler.skips", "crawler.done"]
    c = {"panel": {"bind": "127.0.0.1", "state_dir": str(tmp_path / "st")}, "alerts": {}, "backends": [], "cleanup": {"roots": []}}
    from galleryedge_ops import auth
    auth.create_user(conn, "admin", "FAKE-owner-pass-9876", "admin")
    tc = TestClient(create_app(c, conn))
    tc.post("/api/auth/login", headers={"X-GE-Panel": "1"}, json={"username": "admin", "password": "FAKE-owner-pass-9876"})
    st = tc.get("/api/status").json()["crawler"]
    assert st["last_code"] == "crawler.done" and st["stats"]["records"] == 45
    assert st["skip_reasons"] == {"login-wall": 2, "no-license": 1, "paywall": 1, "robots-disallow": 1}


def test_crawler_limits_error_response_bytes():
    import io
    import urllib.error
    from galleryedge_crawl import HttpFetcher
    class FakeOpener:
        def open(self, req, timeout):
            raise urllib.error.HTTPError(req.full_url, 429, "too many", {}, io.BytesIO(b"x" * 5_000_100))
    r = HttpFetcher("GalleryEdge-test (ops@example.com)", opener=FakeOpener()).get("https://demo.invalid/fail")
    assert r.status == 429 and len(r.body) == 5_000_000


def test_redirect_uses_destination_host_rate_limit(tmp_path):
    from galleryedge_crawl import Skip
    t = [0.0]
    waits = []
    class F:
        ua = "GalleryEdge-Crawler (ops@example.com)"
        def get(self, url, headers=None):
            if url.endswith("/robots.txt"):
                return Response(url, 200, {}, b"User-agent: *\nDisallow:\n")
            if url == "https://a.test/item":
                return Response(url, 302, {"Location": "https://b.test/item"}, b"")
            return Response(url, 200, {}, b"ok")
    class A:
        name = "redirects"
        def start_urls(self): return []
        def parse(self, resp): return []
    crawler = Crawler(A(), CrawlConfig(contact="ops@example.com", allowed_hosts=["a.test", "b.test"],
                                       min_interval_s=1), state_path=str(tmp_path / "state.db"),
                      out_dir=str(tmp_path / "packs"), fetcher=F(),
                      clock=lambda: t[0],
                      sleep=lambda seconds: (waits.append(seconds), t.__setitem__(0, t[0]+seconds)))
    r = crawler._fetch("https://a.test/item")
    assert isinstance(r, Response) and r.url == "https://b.test/item"
    assert t[0] >= 2.0  # a.test and b.test must each respect interval after robots
