"""Synthetic panel state for demos/screenshots. No real packs."""
from galleryedge_ops import packs
from galleryedge_ops.alerts import Alerter
from galleryedge_ops.events import emit


def seed(conn) -> None:
    if conn.execute("SELECT count(*) FROM packs").fetchone()[0]:
        return
    plan = [("demo-pack-0001", "done", "done", 1.0, None), ("demo-pack-0002", "done", "done", 1.0, None),
            ("demo-pack-0003", "upload", "running", 0.62, None), ("demo-pack-0004", "thumbs", "running", 0.3, None),
            ("demo-pack-0005", "verify", "error", 0.0, "checksum mismatch (synthetic)"), ("demo-pack-0006", "download", "pending", 0.0, None),
            ("demo-pack-0007", "index", "paused", 0.4, None), ("demo-pack-0008", "upload", "error", 0.8, "remote timeout (synthetic)")]
    for pid, stage, st, prog, err in plan:
        packs.upsert(conn, pid, stage, st, progress=prog, error=err, backend_id="local-hot", bytes_local=10_000_000)
    for src, lvl, code, msg in [("crawler", "info", "crawler.heartbeat", "demo crawler alive"), ("ingest", "error", "pack.error", "demo-pack-0005 failed at verify"),
                                ("edge", "info", "edge.probe", "worker /healthz ok"), ("agent", "warn", "disk.high", "local disk 81% used (synthetic)")]:
        emit(conn, src, lvl, code, msg)
    emit(conn, "crawler", "info", "crawler.skips", "demo-selfhost skipped: login-wall=2, no-license=1, paywall=1, robots-disallow=1",
         {"adapter": "demo-selfhost", "reasons": {"login-wall": 2, "no-license": 1, "paywall": 1, "robots-disallow": 1}})
    emit(conn, "crawler", "info", "crawler.done", "demo-selfhost done: 60 records, 1 packs",
         {"adapter": "demo-selfhost", "fetched": 67, "records": 60, "skipped": 5, "dupes": 0, "pages_total": 64, "packs": 1})
    A = Alerter(conn)
    A.fire("backend.down.local-cold", "error", "后端 local-cold 不可用 / backend down: path missing (synthetic)")
    A.fire("credential.expiring.demo-r2", "warn", "凭据 demo-r2 5 天后到期，请轮换 (synthetic)")
