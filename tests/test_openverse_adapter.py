import json

from galleryedge_crawl import CrawlConfig, Crawler, Link, Record, Response, Skip
from galleryedge_adapters.openverse import OpenverseAdapter, license_label

ROBOTS_LIVE = b"User-agent: *\nDisallow: /v1/images/\nDisallow: /v1/audio/\n"


def item(**kw):
    d = {"id": "61419a75-885c-4e6b-bb24-28b68fb5411b", "title": "FAKE flowers", "license": "by-sa", "license_version": "2.0",
         "license_url": "https://creativecommons.org/licenses/by-sa/2.0/", "creator": "FAKE Author", "provider": "flickr",
         "url": "https://img.invalid/a.jpg", "foreign_landing_url": "https://src.invalid/p/1", "mature": False,
         "unstable__sensitivity": [], "tags": [{"name": "flower"}], "attribution": "\"FAKE flowers\" by FAKE Author is licensed under CC BY-SA 2.0."}
    d.update(kw); return d


def test_licenses():
    assert license_label("by", "4.0") == "CC BY 4.0" and license_label("cc0", "1.0") == "CC0 1.0"
    assert license_label("by-nc", "2.0") is None and license_label("by-nd", "4.0") is None


def test_record_filters():
    a = OpenverseAdapter()
    r = a.record(item())
    assert isinstance(r, Record) and r.data["license"] == "CC BY-SA 2.0" and "花" in r.data["tags"] and "Openverse" in r.data["attribution"]
    assert a.record(item(license="by-nc")).reason == "license-not-allowed"
    assert a.record(item(mature=True)).reason == "sfw-blocked"
    assert a.record(item(unstable__sensitivity=["user_reported_sensitive"])).reason == "sfw-blocked"
    assert a.record(item(title="Flag of somewhere")).reason == "sensitive-blocked"
    assert a.record(item(url="http://insecure.invalid/a.jpg")).reason == "no-thumbnail"


def test_pagination_bounded():
    a = OpenverseAdapter(["flower"], max_pages_per_query=2)
    u = a.start_urls()[0]
    out = list(a.parse(Response(u, 200, {}, json.dumps({"page_count": 50, "results": [item()]}).encode())))
    assert any(isinstance(x, Link) and "page=2" in x.url for x in out)
    u2 = a.search_url("flower", 2)
    out2 = list(a.parse(Response(u2, 200, {}, json.dumps({"page_count": 50, "results": [item()]}).encode())))
    assert not any(isinstance(x, Link) for x in out2)
    assert isinstance(list(a.parse(Response(u, 200, {}, b"{}")))[0], Skip)


class F:
    ua = "x"
    def __init__(self, robots): self.robots, self.hits = robots, []
    def get(self, url):
        self.hits.append(url)
        if url.endswith("/robots.txt"):
            return Response(url, 200, {}, self.robots)
        return Response(url, 200, {"Content-Type": "application/json"}, json.dumps({"page_count": 1, "results": [item()]}).encode())


def test_live_robots_is_honoured(tmp_path):
    f = F(ROBOTS_LIVE)
    cfg = CrawlConfig(contact="ops@example.invalid", allowed_hosts=["api.openverse.org"], min_interval_s=0)
    res = Crawler(OpenverseAdapter(["flower"]), cfg, state_path=str(tmp_path / "s.db"), out_dir=str(tmp_path / "o"), fetcher=f).run()
    assert all(u.endswith("/robots.txt") for u in f.hits)  # never touched /v1/images/
    assert res["records"] == 0 and res["skip_reasons"].get("robots-disallow") == 1


def test_crawl_when_allowed(tmp_path):
    f = F(b"User-agent: *\nAllow: /\n")
    cfg = CrawlConfig(contact="ops@example.invalid", allowed_hosts=["api.openverse.org"], min_interval_s=0)
    res = Crawler(OpenverseAdapter(["flower"]), cfg, state_path=str(tmp_path / "s.db"), out_dir=str(tmp_path / "o"), fetcher=f).run()
    assert res["records"] == 1
