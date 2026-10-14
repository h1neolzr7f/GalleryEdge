"""Storage routing across backends, hot/cold tiering with verify-before-delete, metering + cost.

Backends implement a tiny interface (put/get/head/delete/list/usage). Shipped:
``LocalBackend`` (directory), ``S3Backend`` (any S3-compatible API incl. R2/MinIO via an
injected boto3-style client), ``MemoryBackend`` (tests / Drive-like adapters plug in the same way).
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Protocol

LEDGERS = frozenset({"done.txt", "seen.txt", "packed_works.txt", "prompts.jsonl"})


class StorageError(Exception):
    pass


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def check_key(key: str) -> str:
    p = PurePosixPath(key)
    if not key or key.startswith("/") or ".." in p.parts or "\\" in key:
        raise StorageError(f"unsafe key {key!r}")
    return key


@dataclass
class Head:
    size: int
    sha256: str | None


class Backend(Protocol):
    id: str
    tier: str
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def head(self, key: str) -> Head | None: ...
    def delete(self, key: str) -> None: ...
    def list(self, prefix: str = "") -> list[str]: ...
    def usage(self) -> tuple[int, int]: ...  # (bytes, objects)


class LocalBackend:
    def __init__(self, id: str, root: str, tier: str = "hot", *, read_only: bool = False):
        self.id, self.tier, self.root, self.read_only = id, tier, Path(root), read_only
        if not read_only:
            self.root.mkdir(parents=True, exist_ok=True)

    def _rw(self):
        if self.read_only:
            raise StorageError(f"{self.id}: read-only backend")

    def _p(self, key):
        return self.root / check_key(key)

    def put(self, key, data):
        self._rw()
        p = self._p(key); p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".put-")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, p)

    def get(self, key):
        try:
            return self._p(key).read_bytes()
        except FileNotFoundError:
            raise StorageError(f"{self.id}: missing {key}") from None

    def head(self, key):
        p = self._p(key)
        return Head(p.stat().st_size, sha(p.read_bytes())) if p.is_file() else None

    def delete(self, key):
        self._rw()
        self._p(key).unlink(missing_ok=True)

    def list(self, prefix=""):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file() and not p.name.startswith(".put-")
                      and str(p.relative_to(self.root)).startswith(prefix))

    def usage(self):
        files = [p for p in self.root.rglob("*") if p.is_file()]
        return sum(p.stat().st_size for p in files), len(files)


class MemoryBackend:
    def __init__(self, id: str, tier: str = "hot", fail: bool = False):
        self.id, self.tier, self.objs, self.fail, self.ops = id, tier, {}, fail, 0

    def _chk(self):
        self.ops += 1
        if self.fail:
            raise StorageError(f"{self.id} unavailable")

    def put(self, key, data):
        self._chk(); self.objs[check_key(key)] = bytes(data)

    def get(self, key):
        self._chk()
        if key not in self.objs:
            raise StorageError(f"{self.id}: missing {key}")
        return self.objs[key]

    def head(self, key):
        self._chk()
        return Head(len(self.objs[key]), sha(self.objs[key])) if key in self.objs else None

    def delete(self, key):
        self._chk(); self.objs.pop(key, None)

    def list(self, prefix=""):
        return sorted(k for k in self.objs if k.startswith(prefix))

    def usage(self):
        return sum(map(len, self.objs.values())), len(self.objs)


class S3Backend:
    """S3-compatible (R2, MinIO, AWS) via a boto3-style client you construct yourself.
    sha256 is stored as object metadata so verification does not depend on ETag semantics."""
    def __init__(self, id: str, client, bucket: str, tier: str = "hot", prefix: str = ""):
        self.id, self.c, self.bucket, self.tier, self.prefix = id, client, bucket, tier, prefix

    def _k(self, key):
        return self.prefix + check_key(key)

    def put(self, key, data):
        self.c.put_object(Bucket=self.bucket, Key=self._k(key), Body=data, Metadata={"sha256": sha(data)})

    def get(self, key):
        try:
            return self.c.get_object(Bucket=self.bucket, Key=self._k(key))["Body"].read()
        except Exception as e:
            raise StorageError(f"{self.id}: get failed ({type(e).__name__})") from None

    def head(self, key):
        try:
            h = self.c.head_object(Bucket=self.bucket, Key=self._k(key))
        except Exception:
            return None
        return Head(int(h["ContentLength"]), (h.get("Metadata") or {}).get("sha256"))

    def delete(self, key):
        self.c.delete_object(Bucket=self.bucket, Key=self._k(key))

    def list(self, prefix=""):
        out, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": self.prefix + prefix}
            if token:
                kw["ContinuationToken"] = token
            r = self.c.list_objects_v2(**kw)
            out += [o["Key"][len(self.prefix):] for o in r.get("Contents", [])]
            if not r.get("IsTruncated"):
                return sorted(out)
            token = r["NextContinuationToken"]

    def usage(self):
        keys = self.list()
        return sum(self.head(k).size for k in keys), len(keys)


SCHEMA = """
CREATE TABLE IF NOT EXISTS objects(key TEXT PRIMARY KEY, backend_id TEXT NOT NULL, tier TEXT NOT NULL, size INTEGER NOT NULL,
  sha256 TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'stored', created_at TEXT NOT NULL, last_access TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS usage_snapshots(ts TEXT NOT NULL, meter TEXT NOT NULL, value REAL NOT NULL, PRIMARY KEY(ts, meter));
"""


class WebDAVBackend:
    """Generic WebDAV (RFC 4918) backend: Nextcloud/ownCloud, Synology, Box/pCloud/Koofr WebDAV, rclone serve webdav...
    Uses PUT/GET/HEAD/DELETE/MKCOL/PROPFIND only; credentials (basic auth) come from the vault, never from config."""

    def __init__(self, id: str, url: str, tier: str = "cold", *, username: str | None = None, password: str | None = None,
                 timeout: float = 30.0, read_only: bool = False, opener=None):
        import urllib.request
        self.id, self.tier, self.read_only, self.timeout = id, tier, read_only, timeout
        self.base = url.rstrip("/") + "/"
        self._auth = None
        if username is not None:
            import base64
            self._auth = "Basic " + base64.b64encode(f"{username}:{password or ''}".encode()).decode()
        self.opener = opener or urllib.request.build_opener()
        self._dirs: set[str] = set()

    def _req(self, method, key="", data=None, headers=None, ok=(200, 201, 204, 207)):
        import urllib.error
        import urllib.request
        from urllib.parse import quote
        url = self.base + quote(key)
        h = {**(headers or {})}
        if self._auth:
            h["Authorization"] = self._auth
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            if e.code in ok:
                return e.code, e.read(), dict(e.headers or {})
            return e.code, b"", {}
        except OSError as e:
            raise StorageError(f"{self.id}: {e}") from None

    def _rw(self):
        if self.read_only:
            raise StorageError(f"{self.id}: read-only backend")

    def _mkdirs(self, key):
        parts = key.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            d = "/".join(parts[:i]) + "/"
            if d in self._dirs:
                continue
            st, _, _ = self._req("MKCOL", d)
            if st not in (201, 405, 301):  # 405 = already exists
                raise StorageError(f"{self.id}: MKCOL {d} -> {st}")
            self._dirs.add(d)

    def put(self, key, data):
        self._rw(); check_key(key); self._mkdirs(key)
        st, _, _ = self._req("PUT", key, data, {"Content-Type": "application/octet-stream"})
        if st not in (200, 201, 204):
            raise StorageError(f"{self.id}: PUT {key} -> {st}")

    def get(self, key):
        st, body, _ = self._req("GET", check_key(key))
        if st != 200:
            raise StorageError(f"{self.id}: missing {key} ({st})")
        return body

    def head(self, key):
        st, _, h = self._req("HEAD", check_key(key))
        if st != 200:
            return None
        return Head(int(h.get("Content-Length") or h.get("content-length") or 0), None)

    def delete(self, key):
        self._rw()
        st, _, _ = self._req("DELETE", check_key(key))
        if st not in (200, 204, 404):
            raise StorageError(f"{self.id}: DELETE {key} -> {st}")

    def _propfind(self, path):
        import xml.etree.ElementTree as ET
        from urllib.parse import unquote, urlsplit
        body = b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/></d:prop></d:propfind>'
        st, xml, _ = self._req("PROPFIND", path, body, {"Depth": "1", "Content-Type": "application/xml"})
        if st == 404:
            return []
        if st != 207:
            raise StorageError(f"{self.id}: PROPFIND {path} -> {st}")
        base_path = urlsplit(self.base).path
        out = []
        for r in ET.fromstring(xml).findall("{DAV:}response"):
            href = unquote(urlsplit(r.findtext("{DAV:}href") or "").path)
            rel = href[len(base_path):] if href.startswith(base_path) else href.lstrip("/")
            if rel.rstrip("/") == path.rstrip("/"):
                continue
            is_dir = r.find(".//{DAV:}collection") is not None
            size = int(r.findtext(".//{DAV:}getcontentlength") or 0)
            out.append((rel, is_dir, size))
        return out

    def _walk(self, path=""):
        for rel, is_dir, size in self._propfind(path):
            if is_dir:
                yield from self._walk(rel if rel.endswith("/") else rel + "/")
            else:
                yield rel, size

    def list(self, prefix=""):
        return sorted(k for k, _ in self._walk() if k.startswith(prefix))

    def usage(self):
        items = list(self._walk())
        return sum(s for _, s in items), len(items)


def _now(): return datetime.now(timezone.utc)


def _iso(d): return d.isoformat(timespec="seconds")


@dataclass
class Rule:
    backend_id: str
    prefix: str = ""
    max_size: int | None = None
    priority: int = 100


class Router:
    def __init__(self, conn, backends: list, rules: list[Rule], *, is_up=lambda bid: True, emit=None,
                 promote_on_read: bool = False, promote_max_bytes: int = 50 * 2**20):
        self.conn, self.b = conn, {x.id: x for x in backends}
        self.promote_on_read, self.promote_max_bytes = promote_on_read, promote_max_bytes
        self.rules = sorted(rules, key=lambda r: r.priority)
        self.is_up, self.emit = is_up, emit or (lambda *a: None)
        conn.executescript(SCHEMA); conn.commit()

    def candidates(self, key: str, size: int) -> list:
        out = [self.b[r.backend_id] for r in self.rules
               if key.startswith(r.prefix) and (r.max_size is None or size <= r.max_size) and r.backend_id in self.b]
        hot = [x for x in out if x.tier == "hot"]
        return list(dict.fromkeys(hot + out))  # hot first, then the rest as fallbacks

    def put(self, key: str, data: bytes, now=None) -> str:
        check_key(key)
        errors = []
        for be in self.candidates(key, len(data)):
            if not self.is_up(be.id):
                errors.append(f"{be.id}: down"); continue
            try:
                be.put(key, data)
                h = be.head(key)
                if not h or h.size != len(data) or (h.sha256 and h.sha256 != sha(data)):
                    raise StorageError("verify after put failed")
            except StorageError as e:
                errors.append(str(e)); self.emit("warn", "storage.failover", f"put {key} failed on {be.id}, trying next"); continue
            t = _iso(now or _now())
            self.conn.execute("INSERT OR REPLACE INTO objects VALUES (?,?,?,?,?,'stored',?,?)", (key, be.id, be.tier, len(data), sha(data), t, t))
            self.conn.commit()
            return be.id
        raise StorageError(f"no backend accepted {key}: {errors}")

    def get(self, key: str, now=None) -> bytes:
        r = self.conn.execute("SELECT backend_id, sha256 FROM objects WHERE key=?", (key,)).fetchone()
        if not r:
            raise StorageError(f"unknown key {key}")
        data = self.b[r[0]].get(key)
        if sha(data) != r[1]:
            raise StorageError(f"integrity error for {key} on {r[0]}")
        self.conn.execute("UPDATE objects SET last_access=? WHERE key=?", (_iso(now or _now()), key)); self.conn.commit()
        if self.promote_on_read and self.b[r[0]].tier != "hot" and len(data) <= self.promote_max_bytes:
            self._promote(key, data, r[0], now)
        return data

    def _promote(self, key, data, src_id, now=None):
        """Copy a cold object back to a hot backend (verified), then drop the cold copy. Failures never break the read."""
        for be in self.candidates(key, len(data)):
            if be.tier != "hot" or be.id == src_id or not self.is_up(be.id) or getattr(be, "read_only", False):
                continue
            try:
                be.put(key, data)
                h = be.head(key)
                if not h or h.size != len(data) or (h.sha256 and h.sha256 != sha(data)):
                    raise StorageError("verify after promote failed")
            except StorageError as e:
                self.emit("warn", "storage.promote_failed", f"promote {key} to {be.id} failed: {e}"); continue
            t = _iso(now or _now())
            self.conn.execute("UPDATE objects SET backend_id=?, tier=?, last_access=? WHERE key=?", (be.id, be.tier, t, key)); self.conn.commit()
            src = self.b[src_id]
            if not getattr(src, "read_only", False) and not protected(key):
                try:
                    src.delete(key)
                except StorageError:
                    pass  # stale cold copy is harmless; cleanup can remove it later
            self.emit("info", "storage.promoted", f"{key}: {src_id} -> {be.id}")
            return be.id
        return None


def protected(key: str) -> str | None:
    if PurePosixPath(key).name in LEDGERS:
        return "ledger"
    if key.endswith((".partial", ".inprogress", ".lock")) or "/staging/" in f"/{key}":
        return "in-progress"
    return None


@dataclass
class Move:
    key: str
    src: str
    dst: str
    size: int


def plan_cold_migration(router: Router, cold_id: str, *, older_than_days: int, now=None) -> tuple[list[Move], list[tuple[str, str]]]:
    cutoff = _iso((now or _now()) - timedelta(days=older_than_days))
    moves, skipped = [], []
    for key, bid, size, state in router.conn.execute(
            "SELECT key, backend_id, size, state FROM objects WHERE tier='hot' AND last_access < ? ORDER BY last_access", (cutoff,)):
        why = protected(key) or (None if state == "stored" else f"state={state}")
        if why:
            skipped.append((key, why))
        else:
            moves.append(Move(key, bid, cold_id, size))
    return moves, skipped


def migrate(router: Router, moves: list[Move], *, dry_run: bool = True) -> dict:
    """Copy -> verify (head + full read-back hash) -> repoint index -> delete source. Never deletes unverified data."""
    done, failed = [], []
    if dry_run:
        return {"dry_run": True, "would_move": len(moves), "bytes": sum(m.size for m in moves)}
    for m in moves:
        src, dst = router.b[m.src], router.b[m.dst]
        row = router.conn.execute("SELECT sha256 FROM objects WHERE key=? AND backend_id=?", (m.key, m.src)).fetchone()
        if not row or protected(m.key):
            failed.append((m.key, "changed or protected")); continue
        want = row[0]
        try:
            data = src.get(m.key)
            if sha(data) != want:
                raise StorageError("source integrity mismatch")
            dst.put(m.key, data)
            h = dst.head(m.key)
            if not h or h.size != len(data) or sha(dst.get(m.key)) != want:
                raise StorageError("destination verify failed")
        except StorageError as e:
            failed.append((m.key, str(e)))
            router.emit("error", "storage.migrate.failed", f"{m.key}: {e} (source kept)")
            continue
        router.conn.execute("UPDATE objects SET backend_id=?, tier=? WHERE key=?", (dst.id, dst.tier, m.key)); router.conn.commit()
        try:
            src.delete(m.key)
        except StorageError as e:  # index already points to verified copy; stale source copy is harmless
            router.emit("warn", "storage.migrate.stale", f"{m.key}: source delete failed: {e}")
        done.append(m.key)
    router.emit("info", "storage.migrate.done", f"moved {len(done)} objects to cold, {len(failed)} failed")
    return {"dry_run": False, "moved": done, "failed": failed}


# ---------------- metering & cost ----------------

def snapshot(conn, meters: dict[str, float], now=None) -> None:
    t = _iso(now or _now())
    conn.executemany("INSERT OR REPLACE INTO usage_snapshots VALUES (?,?,?)", [(t, k, float(v)) for k, v in meters.items()])
    conn.commit()


def forecast(conn, meter: str, quota: float | None, *, window_days: int = 30, now=None) -> dict:
    now = now or _now()
    rows = conn.execute("SELECT ts, value FROM usage_snapshots WHERE meter=? AND ts >= ? ORDER BY ts",
                        (meter, _iso(now - timedelta(days=window_days)))).fetchall()
    if not rows:
        return {"meter": meter, "value": None, "per_day": None, "days_to_quota": None, "quota": quota, "ratio": None}
    pts = [((datetime.fromisoformat(t) - now).total_seconds() / 86400, v) for t, v in rows]
    cur = pts[-1][1]
    slope = None
    if len(pts) >= 2:
        mx = sum(x for x, _ in pts) / len(pts); my = sum(y for _, y in pts) / len(pts)
        den = sum((x - mx) ** 2 for x, _ in pts)
        slope = sum((x - mx) * (y - my) for x, y in pts) / den if den else None
    days = None
    if quota is not None and slope and slope > 0:
        days = max(0.0, (quota - cur) / slope)
    return {"meter": meter, "value": cur, "per_day": slope, "days_to_quota": days, "quota": quota,
            "ratio": (cur / quota) if quota else None}


def monthly_cost(bytes_stored: float, ops_per_month: float, price_gb_month: float = 0.0, price_per_million_ops: float = 0.0,
                 free_gb: float = 0.0) -> float:
    gb = max(0.0, bytes_stored / 1e9 - free_gb)
    return round(gb * price_gb_month + ops_per_month / 1e6 * price_per_million_ops, 4)


# ---------------- factory (credentials only from the encrypted vault) ----------------

def make_backend(b: dict, vault=None, *, client_factory=None):
    """Build a backend from a config entry. S3/R2 credentials come from ``credential_ref``
    (a vault entry holding JSON {"access_key_id": ..., "secret_access_key": ...})."""
    t = b["type"]
    if t == "local":
        return LocalBackend(b["id"], b["path"], b.get("tier", "hot"), read_only=bool(b.get("read_only")))
    if t in ("s3", "r2"):
        if not b.get("bucket"):
            raise StorageError(f"{b['id']}: bucket required")
        creds = {}
        if b.get("credential_ref"):
            if vault is None:
                raise StorageError(f"{b['id']}: vault locked, cannot read {b['credential_ref']}")
            creds = json.loads(vault.resolve(b["credential_ref"]))
        if client_factory is None:
            try:
                import boto3  # type: ignore
                from botocore.config import Config  # type: ignore
            except ImportError:
                raise StorageError("install galleryedge[storage] (boto3) for s3/r2 backends") from None
            client_factory = lambda **kw: boto3.client("s3", config=Config(signature_version="s3v4", retries={"max_attempts": 3}), **kw)  # noqa: E731
        client = client_factory(endpoint_url=b.get("url"), region_name=b.get("region", "auto"),
                                aws_access_key_id=creds.get("access_key_id"), aws_secret_access_key=creds.get("secret_access_key"))
        return S3Backend(b["id"], client, b["bucket"], b.get("tier", "hot"), b.get("prefix", ""))
    if t == "webdav":
        creds = {}
        if b.get("credential_ref"):
            if vault is None:
                raise StorageError(f"{b['id']}: vault locked, cannot read {b['credential_ref']}")
            creds = json.loads(vault.resolve(b["credential_ref"]))
        return WebDAVBackend(b["id"], b["url"], b.get("tier", "cold"), username=creds.get("username"), password=creds.get("password"),
                             read_only=bool(b.get("read_only")))
    raise StorageError(f"{b['id']}: unsupported backend type {t}")


def add_to_monthly_counter(conn, meter: str, delta: float, now=None) -> float:
    """Accumulate a per-calendar-month counter (e.g. Turso rows written) and snapshot it."""
    conn.executescript(SCHEMA)
    now = now or _now()
    month = now.strftime("%Y-%m")
    r = conn.execute("SELECT value FROM usage_snapshots WHERE meter=? AND ts LIKE ? ORDER BY ts DESC LIMIT 1", (meter, month + "%")).fetchone()
    v = (r[0] if r else 0.0) + float(delta)
    snapshot(conn, {meter: v}, now=now)
    return v
