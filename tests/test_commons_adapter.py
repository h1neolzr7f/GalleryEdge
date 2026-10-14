"""Wikimedia Commons adapter: synthetic HTML fixtures shaped like Commons pages (no real content)."""
import pytest
from galleryedge_adapters.commons import CommonsAdapter, thumb_url
from galleryedge_crawl import CrawlConfig, Crawler, Link, Record, Response, Skip
from galleryedge_pack import read_pack

B = "https://commons.wikimedia.org"
THUMB = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/FAKE_Lake.jpg/1280px-FAKE_Lake.jpg?utm_source=x"


def page(lic="CC BY-SA 4.0", cats=("Lakes of Nowhere", "Featured pictures of landscapes"), desc="A FAKE mountain lake at sunset", author="FAKE Ann", nonfree=False):
    catl = "".join(f'<li><a href="/wiki/Category:{c.replace(" ", "_")}" title="Category:{c}">{c}</a></li>' for c in cats)
    return (f'<meta property="og:image" content="{THUMB}">'
            f'<td class="description"><div class="description en" lang="en"><span><b>English&#58; </b></span> {desc}</div></td>'
            f'<td id="fileinfotpl&#95;aut">Author</td> <td> <a href="/wiki/User:X">{author}</a></td>'
            f'<span class="licensetpl&#95;short">{lic}</span><span class="licensetpl&#95;link">https://creativecommons.org/licenses/by-sa/4.0</span>'
            + ('<span class="licensetpl_nonfree">x</span>' if nonfree else "")
            + f'<div id="mw-normal-catlinks" class="mw-normal-catlinks"><ul>{catl}</ul></div>').encode()


def parse(body, path="/wiki/File:FAKE_Lake.jpg"):
    return list(CommonsAdapter().parse(Response(B + path, 200, {}, body)))


def test_record_fields_and_attribution():
    [r] = parse(page())
    assert isinstance(r, Record)
    d = r.data
    assert d["license"] == "CC BY-SA 4.0" and d["author"] == "FAKE Ann" and d["rating"] == "general"
    assert d["source_url"] == B + "/wiki/File:FAKE_Lake.jpg"
    assert d["cover_url"] == "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/FAKE_Lake.jpg/500px-FAKE_Lake.jpg"
    assert "via Wikimedia Commons" in d["attribution"] and "FAKE Ann" in d["attribution"]
    assert {"山", "湖", "日落", "风景"} <= set(d["tags"]) and "Lakes of Nowhere" in d["tags"]
    assert not any(t.startswith("Featured") for t in d["tags"])
    assert r.media_urls == []  # never downloads images
    assert parse(page())[0].data["id"] == d["id"]  # deterministic ids


@pytest.mark.parametrize("lic", ["CC BY-NC 4.0", "CC BY-ND 3.0", "GFDL", "Fair use", ""])
def test_non_free_licenses_skipped(lic):
    assert parse(page(lic=lic)) == [Skip("license-not-allowed")]


@pytest.mark.parametrize("lic", ["CC0", "Public domain", "CC BY 2.0", "CC BY-SA 3.0 de", "PD-old"])
def test_free_licenses_accepted(lic):
    assert isinstance(parse(page(lic=lic))[0], Record)


def test_nonfree_marker_and_sfw_block():
    assert parse(page(nonfree=True)) == [Skip("license-not-allowed")]
    assert parse(page(cats=("Nude women",))) == [Skip("sfw-blocked")]
    assert parse(page(desc="FAKE topless figure")) == [Skip("sfw-blocked")]


def test_category_allow_list_and_links():
    with pytest.raises(ValueError):
        CommonsAdapter(["Anything_goes"])
    out = list(CommonsAdapter().parse(Response(B + "/wiki/Category:Featured_pictures_of_landscapes", 200, {},
                                               b'<a href="/wiki/File:A.jpg">a</a><a href="/wiki/File:A.jpg">a</a><a href="/wiki/Category:Sub">s</a>')))
    assert out == [Link("/wiki/File:A.jpg")]  # no sub-category recursion, de-duplicated
    assert thumb_url("https://example.com/x.jpg") is None


def test_crawl_honours_robots_and_writes_pack(tmp_path):
    robots = "User-agent: *\nDisallow: /w/\nDisallow: /wiki/File:Blocked.jpg\n"
    pages = {B + "/robots.txt": robots.encode(),
             B + "/wiki/Category:Featured_pictures_of_landscapes": b'<a href="/wiki/File:FAKE_Lake.jpg"></a><a href="/wiki/File:Blocked.jpg"></a><a href="/w/index.php?title=X&amp;filefrom=B"></a>',
             B + "/wiki/File:FAKE_Lake.jpg": page()}

    class F:
        ua = "GalleryEdge-Crawler/test"
        def get(self, url):
            self.seen = getattr(self, "seen", []) + [url]
            return Response(url, 200 if url in pages else 404, {}, pages.get(url, b""))
    f = F()
    cfg = CrawlConfig(contact="https://example.invalid/contact", allowed_hosts=["commons.wikimedia.org"], min_interval_s=0)
    st = Crawler(CommonsAdapter(["Featured_pictures_of_landscapes"]), cfg, state_path=str(tmp_path / "s.db"), out_dir=str(tmp_path / "p"),
                 fetcher=f, sleep=lambda s: None).run()
    assert st["records"] == 1 and st["skip_reasons"].get("robots-disallow", 0) >= 1
    assert not any("/w/" in u or "Blocked" in u for u in f.seen)
    _, recs, media = read_pack(st["packs"][0])
    assert recs[0]["provenance"]["robots"] == "allowed" and media == {}


def test_network_errors_are_skipped_not_fatal(tmp_path):
    class F:
        ua = "x"
        def get(self, url):
            if url.endswith("robots.txt"):
                return Response(url, 404, {}, b"")
            raise TimeoutError("timed out")
    cfg = CrawlConfig(contact="https://example.invalid/c", allowed_hosts=["commons.wikimedia.org"], min_interval_s=0)
    st = Crawler(CommonsAdapter(["Featured_pictures_of_landscapes"]), cfg, state_path=str(tmp_path / "s.db"), out_dir=str(tmp_path / "p"),
                 fetcher=F(), sleep=lambda s: None).run()
    assert st["skip_reasons"] == {"network-error": 1}
