"""Self-hosted demo source site (synthetic). Lets you practise crawling without touching any real site.

Routes: /robots.txt, /works?page=N, /works/<id> (JSON-LD with license), /media/<id>.svg,
/private/* (robots-disallowed), /members/<id> (401 login wall), /premium/<id> (402 paywall),
/old/<id> (302 -> /login), /nolicense/<id> (record without license).
"""
from __future__ import annotations

import argparse
import html
import json
import re
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from galleryedge_demo.generate import generate

PER_PAGE = 20
ROBOTS = "User-agent: *\nDisallow: /private/\nCrawl-delay: 0\n"


class DemoSource:
    def __init__(self, n: int = 60, seed: int = 7):
        self.works = generate(n, seed)
        self.by_id = {w["id"]: w for w in self.works}
        self.hits: list[tuple[str, str]] = []  # (path, user-agent) for tests

    def __call__(self, env, sr):
        path, ua = env.get("PATH_INFO", "/"), env.get("HTTP_USER_AGENT", "")
        self.hits.append((path, ua))
        q = {k: v[0] for k, v in parse_qs(env.get("QUERY_STRING", "")).items()}

        def send(status, ctype, body, extra=()):
            b = body.encode() if isinstance(body, str) else body
            sr(status, [("Content-Type", ctype), ("Content-Length", str(len(b))), *extra])
            return [b]

        if path == "/robots.txt":
            return send("200 OK", "text/plain", ROBOTS)
        if path in ("/", "/works"):
            page = max(1, int(q.get("page", "1")) if q.get("page", "1").isdigit() else 1)
            chunk = self.works[(page - 1) * PER_PAGE: page * PER_PAGE]
            links = "".join(f'<li><a href="/works/{w["id"]}">{html.escape(w["title"])}</a></li>' for w in chunk)
            extra = ('<li><a href="/private/secret">private</a></li><li><a href="/members/m1">members</a></li>'
                     '<li><a href="/premium/p1">premium</a></li><li><a href="/old/o1">old</a></li><li><a href="/nolicense/n1">nolicense</a></li>') if page == 1 else ""
            nxt = f'<a rel="next" href="/works?page={page + 1}">next</a>' if page * PER_PAGE < len(self.works) else ""
            return send("200 OK", "text/html; charset=utf-8", f"<html><body><h1>Demo source (synthetic)</h1><ul>{links}{extra}</ul>{nxt}</body></html>")
        m = re.fullmatch(r"/works/([\w\-]+)", path)
        if m and m.group(1) in self.by_id:
            w = self.by_id[m.group(1)]
            ld = {"@context": "https://schema.org", "@type": "VisualArtwork", "identifier": w["id"], "name": w["title"],
                  "creator": w["artist"], "keywords": w["tags"], "license": w["license"], "contentRating": w["rating"],
                  "description": w["prompt"], "image": f"/media/{w['id']}.svg", "dateCreated": w["created_at"]}
            return send("200 OK", "text/html; charset=utf-8",
                        f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head><body><h1>{html.escape(w["title"])}</h1></body></html>')
        m = re.fullmatch(r"/media/([\w\-]+)\.svg", path)
        if m and m.group(1) in self.by_id:
            return send("200 OK", "image/svg+xml", f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4 3"><rect width="4" height="3" fill="#{__import__("hashlib").sha256(m.group(1).encode()).hexdigest()[:6]}"/></svg>')
        if path.startswith("/members/"):
            return send("401 Unauthorized", "text/html", "<h1>login required</h1>", [("WWW-Authenticate", 'Basic realm="demo"')])
        if path.startswith("/premium/"):
            return send("402 Payment Required", "text/html", "<h1>subscribe</h1>")
        if path.startswith("/old/"):
            return send("302 Found", "text/html", "", [("Location", "/login?next=" + path)])
        if path.startswith("/nolicense/"):
            ld = {"@type": "VisualArtwork", "identifier": "nolicense-1", "name": "no license here", "contentRating": "general"}
            return send("200 OK", "text/html", f'<script type="application/ld+json">{json.dumps(ld)}</script>')
        if path.startswith("/private/"):
            return send("200 OK", "text/html", "<h1>should never be fetched</h1>")
        return send("404 Not Found", "text/plain", "not found")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ge-demo-source")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("-n", type=int, default=60)
    a = ap.parse_args(argv)
    with make_server("127.0.0.1", a.port, DemoSource(a.n)) as s:
        print(f"demo source on http://127.0.0.1:{a.port}")
        s.serve_forever()
    return 0
