"""Wikimedia Commons adapter: synthetic HTML fixtures shaped like Commons pages (no real content)."""
import re
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


def test_retry_after_backoff_then_success(tmp_path):
    calls, waits = [], []

    class F:
        ua = "x"
        def get(self, url):
            if url.endswith("robots.txt"):
                return Response(url, 404, {}, b"")
            calls.append(url)
            if len(calls) <= 2:
                return Response(url, 429, {"Retry-After": "7"}, b"")
            return Response(url, 200, {}, b"")
    ev = []
    cfg = CrawlConfig(contact="https://example.invalid/c", allowed_hosts=["commons.wikimedia.org"], min_interval_s=0)
    t = [0.0]
    cr = Crawler(CommonsAdapter(["Featured_pictures_of_landscapes"]), cfg, state_path=str(tmp_path / "s.db"), out_dir=str(tmp_path / "p"),
                 fetcher=F(), clock=lambda: t[0], sleep=lambda s: (waits.append(s), t.__setitem__(0, t[0] + s)),
                 on_event=lambda *a: ev.append(a[1]))
    st = cr.run()
    assert len(calls) == 3 and st["backoffs"] == 2 and "crawler.backoff" in ev and st["skip_reasons"] == {"empty-category": 1}
    assert sum(waits) >= 14


def test_backoff_gives_up_and_progress_lines(tmp_path):
    class F:
        ua = "x"
        def get(self, url):
            return Response(url, 404, {}, b"") if url.endswith("robots.txt") else Response(url, 503, {}, b"")
    lines = []
    cfg = CrawlConfig(contact="https://example.invalid/c", allowed_hosts=["commons.wikimedia.org"], min_interval_s=0, max_backoff_retries=2)
    st = Crawler(CommonsAdapter(["Featured_pictures_of_landscapes"]), cfg, state_path=str(tmp_path / "s.db"), out_dir=str(tmp_path / "p"),
                 fetcher=F(), sleep=lambda s: None, progress=lines.append, progress_every=1).run()
    assert st["skip_reasons"] == {"http-503": 1} and st["backoffs"] == 2
    assert lines[0].endswith("starting fresh") and any("pages 0/" in l and "skipped 1" in l for l in lines)
    st2 = Crawler(CommonsAdapter(["Featured_pictures_of_landscapes"]), cfg, state_path=str(tmp_path / "s.db"), out_dir=str(tmp_path / "p"),
                  fetcher=F(), sleep=lambda s: None, progress=lines.append).run()
    assert any("resuming from page 0" in l for l in lines)


def test_cli_rejects_fake_contact(capsys):
    from galleryedge_crawl.cli import main
    with pytest.raises(SystemExit):
        main(["--adapter", "wikimedia-commons", "--contact", "nobody"])
    assert "real email address" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["--adapter", "wikimedia-commons", "--contact", "a@b.c", "--min-interval", "0.2"])


def test_ingest_token_file(tmp_path, monkeypatch):
    import json
    import galleryedge_ingest as gi
    seen = {}
    monkeypatch.setattr(gi.C, "connect", lambda url, tok, prefix="": seen.setdefault("tok", tok) and (_ for _ in ()).throw(SystemExit(0)))
    f = tmp_path / "t.json"; f.write_text(json.dumps({"rw_token": "FAKE-TOKEN"}))
    with pytest.raises(SystemExit):
        gi.main(["--db", "libsql://x.invalid", "--token-file", str(f), "p.tar"])
    assert seen["tok"] == "FAKE-TOKEN"


def test_sfw_recheck_blocks_bijinga():
    from galleryedge_adapters.commons import sfw_ok
    assert not sfw_ok({"title": "FAKE print", "tags": ["Bijin-ga"]})
    assert not parse(page(cats=("Shunga",)))[0].data if False else parse(page(cats=("Shunga",))) == [Skip("sfw-blocked")]
    assert sfw_ok({"title": "FAKE lake", "tags": ["湖"]})


def test_clean_author_and_normalize():
    from galleryedge_adapters.commons import clean_author, normalize_record
    assert clean_author("© FAKE Super / CC BY-SA 4.0 (via Wikimedia Commons) You are free: to share") == "FAKE Super"
    assert clean_author("FAKE Diego You are free: to share – to copy") == "FAKE Diego"
    assert clean_author("") == "unknown"
    r = normalize_record({"title": "FAKE Volcanoes at night", "prompt": "", "tags": ["Clouds and blue sky", "火山"], "license": "CC0", "author": "© FAKE X / CC0"})
    assert r["author"] == "FAKE X" and "via Wikimedia Commons" in r["attribution"] and {"火山", "夜景"} <= set(r["tags"])
    assert "Clouds and blue sky" in r["tags"]


