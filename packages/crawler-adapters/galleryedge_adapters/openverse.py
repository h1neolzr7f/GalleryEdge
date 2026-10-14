"""Openverse API adapter (https://api.openverse.org) — CC/PD licences only, SFW + sensitive filters, attribution.

Compliance notes (checked 2026-10-14):
* api.openverse.org/robots.txt has ``User-agent: * / Disallow: /v1/images/``. The crawler honours robots.txt,
  so against the live API every request is skipped as ``robots-disallow``; this adapter only runs where the
  operator's robots allow it (e.g. a self-hosted Openverse mirror or a registered-client arrangement that
  publishes a permissive robots policy). We do not bypass robots and do not auto-register API clients.
* Anonymous limits advertised by the API: 20/min burst, 200/day sustained => use --min-interval >= 3.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlencode, urlsplit, parse_qs

from galleryedge_crawl import Link, Record, Response, Skip

from .commons import BLOCK, SENSITIVE, zh_tags_for

HOST = "api.openverse.org"
LICENSES = ("cc0", "pdm", "by", "by-sa")
QUERIES = ("flower", "bird", "butterfly", "cat", "waterfall", "forest", "beach", "clouds", "autumn leaves", "still life",
           "mushroom", "succulent", "seashell", "dew drops", "sunset sky", "snowy mountain", "lake reflection", "tulip", "orchid", "bee")


def license_label(code: str, version: str | None) -> str | None:
    code = (code or "").lower()
    if code not in LICENSES:
        return None
    return {"cc0": "CC0 1.0", "pdm": "Public domain"}.get(code) or f"CC {code.upper()} {version or ''}".strip()


class OpenverseAdapter:
    name = "openverse"

    def __init__(self, queries=QUERIES, max_pages_per_query: int = 5, page_size: int = 20, base: str = f"https://{HOST}"):
        self.queries, self.max_pages, self.page_size, self.base = list(queries), max_pages_per_query, page_size, base.rstrip("/")

    def search_url(self, q: str, page: int = 1) -> str:
        return f"{self.base}/v1/images/?" + urlencode({"q": q, "license": ",".join(LICENSES), "mature": "false",
                                                       "page_size": self.page_size, "page": page})

    def start_urls(self):
        return [self.search_url(q) for q in self.queries]

    def parse(self, resp: Response):
        try:
            data = json.loads(resp.text)
        except ValueError:
            yield Skip("bad-json"); return
        qs = parse_qs(urlsplit(resp.url).query)
        q, page = qs.get("q", [""])[0], int(qs.get("page", ["1"])[0])
        results = data.get("results") or []
        if not results:
            yield Skip("empty-results"); return
        for it in results:
            yield self.record(it)
        if page < min(self.max_pages, int(data.get("page_count") or 0)):
            yield Link(self.search_url(q, page + 1))

    def record(self, it: dict):
        lic = license_label(it.get("license"), it.get("license_version"))
        if not lic:
            return Skip("license-not-allowed")
        if it.get("mature") or it.get("unstable__sensitivity"):
            return Skip("sfw-blocked")
        tags = [t.get("name", "") for t in it.get("tags") or [] if t.get("name")][:20]
        title = (it.get("title") or "").strip() or "Untitled"
        hay = " ".join([title, *tags])
        if BLOCK.search(hay):
            return Skip("sfw-blocked")
        if SENSITIVE.search(hay):
            return Skip("sensitive-blocked")
        cover, landing = it.get("url"), it.get("foreign_landing_url")
        if not cover or not str(cover).startswith("https://") or not landing:
            return Skip("no-thumbnail")
        author = (it.get("creator") or "Unknown").strip()[:200]
        zh = zh_tags_for(hay.lower())
        attribution = (it.get("attribution") or f"“{title}” by {author}, {lic}").strip()[:600]
        return Record({
            "id": "ov-" + re.sub(r"[^a-f0-9-]", "", str(it.get("id", "")).lower())[:40],
            "title": title[:200], "prompt": "", "artist": author[:120], "author": author,
            "tags": list(dict.fromkeys(tags + zh)), "license": lic, "license_url": it.get("license_url") or "",
            "attribution": attribution + f" (via Openverse; provider: {it.get('provider') or it.get('source') or '?'})",
            "rating": "general", "source_url": landing, "cover_url": cover, "image_count": 1,
            "rights_note": "Hotlinked provider image via Openverse metadata; attribution shown on the work page.",
        })
