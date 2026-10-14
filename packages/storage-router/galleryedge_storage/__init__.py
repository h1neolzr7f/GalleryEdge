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
    def __init__(self, id: str, root: str, tier: str = "hot"):
        self.id, self.tier, self.root = id, tier, Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key):
        return self.root / check_key(key)

    def put(self, key, data):
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


def _now(): return datetime.now(timezone.utc)


def _iso(d): return d.isoformat(timespec="seconds")


@dataclass
class Rule:
    backend_id: str
    prefix: str = ""
    max_size: int | None = None
    priority: int = 100


class Router:
    def __init__(self, conn, backends: list, rules: list[Rule], *, is_up=lambda bid: True, emit=None):
        self.conn, self.b = conn, {x.id: x for x in backends}
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
        return data


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
