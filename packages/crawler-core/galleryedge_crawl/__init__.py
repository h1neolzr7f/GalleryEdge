"""Compliant crawler core. Rules are enforced by the framework, not by adapters:

* robots.txt fetched per host and obeyed (Disallow + Crawl-delay); unreachable robots
  (5xx/network) => treat host as disallowed; 4xx robots => allowed (RFC 9309)
* honest User-Agent naming the project and a contact (required)
* per-host minimum interval (max of config and Crawl-delay)
* login walls / paywalls / CAPTCHAs are skipped with a reason, never bypassed
* allowed-hosts list; only http(s); max pages budget
* metadata-only by default (fetch_media=False); every record carries provenance + license
* dedupe by URL and record id; resumable checkpoint (SQLite); packs are checksummed
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Protocol
from urllib.parse import urldefrag, urljoin, urlsplit

from galleryedge_pack import write_pack

VERSION = "0.4"
SKIP_STATUSES = {401: "login-wall", 402: "paywall", 403: "forbidden", 407: "proxy-auth", 429: "rate-limited"}
LOGIN_HINTS = ("/login", "/signin", "/sign-in", "/account/login", "/auth")


@dataclass
class Response:
    url: str
    status: int
    headers: dict
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


@dataclass
class Link:
    url: str


@dataclass
class Record:
    data: dict
    media_urls: list[str] = field(default_factory=list)


@dataclass
class Skip:
    reason: str


class SiteAdapter(Protocol):
    name: str
    def start_urls(self) -> Iterable[str]: ...
    def parse(self, resp: Response) -> Iterable[Link | Record | Skip]: ...


class HttpFetcher:
    def __init__(self, user_agent: str, timeout: float = 10.0, opener=None):
        self.ua, self.timeout = user_agent, timeout
        self.opener = opener or urllib.request.build_opener(_NoRedirect)

    def get(self, url: str) -> Response:
        req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept": "text/html,application/json;q=0.9,*/*;q=0.5"})
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return Response(url, r.status, dict(r.headers), r.read(5_000_000))
        except urllib.error.HTTPError as e:
            return Response(url, e.code, dict(e.headers or {}), e.read() if e.fp else b"")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface redirects so the crawler can see (and refuse) login redirects."""
    def redirect_request(self, *a, **k):
        return None


@dataclass
class CrawlConfig:
    contact: str
    allowed_hosts: list[str]
    min_interval_s: float = 1.0
    max_pages: int = 1000
    fetch_media: bool = False
    pack_size: int = 500
    max_redirects: int = 3
    max_backoff_retries: int = 3  # on 429/503: honour Retry-After, exponential backoff, capped at 300s

    def user_agent(self) -> str:
        if not self.contact or "@" not in self.contact and not self.contact.startswith("http"):
            raise ValueError("a real contact (email or URL) is required for the User-Agent")
        return f"GalleryEdge-Crawler/{VERSION} (+https://github.com/h1neolzr7f/GalleryEdge; contact: {self.contact})"


def canonical(url: str) -> str:
    u, _ = urldefrag(url)
    s = urlsplit(u)
    return s._replace(scheme=s.scheme.lower(), netloc=s.netloc.lower(), path=s.path or "/").geturl()


