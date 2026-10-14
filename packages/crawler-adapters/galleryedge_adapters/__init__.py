"""Demo adapters plus one openly-licensed real source (Wikimedia Commons, SFW allow-list; see commons.py)."""
from __future__ import annotations

import json
import re
from typing import Iterable

from galleryedge_crawl import Link, Record, Response, Skip
from galleryedge_demo.generate import generate

LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
HREF_RE = re.compile(r'href="([^"]+)"')


class DemoSelfhostAdapter:
    """Parses the bundled demo source site (apps/demo-source)."""
    name = "demo-selfhost"

    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def start_urls(self) -> Iterable[str]:
        return [self.base + "/works"]

    def parse(self, resp: Response):
        for m in LD_RE.finditer(resp.text):
            try:
                ld = json.loads(m.group(1))
            except ValueError:
                yield Skip("bad-jsonld"); continue
            yield Record({
                "id": ld.get("identifier"), "title": ld.get("name"), "artist": ld.get("creator"),
                "tags": list(ld.get("keywords") or []), "license": ld.get("license"), "rating": ld.get("contentRating", "general"),
                "prompt": ld.get("description", ""), "created_at": ld.get("dateCreated"), "source_url": resp.url,
                "image_count": 1, "synthetic": True,
            }, media_urls=[self.base + ld["image"]] if ld.get("image") else [])
        if "/works" in resp.url and "/works/" not in resp.url:
            for h in HREF_RE.findall(resp.text):
                yield Link(h)


class SyntheticAdapter:
    """No network at all: emits generated records from a fake listing (pipeline smoke tests)."""
    name = "synthetic"

    def __init__(self, n: int = 100, seed: int = 42, base: str = "https://demo-source.invalid"):
        self.works = {w["id"]: w for w in generate(n, seed)}
        self.base = base

    def start_urls(self):
        return [f"{self.base}/works"]

    def parse(self, resp: Response):
        if resp.url.endswith("/works"):
            for wid in self.works:
                yield Link(f"/works/{wid}")
            return
        wid = resp.url.rsplit("/", 1)[-1]
        if wid in self.works:
            yield Record({k: v for k, v in self.works[wid].items() if k not in ("pack_id", "content_flags", "cover_key")})


class SyntheticFetcher:
    """Offline fetcher for SyntheticAdapter (robots allows all)."""
    ua = "GalleryEdge-Crawler/0.4 (synthetic)"

    def get(self, url: str) -> Response:
        if url.endswith("/robots.txt"):
            return Response(url, 200, {}, b"User-agent: *\nAllow: /\n")
        return Response(url, 200, {"Content-Type": "text/html"}, f"<synthetic {url}>".encode())


from .commons import CommonsAdapter  # noqa: E402

ADAPTERS = {"demo-selfhost": DemoSelfhostAdapter, "synthetic": SyntheticAdapter, "wikimedia-commons": CommonsAdapter}
