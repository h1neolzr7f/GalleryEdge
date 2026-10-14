"""Backend health probes with state transitions -> alerts."""
from __future__ import annotations

import os
import shutil
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .events import emit, now


@dataclass
class ProbeResult:
    id: str
    type: str
    ok: bool
    detail: str
    latency_ms: int = 0
    used_bytes: int | None = None
    quota_bytes: int | None = None
    extra: dict = field(default_factory=dict)


def _dir_size(p: Path) -> int:
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def probe_local(b: dict) -> ProbeResult:
    p = Path(b["path"])
    t = time.monotonic()
    if not p.is_dir():
        return ProbeResult(b["id"], "local", False, "path missing or not a directory")
    probe = p / f".ge-probe-{uuid.uuid4().hex}"
    try:
        probe.write_bytes(b"ok")
        ok = probe.read_bytes() == b"ok"
    except OSError as e:
        return ProbeResult(b["id"], "local", False, f"not writable: {e.strerror}")
    finally:
        probe.unlink(missing_ok=True)
    used = _dir_size(p)
    free = shutil.disk_usage(p).free
    return ProbeResult(b["id"], "local", ok, "ok" if ok else "read-back mismatch", int((time.monotonic() - t) * 1000),
                       used, b.get("quota_bytes"), {"disk_free_bytes": free})


def probe_http(b: dict, opener=urllib.request.urlopen) -> ProbeResult:
    t = time.monotonic()
    req = urllib.request.Request(b["url"], method="HEAD" if b["type"] in ("s3", "r2") else "GET",
                                 headers={"User-Agent": "GalleryEdge-Health/0.1"})
    try:
        with opener(req, timeout=b.get("timeout_s", 5)) as r:
            code = getattr(r, "status", 200)
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        return ProbeResult(b["id"], b["type"], False, f"unreachable: {type(e).__name__}")
    # object stores answer 403 to unsigned HEAD; that still proves reachability
    ok = code < 400 or (b["type"] in ("s3", "r2") and code in (401, 403))
    return ProbeResult(b["id"], b["type"], ok, f"http {code}", int((time.monotonic() - t) * 1000))


def probe_libsql(b: dict, token: str | None = None) -> ProbeResult:
    from galleryedge_db.client import connect
    t = time.monotonic()
    try:
        c = connect(b["url"], token)
        ok = c.execute("SELECT 1").rows[0][0] == 1
    except Exception as e:
        return ProbeResult(b["id"], "libsql", False, f"query failed: {type(e).__name__}")
    return ProbeResult(b["id"], "libsql", ok, "ok", int((time.monotonic() - t) * 1000))


def probe(b: dict, *, vault=None, opener=None) -> ProbeResult:
    if b["type"] == "local":
        return probe_local(b)
    if b["type"] == "libsql":
        return probe_libsql(b, vault.resolve(b.get("credential_ref")) if vault else None)
    return probe_http(b, opener) if opener else probe_http(b)


def run_checks(conn, backends: list[dict], alerter, *, fail_threshold: int = 1, vault=None, opener=None) -> list[ProbeResult]:
    results = []
    for b in backends:
        r = probe(b, vault=vault, opener=opener)
        results.append(r)
        row = conn.execute("SELECT status, fail_count FROM backend_status WHERE id=?", (b["id"],)).fetchone()
        prev_status, fails = (row or ("unknown", 0))
        fails = 0 if r.ok else fails + 1
        status = "up" if r.ok else ("down" if fails >= fail_threshold else prev_status if prev_status != "unknown" else "degraded")
        conn.execute("INSERT INTO backend_status(id,type,status,detail,last_probe_at,fail_count) VALUES (?,?,?,?,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET type=excluded.type,status=excluded.status,detail=excluded.detail,"
                     "last_probe_at=excluded.last_probe_at,fail_count=excluded.fail_count",
                     (b["id"], b["type"], status, r.detail, now(), fails))
        conn.commit()
        emit(conn, "health", "info" if r.ok else "error", "probe.ok" if r.ok else "probe.fail", f"{b['id']}: {r.detail}")
        if status == "down" and prev_status != "down":
            alerter.fire(f"backend.down.{b['id']}", "error", f"后端 {b['id']} 不可用 / backend down: {r.detail}", force=True)
        elif status == "up" and prev_status == "down":
            alerter.fire(f"backend.recovered.{b['id']}", "info", f"后端 {b['id']} 已恢复 / backend recovered", force=True)
        q = b.get("quota_bytes")
        if r.ok and q and r.used_bytes is not None and r.used_bytes >= q * b.get("warn_ratio", 0.85):
            alerter.fire(f"quota.{b['id']}", "warn", f"后端 {b['id']} 用量 {r.used_bytes}/{q} 字节，接近上限 / near quota")
    return results