class State:
    """Checkpoint: frontier, seen URLs, emitted ids, skips, pending records."""
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS frontier(url TEXT PRIMARY KEY, added INTEGER);
        CREATE TABLE IF NOT EXISTS seen(url TEXT PRIMARY KEY, status INTEGER, at TEXT);
        CREATE TABLE IF NOT EXISTS emitted(id TEXT PRIMARY KEY, pack_id TEXT);
        CREATE TABLE IF NOT EXISTS skips(url TEXT PRIMARY KEY, reason TEXT, at TEXT);
        CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY, json TEXT, media TEXT);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        """)
        self.db.commit()

    def push(self, url):
        if not self.db.execute("SELECT 1 FROM seen WHERE url=?", (url,)).fetchone():
            self.db.execute("INSERT OR IGNORE INTO frontier VALUES (?, ?)", (url, time.time_ns()))

    def pop(self):
        r = self.db.execute("SELECT url FROM frontier ORDER BY added LIMIT 1").fetchone()
        return r[0] if r else None

    def mark(self, url, status):
        self.db.execute("DELETE FROM frontier WHERE url=?", (url,))
        self.db.execute("INSERT OR REPLACE INTO seen VALUES (?,?,?)", (url, status, _now()))
        self.db.commit()

    def skip(self, url, reason):
        self.db.execute("INSERT OR REPLACE INTO skips VALUES (?,?,?)", (url, reason, _now()))

    def counter(self, k, inc=0) -> int:
        v = int((self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone() or ("0",))[0]) + inc
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))
        return v


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Crawler:
    def __init__(self, adapter: SiteAdapter, cfg: CrawlConfig, *, state_path: str, out_dir: str,
                 fetcher=None, clock=time.monotonic, sleep=time.sleep, on_event=None, heartbeat_every: int = 25,
                 progress=None, progress_every: int = 50):
        self.a, self.cfg = adapter, cfg
        self.fetcher = fetcher or HttpFetcher(cfg.user_agent())
        cfg.user_agent()  # validate contact even with an injected fetcher
        self.st = State(state_path)
        self.st_path = state_path
        self.out = Path(out_dir)
        self.clock, self.sleep = clock, sleep
        self.robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.last_hit: dict[str, float] = {}
        self.packs: list[Path] = []
        self.stats = {"fetched": 0, "records": 0, "skipped": 0, "dupes": 0}
        self.on_event = on_event or (lambda level, code, msg, payload: None)
        self.heartbeat_every = heartbeat_every
        self.progress, self.progress_every = progress, progress_every

    # --- policy ---
    def _host(self, url):
        return urlsplit(url).netloc.lower()

    def _wait(self, host, extra: float = 0):
        gap = max(self.cfg.min_interval_s, extra)
        last = self.last_hit.get(host)
        if last is not None:
            d = gap - (self.clock() - last)
            if d > 0:
                self.sleep(d)
        self.last_hit[host] = self.clock()

    def _robots(self, url):
        s = urlsplit(url)
        host = s.netloc.lower()
        if host not in self.robots:
            rurl = f"{s.scheme}://{s.netloc}/robots.txt"
            self._wait(host)
            try:
                r = self.fetcher.get(rurl)
                status = r.status
            except Exception:
                status = 599
            rp = urllib.robotparser.RobotFileParser()
            if 200 <= status < 300:
                rp.parse(r.text.splitlines())
            elif 400 <= status < 500:
                rp.parse([])  # no robots => allowed
            else:
                rp = None  # unreachable => disallow everything (conservative)
            self.robots[host] = rp
        return self.robots[host]

    def allowed(self, url) -> tuple[bool, str]:
        s = urlsplit(url)
        if s.scheme not in ("http", "https"):
            return False, "scheme"
        if s.netloc.lower() not in [h.lower() for h in self.cfg.allowed_hosts]:
            return False, "host-not-allowed"
        rp = self._robots(url)
        if rp is None:
            return False, "robots-unreachable"
        if not rp.can_fetch(self.fetcher.ua if hasattr(self.fetcher, "ua") else "*", url):
            return False, "robots-disallow"
        return True, ""

    def _fetch(self, url) -> Response | Skip:
        ok, why = self.allowed(url)
        if not ok:
            return Skip(why)
        host = self._host(url)
        rp = self.robots[host]
        delay = (rp.crawl_delay(self.fetcher.ua if hasattr(self.fetcher, "ua") else "*") or 0) if rp else 0
        backoffs = 0
        for _ in range(self.cfg.max_redirects + 1 + self.cfg.max_backoff_retries):
            r = None
            for attempt in range(2):  # one polite retry on network errors, then skip
                self._wait(host, float(delay) + 5.0 * attempt)
                try:
                    r = self.fetcher.get(url)
                    break
                except (OSError, ValueError):
                    r = None
            if r is None:
                return Skip("network-error")
            self.stats["fetched"] += 1
            if r.status in (301, 302, 303, 307, 308):
                loc = urljoin(url, r.headers.get("Location", r.headers.get("location", "")))
                if any(h in urlsplit(loc).path.lower() for h in LOGIN_HINTS):
                    return Skip("login-wall")
                ok, why = self.allowed(loc)
                if not ok:
                    return Skip(why)
                url = loc
                continue
            if r.status in (429, 503) and backoffs < self.cfg.max_backoff_retries:
                ra = str(r.headers.get("Retry-After", r.headers.get("retry-after", "")) or "")
                wait = min(300.0, float(ra) if ra.isdigit() else 10.0 * 2 ** backoffs)
                backoffs += 1
                self.stats["backoffs"] = self.stats.get("backoffs", 0) + 1
                self.on_event("warn", "crawler.backoff", f"{host}: HTTP {r.status}, waiting {wait:.0f}s", {"url": url, "wait_s": wait})
                self._wait(host, wait)
                continue
            if r.status in SKIP_STATUSES:
                return Skip(SKIP_STATUSES[r.status])
            if r.status >= 400:
                return Skip(f"http-{r.status}")
            return r
        return Skip("too-many-redirects")

    # --- main loop ---
    def run(self) -> dict:
        resumed = bool(self.st.counter("started"))
        if not resumed:
            for u in self.a.start_urls():
                self.st.push(canonical(u))
            self.st.counter("started", 1)
            self.st.db.commit()
        pages = self.st.counter("pages")
        if self.progress:
            self.progress(f"[{self.a.name}] " + (f"resuming from page {pages}, {self.st.db.execute('SELECT count(*) FROM frontier').fetchone()[0]} queued (state {self.st_path})" if resumed else "starting fresh"))
        self.on_event("info", "crawler.start", f"{self.a.name} crawl started", {"adapter": self.a.name, "resumed_at_page": pages})
        while pages < self.cfg.max_pages and (url := self.st.pop()):
            res = self._fetch(url)
            if isinstance(res, Skip):
                self.st.skip(url, res.reason); self.stats["skipped"] += 1
                self.st.mark(url, 0)
                self._tick(pages)
                continue
            for item in self.a.parse(res):
                if isinstance(item, Link):
                    self.st.push(canonical(urljoin(res.url, item.url)))
                elif isinstance(item, Skip):
                    self.st.skip(url, item.reason); self.stats["skipped"] += 1
                elif isinstance(item, Record):
                    self._accept(item, res)
            pages = self.st.counter("pages", 1)
            self.st.mark(url, res.status)
            self._tick(pages)
            if pages % self.heartbeat_every == 0:
                self.on_event("info", "crawler.heartbeat", f"{self.a.name}: {pages} pages", {"adapter": self.a.name, "pages": pages, **self.stats})
            self._maybe_flush(False)
        self._maybe_flush(True)
        self.stats["pages_total"] = pages
        self.stats["packs"] = [str(p) for p in self.packs]
        reasons = dict(self.st.db.execute("SELECT reason, count(*) FROM skips GROUP BY reason").fetchall())
        self.stats["skip_reasons"] = reasons
        if reasons:
            self.on_event("warn" if reasons.get("robots-unreachable") else "info", "crawler.skips",
                          f"{self.a.name} skipped: " + ", ".join(f"{k}={v}" for k, v in sorted(reasons.items())), {"adapter": self.a.name, "reasons": reasons})
        self.on_event("info", "crawler.done", f"{self.a.name} done: {self.stats['records']} records, {len(self.packs)} packs",
                      {"adapter": self.a.name, **{k: v for k, v in self.stats.items() if k != "packs"}, "packs": len(self.packs)})
        return self.stats

    def _tick(self, pages: int):
        self._n = getattr(self, "_n", 0) + 1
        if self.progress and self._n % self.progress_every == 0:
            left = self.st.db.execute("SELECT count(*) FROM frontier").fetchone()[0]
            self.progress(f"[{self.a.name}] pages {pages}/{self.cfg.max_pages} · records {self.stats['records']} · "
                          f"skipped {self.stats['skipped']} · queue {left}")

    def _accept(self, rec: Record, res: Response):
        d = dict(rec.data)
        rid = str(d.get("id", ""))
        if not rid or self.st.db.execute("SELECT 1 FROM emitted WHERE id=? UNION SELECT 1 FROM pending WHERE id=?", (rid, rid)).fetchone():
            self.stats["dupes"] += 1
            return
        if not d.get("license"):
            self.st.skip(res.url, "no-license"); self.stats["skipped"] += 1
            return  # no license info => not collected
        d.setdefault("source_url", res.url)
        d["provenance"] = {"adapter": self.a.name, "fetched_at": _now(), "fetch_url": res.url, "http_status": res.status,
                           "robots": "allowed", "content_sha256": hashlib.sha256(res.body).hexdigest(),
                           "media_fetched": self.cfg.fetch_media}
        media = rec.media_urls if self.cfg.fetch_media else []
        self.st.db.execute("INSERT INTO pending VALUES (?,?,?)", (rid, json.dumps(d), json.dumps(media)))
        self.st.db.commit()
        self.stats["records"] += 1

    def _maybe_flush(self, final: bool):
        n = self.st.db.execute("SELECT count(*) FROM pending").fetchone()[0]
        while n and (final or n >= self.cfg.pack_size):
            rows = self.st.db.execute("SELECT id,json,media FROM pending ORDER BY id LIMIT ?", (self.cfg.pack_size,)).fetchall()
            seq = self.st.counter("pack_seq", 1)
            pack_id = f"{self.a.name}-{seq:05d}"
            media: dict[str, bytes] = {}
            recs = []
            for rid, js, mjs in rows:
                rec = json.loads(js)
                for i, mu in enumerate(json.loads(mjs)):
                    r = self._fetch(mu)
                    if isinstance(r, Response):
                        name = f"{rid}/{i}{Path(urlsplit(mu).path).suffix[:8]}"
                        media[name] = r.body
                        rec.setdefault("media", []).append({"name": name, "sha256": hashlib.sha256(r.body).hexdigest(), "url": mu})
                recs.append(rec)
            path = self.out / f"{pack_id}.tar"
            write_pack(path, pack_id, self.a.name, recs, media)
            self.st.db.executemany("INSERT OR REPLACE INTO emitted VALUES (?,?)", [(r[0], pack_id) for r in rows])
            self.st.db.executemany("DELETE FROM pending WHERE id=?", [(r[0],) for r in rows])
            self.st.db.commit()
            self.packs.append(path)
            n = self.st.db.execute("SELECT count(*) FROM pending").fetchone()[0]
