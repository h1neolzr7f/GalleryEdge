import threading
import time

from galleryedge_crawl import CrawlConfig, Crawler, Link, Record, Response


class Adapter:
    name = "multi"
    def __init__(self, hosts, n): self.hosts, self.n = hosts, n
    def start_urls(self): return [f"https://{h}/list" for h in self.hosts]
    def parse(self, resp):
        if resp.url.endswith("/list"):
            for i in range(self.n):
                yield Link(f"/item/{i}")
        else:
            yield Record({"id": resp.url.replace("https://", "").replace("/", "-"), "title": resp.text, "license": "CC0", "rating": "general", "source_url": resp.url})


class Fetcher:
    ua = "t"
    def __init__(self, body="v1", etag='"e1"'):
        self.lock = threading.Lock(); self.inflight = {}; self.max_per_host = 0; self.max_total = 0; self.total = 0
        self.body, self.etag, self.cond_headers = body, etag, []
    def get(self, url, headers=None):
        h = url.split("/")[2]
        if url.endswith("/robots.txt"):
            return Response(url, 200, {}, b"User-agent: *\nAllow: /\n")
        with self.lock:
            self.inflight[h] = self.inflight.get(h, 0) + 1; self.total += 1
            self.max_per_host = max(self.max_per_host, self.inflight[h]); self.max_total = max(self.max_total, self.total)
        time.sleep(0.01)
        with self.lock:
            self.inflight[h] -= 1; self.total -= 1
        if headers:
            self.cond_headers.append(headers)
            if headers.get("If-None-Match") == self.etag:
                return Response(url, 304, {}, b"")
        return Response(url, 200, {"ETag": self.etag}, self.body.encode())


def cfg(**kw):
    return CrawlConfig(contact="ops@example.invalid", allowed_hosts=["a.invalid", "b.invalid", "c.invalid"], min_interval_s=0, **kw)


def test_parallel_across_hosts_but_one_per_host(tmp_path):
    f = Fetcher()
    res = Crawler(Adapter(["a.invalid", "b.invalid", "c.invalid"], 5), cfg(concurrency=3),
                  state_path=str(tmp_path / "s.db"), out_dir=str(tmp_path / "o"), fetcher=f).run()
    assert res["records"] == 15
    assert f.max_per_host == 1 and f.max_total >= 2


def test_min_interval_still_applies_per_host(tmp_path):
    t = {"now": 0.0}; sleeps = []
    def sleep(d): sleeps.append(d); t["now"] += d
    f = Fetcher()
    Crawler(Adapter(["a.invalid"], 3), cfg(concurrency=4).__class__(contact="ops@example.invalid", allowed_hosts=["a.invalid"], min_interval_s=2.0, concurrency=4),
            state_path=str(tmp_path / "s.db"), out_dir=str(tmp_path / "o"), fetcher=f, clock=lambda: t["now"], sleep=sleep).run()
    assert len(sleeps) >= 4 and all(abs(d - 2.0) < 1e-6 for d in sleeps)


def test_incremental_recrawl_304_and_changes(tmp_path):
    st, out = str(tmp_path / "s.db"), str(tmp_path / "o")
    f = Fetcher()
    c = Crawler(Adapter(["a.invalid"], 3), cfg(), state_path=st, out_dir=out, fetcher=f); assert c.run()["records"] == 3
    # unchanged server: every page answers 304 thanks to If-None-Match
    r2 = Crawler(Adapter(["a.invalid"], 3), cfg(), state_path=st, out_dir=out, fetcher=f).run_incremental()
    assert r2["records"] == 0 and r2["skip_reasons"].get("not-modified") == 4 and f.cond_headers
    # content changed (new ETag): records re-emitted as updates
    f2 = Fetcher(body="v2", etag='"e2"')
    r3 = Crawler(Adapter(["a.invalid"], 3), cfg(), state_path=st, out_dir=out, fetcher=f2).run_incremental()
    assert r3["records"] == 3 and r3.get("updated") == 3


def test_incremental_since_filters(tmp_path):
    st, out = str(tmp_path / "s.db"), str(tmp_path / "o")
    Crawler(Adapter(["a.invalid"], 2), cfg(), state_path=st, out_dir=out, fetcher=Fetcher()).run()
    f = Fetcher()
    r = Crawler(Adapter(["a.invalid"], 2), cfg(), state_path=st, out_dir=out, fetcher=f).run_incremental(since="2000-01-01T00:00:00+00:00")
    assert r["fetched"] == 0  # nothing older than `since`


def test_cli_recrawl_synthetic(tmp_path, capsys):
    import json
    from galleryedge_crawl.cli import main
    args = ["--adapter", "synthetic", "--contact", "ops@example.invalid", "--out", str(tmp_path / "o"), "--state", str(tmp_path / "s.db"),
            "--min-interval", "0", "--quiet"]
    assert main(args) == 0; first = json.loads(capsys.readouterr().out)
    assert main(args + ["--recrawl", "--concurrency", "2"]) == 0; again = json.loads(capsys.readouterr().out)
    assert first["records"] > 0 and again["records"] == 0 and again.get("unchanged", 0) == first["records"]


def test_seed_revisits_previously_skipped(tmp_path):
    st, out = str(tmp_path / "s.db"), str(tmp_path / "o")
    Crawler(Adapter(["a.invalid"], 2), cfg(), state_path=st, out_dir=out, fetcher=Fetcher()).run()
    c = Crawler(Adapter(["a.invalid"], 2), cfg(), state_path=st, out_dir=out, fetcher=Fetcher(body="v9", etag='"z"'))
    c.recrawl = True
    assert c.seed(["https://a.invalid/item/1"]) == 1
    r = c.run()
    assert r["fetched"] == 1 and r["records"] == 1
