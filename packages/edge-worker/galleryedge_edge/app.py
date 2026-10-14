"""WSGI edge app: work cards, tag/artist/full-text search, prompt copy, keyed JSON API.

This mirrors the routes the Cloudflare Worker will expose; the Worker port (JS)
talks to the same Turso schema over the same Hrana HTTP protocol.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from urllib.parse import parse_qs, urlencode

from galleryedge_db import dialect as dialects

from . import keys

PAGE = 24
CSS = """
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:#0f1115;color:#e6e6e6}
a{color:#8ab4ff;text-decoration:none}header{padding:16px 24px;background:#161a22;border-bottom:1px solid #262c38;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header h1{font-size:18px;margin:0}header h1 small{color:#8a93a6;font-weight:400}form{display:flex;gap:8px;flex:1;min-width:280px}
input{flex:1;padding:8px 12px;border-radius:8px;border:1px solid #303848;background:#0f1115;color:#e6e6e6}
button{padding:8px 14px;border-radius:8px;border:0;background:#3b6cff;color:#fff;cursor:pointer}
main{padding:20px 24px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:16px}
.card{background:#161a22;border:1px solid #262c38;border-radius:12px;overflow:hidden}.card img{width:100%;display:block;aspect-ratio:4/3}
.card .b{padding:10px 12px}.card h3{font-size:14px;margin:0 0 6px}.tag{display:inline-block;font-size:12px;padding:1px 8px;margin:2px 4px 2px 0;border-radius:99px;background:#232a38;color:#b9c4dd}
.meta{color:#8a93a6;font-size:12px}.banner{background:#2a2414;color:#e8d29a;padding:6px 24px;font-size:13px}
pre{white-space:pre-wrap;background:#161a22;border:1px solid #262c38;padding:12px;border-radius:8px}.pager{margin:20px 0;display:flex;gap:12px}
"""


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def cover_svg(work_id: str, title: str) -> str:
    h = hashlib.sha256(work_id.encode()).digest()
    c1, c2 = f"hsl({h[0] * 360 // 256},55%,45%)", f"hsl({h[1] * 360 // 256},60%,25%)"
    shapes = "".join(f'<circle cx="{h[2 + i] * 400 // 256}" cy="{h[8 + i] * 300 // 256}" r="{20 + h[14 + i] % 60}" fill="#fff" opacity="0.{1 + h[20 + i] % 3}"/>' for i in range(5))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient></defs>'
            f'<rect width="400" height="300" fill="url(#g)"/>{shapes}<text x="16" y="284" fill="#fff" font-family="sans-serif" font-size="16" opacity="0.85">{esc(title)}</text></svg>')



def split_tags(t) -> list[str]:
    """tags_text is ' | '-joined (multi-word tags); older rows were space-joined."""
    s = str(t or "")
    return [x.strip() for x in (s.split("|") if "|" in s else s.split()) if x.strip()]

class App:
    def __init__(self, db, dialect=None, *, demo_banner: bool = True):
        self.db = db
        name = None
        try:
            r = db.execute("SELECT v FROM meta WHERE k='fts_dialect'").rows
            name = r[0][0] if r else None
        except Exception:
            pass
        self.dialect = dialect or dialects.DIALECTS.get(name or "fts5")
        self.demo_banner = demo_banner

    # ---------- data ----------
    def search(self, q: str = "", tag: str = "", artist: str = "", page: int = 1) -> dict:
        where, args = [], []
        if q.strip():
            sql, a = self.dialect.search_sql(q)
            where.append(f"w.id IN ({sql})"); args += a
        if tag:
            where.append("w.id IN (SELECT work_id FROM work_tags WHERE tag=?)"); args.append(tag)
        if artist:
            where.append("w.artist=?"); args.append(artist)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        total = self.db.execute(f"SELECT count(*) FROM works w{w}", args).rows[0][0]
        page = max(1, page)
        rows = self.db.execute(f"SELECT w.id,w.title,w.artist,w.tags_text,w.license,w.image_count,w.cover_url FROM works w{w} ORDER BY w.created_at DESC, w.id LIMIT ? OFFSET ?",
                               [*args, PAGE, (page - 1) * PAGE]).dicts()
        for r in rows:
            r["tags"] = split_tags(r.pop("tags_text"))
        return {"total": total, "page": page, "page_size": PAGE, "items": rows}

    def work(self, wid: str) -> dict | None:
        r = self.db.execute("SELECT * FROM works WHERE id=?", [wid]).dicts()
        if not r:
            return None
        w = r[0]
        w["tags"] = split_tags(w.pop("tags_text"))
        w["synthetic"] = bool(w["synthetic"])
        return w

    def tags(self) -> list[dict]:
        return self.db.execute("SELECT tag, count(*) n FROM work_tags GROUP BY tag ORDER BY n DESC, tag LIMIT 200").dicts()

    # ---------- wsgi ----------
    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "/")
        qs = {k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", "")).items()}
        try:
            if environ.get("REQUEST_METHOD", "GET") not in ("GET", "HEAD"):
                return self._resp(start_response, 405, "text/plain", "method not allowed")
            if path == "/healthz":
                self.db.execute("SELECT 1")
                return self._json(start_response, 200, {"ok": True, "fts": self.dialect.name})
            if path.startswith("/api/"):
                return self._api(environ, start_response, path, qs)
            if path == "/":
                return self._resp(start_response, 200, "text/html; charset=utf-8", self._index(qs))
            m = re.fullmatch(r"/work/([\w\-]+)", path)
            if m:
                w = self.work(m.group(1))
                if not w:
                    return self._resp(start_response, 404, "text/html; charset=utf-8", self._layout("Not found", "<main><p>作品不存在 / not found</p></main>"))
                return self._resp(start_response, 200, "text/html; charset=utf-8", self._work_page(w))
            m = re.fullmatch(r"/cover/([\w\-]+)\.svg", path)
            if m:
                w = self.work(m.group(1))
                if not w:
                    return self._resp(start_response, 404, "text/plain", "not found")
                return self._resp(start_response, 200, "image/svg+xml", cover_svg(w["id"], w["title"]), {"Cache-Control": "public, max-age=86400"})
            return self._resp(start_response, 404, "text/plain", "not found")
        except Exception as e:  # never leak internals
            return self._resp(start_response, 500, "text/plain", f"internal error ({type(e).__name__})")

    def _api(self, environ, start_response, path, qs):
        status, reason, hdrs = keys.check(self.db, environ.get("HTTP_X_API_KEY"))
        if status != 200:
            return self._json(start_response, status, {"error": reason}, hdrs)
        if path == "/api/v1/search":
            try:
                page = int(qs.get("page", "1"))
            except ValueError:
                return self._json(start_response, 400, {"error": "bad page"}, hdrs)
            return self._json(start_response, 200, self.search(qs.get("q", ""), qs.get("tag", ""), qs.get("artist", ""), page), hdrs)
        m = re.fullmatch(r"/api/v1/works/([\w\-]+)", path)
        if m:
            w = self.work(m.group(1))
            return self._json(start_response, 200 if w else 404, w or {"error": "not found"}, hdrs)
        if path == "/api/v1/tags":
            return self._json(start_response, 200, {"items": self.tags()}, hdrs)
        return self._json(start_response, 404, {"error": "unknown endpoint"}, hdrs)

    @staticmethod
    def _resp(start_response, status, ctype, body: str, extra=None):
        reasons = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 429: "Too Many Requests", 500: "Internal Server Error"}
        b = body.encode("utf-8")
        headers = [("Content-Type", ctype), ("Content-Length", str(len(b))), ("X-Content-Type-Options", "nosniff"),
                   ("Content-Security-Policy", "default-src 'self'; img-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"),
                   ("Referrer-Policy", "no-referrer")]
        headers += list((extra or {}).items())
        start_response(f"{status} {reasons.get(status, '')}", headers)
        return [b]

    def _json(self, start_response, status, obj, extra=None):
        return self._resp(start_response, status, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False), extra)

    # ---------- html ----------
    def _layout(self, title, body, q="", ) -> str:
        banner = '<div class="banner">演示站：全部为合成数据 · Demo site — all data is synthetic</div>' if self.demo_banner else ""
        return (f'<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>{esc(title)} · GalleryEdge</title><style>{CSS}</style></head><body>{banner}'
                f'<header><h1><a href="/">GalleryEdge</a> <small>边缘图阁</small></h1>'
                f'<form action="/"><input name="q" value="{esc(q)}" placeholder="搜索标题 / 提示词 / 标签 / 画师 · search"><button>搜索</button></form></header>{body}</body></html>')

    def _index(self, qs) -> str:
        try:
            page = int(qs.get("page", "1"))
        except ValueError:
            page = 1
        q, tag, artist = qs.get("q", ""), qs.get("tag", ""), qs.get("artist", "")
        res = self.search(q, tag, artist, page)
        cards = "".join(
            f'<a class="card" href="/work/{esc(w["id"])}"><img src="/cover/{esc(w["id"])}.svg" alt="" loading="lazy"><div class="b"><h3>{esc(w["title"])}</h3>'
            f'<div class="meta">{esc(w["artist"])} · {esc(w["license"])} · {w["image_count"]} 张</div>'
            + "".join(f'<span class="tag">{esc(t)}</span>' for t in w["tags"]) + "</div></a>" for w in res["items"])
        filt = " · ".join(x for x in [f"标签 tag: <b>{esc(tag)}</b>" if tag else "", f"画师 artist: <b>{esc(artist)}</b>" if artist else ""] if x)
        tagbar = " ".join(f'<a class="tag" href="/?{urlencode({"tag": t["tag"]})}">{esc(t["tag"])} {t["n"]}</a>' for t in self.tags()[:20])
        pages = max(1, -(-res["total"] // PAGE))
        def link(p):
            return "/?" + urlencode({k: v for k, v in {"q": q, "tag": tag, "artist": artist, "page": p}.items() if v})
        pager = (f'<div class="pager">{"<a href=" + chr(34) + link(page - 1) + chr(34) + ">← 上一页</a>" if page > 1 else ""}'
                 f'<span class="meta">第 {page}/{pages} 页 · 共 {res["total"]} 件</span>'
                 f'{"<a href=" + chr(34) + link(page + 1) + chr(34) + ">下一页 →</a>" if page < pages else ""}</div>')
        body = f'<main><p>{tagbar}</p>{"<p>" + filt + "</p>" if filt else ""}<div class="grid">{cards or "<p>没有结果 / no results</p>"}</div>{pager}</main>'
        return self._layout("搜索" if q else "首页", body, q)

    def _work_page(self, w) -> str:
        tags = "".join(f'<a class="tag" href="/?{urlencode({"tag": t})}">{esc(t)}</a>' for t in w["tags"])
        body = (f'<main style="max-width:900px"><img src="/cover/{esc(w["id"])}.svg" alt="" style="width:100%;max-width:480px;border-radius:12px">'
                f'<h2>{esc(w["title"])}</h2><p class="meta">画师 <a href="/?{urlencode({"artist": w["artist"] or ""})}">{esc(w["artist"])}</a> · '
                f'许可 {esc(w["license"])} · 来源 <a href="{esc(w["source_url"])}" rel="nofollow noopener">{esc(w["source_url"])}</a></p><p>{tags}</p>'
                f'<h3>提示词 Prompt</h3><pre id="prompt">{esc(w["prompt"])}</pre>'
                f'<button id="copy" onclick="navigator.clipboard.writeText(document.getElementById(\'prompt\').innerText).then(()=>this.textContent=\'已复制 ✓\')">复制提示词 Copy</button></main>')
        return self._layout(w["title"], body)