def test_multiword_tags_roundtrip(tmp_path):
    from galleryedge_db import dialect, schema
    from galleryedge_db.client import connect
    from galleryedge_edge.app import split_tags
    db = connect(f"file:{tmp_path}/x.db"); schema.init(db, dialect.detect(db))
    [rec] = parse(page())
    schema.ingest(db, [rec.data])
    t = db.execute("SELECT tags_text FROM works").rows[0][0]
    assert "Lakes of Nowhere" in split_tags(t) and split_tags("a b") == ["a", "b"]


def test_templatestyles_css_never_leaks():
    from galleryedge_adapters.commons import _txt, clean_author
    assert _txt('<style>.mw-parser-output .x{color:red}</style><b>FAKE Name</b>') == "FAKE Name"
    assert clean_author("FAKE Studio. Restored by .mw-parser-output .commons-creator-ta") == "FAKE Studio. Restored by"


@pytest.mark.parametrize("cats,desc", [(("Flags of FAKE land",), "x"), (("Coats of arms of FAKE",), "x"), (("Maps of FAKE",), "x"),
                                       (("FAKE protests",), "x"), (("Military parades",), "x"), ((), "FAKE president speaking"),
                                       (("Landscapes of China",), "x"), ((), "国旗"), (("Churches in FAKE",), "x")])
def test_sensitive_blocked(cats, desc):
    assert parse(page(cats=cats, desc=desc)) == [Skip("sensitive-blocked")]


def test_sensitive_offline_recheck_and_neutral_categories():
    from galleryedge_adapters.commons import SFW_CATEGORIES, sfw_ok
    assert not sfw_ok({"title": "Flag of FAKE", "tags": []}) and not sfw_ok({"title": "x", "tags": ["中国"]})
    assert sfw_ok({"title": "FAKE autumn forest", "tags": ["森林"]})
    assert not any(re.search(r"China|Japan|Taiwan|Hong_Kong", c) for c in SFW_CATEGORIES)


def test_empty_category_is_reported():
    out = list(CommonsAdapter().parse(Response(B + "/wiki/Category:Featured_pictures_of_cats", 200, {}, b"<p>no files</p>")))
    assert out == [Skip("empty-category")]


def test_llama_not_lama():
    from galleryedge_adapters.commons import SENSITIVE
    assert not SENSITIVE.search("FAKE llama in the hills") and SENSITIVE.search("FAKE lama prays")


def test_cli_categories(capsys):
    from galleryedge_crawl.cli import main
    assert main(["--adapter", "wikimedia-commons", "--list-categories"]) == 0
    assert "Featured_pictures_of_cats" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["--adapter", "wikimedia-commons", "--contact", "a@b.c", "--categories", "Flags_of_everything"])
    assert "--list-categories" in capsys.readouterr().err


def test_clean_author_upload_tool_note():
    from galleryedge_adapters.commons import clean_author
    assert clean_author("FAKE Krapf This file was uploaded with Commonist.") == "FAKE Krapf"


def test_category_pagination_rewrites_to_wiki_path():
    from galleryedge_adapters.commons import CommonsAdapter
    from galleryedge_crawl import Link, Response
    a = CommonsAdapter(["Quality_images_of_flowers"], max_category_pages=2)
    html = ('<a href="/wiki/File:A.jpg">x</a><a href="/w/index.php?title=Category:Quality_images_of_flowers&amp;filefrom=B+c.JPG#mw-category-media" '
            'title="Category:Quality images of flowers">next page</a>')
    u = "https://commons.wikimedia.org/wiki/Category:Quality_images_of_flowers"
    links = [x.url for x in a.parse(Response(u, 200, {}, html.encode())) if isinstance(x, Link)]
    nxt = [l for l in links if "filefrom" in l]
    assert nxt == ["https://commons.wikimedia.org/wiki/Category:Quality_images_of_flowers?filefrom=B+c.JPG"]
    assert "/w/index.php" not in nxt[0]
    # bounded: second listing page does not link further
    links2 = [x.url for x in a.parse(Response(nxt[0], 200, {}, html.encode())) if isinstance(x, Link)]
    assert not any("filefrom" in l for l in links2)


def test_category_pagination_ignores_subcategory_pager():
    from galleryedge_adapters.commons import CommonsAdapter
    from galleryedge_crawl import Link, Response
    a = CommonsAdapter(["Quality_images_of_flowers"], max_category_pages=5)
    html = ('<a href="/wiki/File:A.jpg">x</a>'
            '<a href="/w/index.php?title=Category:Quality_images_of_flowers&amp;filefrom=OLD.JPG&amp;subcatfrom=Jasione#mw-subcategories" title="c">next page</a>'
            '<a href="/w/index.php?title=Category:Quality_images_of_flowers&amp;filefrom=NEW.JPG#mw-category-media" title="c">next page</a>')
    u = "https://commons.wikimedia.org/wiki/Category:Quality_images_of_flowers?filefrom=OLD.JPG"
    nxt = [x.url for x in a.parse(Response(u, 200, {}, html.encode())) if isinstance(x, Link) and "filefrom" in x.url]
    assert nxt == ["https://commons.wikimedia.org/wiki/Category:Quality_images_of_flowers?filefrom=NEW.JPG"]
