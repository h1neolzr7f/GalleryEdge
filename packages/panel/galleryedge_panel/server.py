"""FastAPI backend for the local ops panel.

Safety: binds 127.0.0.1 by default; rejects non-loopback clients and foreign Host
headers (DNS rebinding) unless ``panel.allow_remote``; every write needs the
``X-GE-Panel: 1`` header (forces a CORS preflight, which we never allow) so other
websites cannot drive the panel; pack writes need ``panel.takeover``.
"""
from __future__ import annotations

import hashlib
import ipaddress
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from galleryedge_ops import alerts as alerts_mod
from galleryedge_ops import cleanup, events, health, packs
from galleryedge_ops.masking import redact_obj

WEB_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}


def plan_token(pl) -> str:
    h = hashlib.sha256()
    for p in sorted(pl.delete):
        h.update(f"{p}\0{p.lstat().st_size}\n".encode())
    return h.hexdigest()[:32]


def _is_loopback(host: str | None) -> bool:
    if host in ("testclient", "testserver"):
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def create_app(cfg: dict, conn, vault=None, *, alerter=None) -> FastAPI:
    app = FastAPI(title="GalleryEdge panel", docs_url=None, redoc_url=None, openapi_url=None)
    panel = cfg["panel"]
    allow_remote = bool(panel.get("allow_remote"))
    takeover = bool(panel.get("takeover"))
    A = alerter or alerts_mod.Alerter(conn, dedupe_minutes=cfg["alerts"].get("dedupe_minutes", 30))
    roots = cfg.get("cleanup", {}).get("roots", [])
    extra = cfg.get("cleanup", {}).get("extra_protected")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not allow_remote:
            host_hdr = request.url.hostname or ""
            if not _is_loopback(request.client.host if request.client else None) or host_hdr not in LOCAL_HOSTS:
                return JSONResponse({"error": "panel is localhost-only"}, status_code=403)
        if request.method not in ("GET", "HEAD") and request.headers.get("x-ge-panel") != "1":
            return JSONResponse({"error": "missing X-GE-Panel header"}, status_code=403)
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def need_takeover():
        if not takeover:
            raise HTTPException(403, "read-only observe mode: enable panel.takeover to change pack tasks")

    def rows(sql, args=()):
        r = conn.execute(sql, args)
        cols = [d[0] for d in r.description]
        return [dict(zip(cols, x)) for x in r.fetchall()]

    def crawler_status():
        import json as _j
        hb = rows("SELECT ts,code,message,payload FROM events WHERE source='crawler' ORDER BY id DESC LIMIT 1")
        sk = rows("SELECT payload FROM events WHERE source='crawler' AND code='crawler.skips' ORDER BY id DESC LIMIT 1")
        if not hb:
            return None
        last = hb[0]
        return {"last_ts": last["ts"], "last_code": last["code"], "last_message": last["message"],
                "stats": _j.loads(last["payload"] or "{}"), "skip_reasons": _j.loads(sk[0]["payload"]).get("reasons", {}) if sk else {}}

    @app.get("/api/status")
    def status():
        expiring = vault.expiring(cfg.get("vault", {}).get("remind_days", 14)) if vault else []
        return {
            "takeover": takeover,
            "backends": rows("SELECT id,type,status,detail,last_probe_at,fail_count FROM backend_status ORDER BY id"),
            "configured_backends": [b["id"] for b in cfg["backends"]],
            "packs": packs.summary(conn),
            "alerts_open": conn.execute("SELECT count(*) FROM alerts WHERE acked=0").fetchone()[0],
            "credentials_expiring": [{k: e[k] for k in ("name", "days_left", "expired")} for e in expiring],
            "crawler": crawler_status(),
            "recent_events": rows("SELECT id,ts,source,level,code,message FROM events ORDER BY id DESC LIMIT 10"),
        }

    @app.post("/api/health/run")
    def run_health():
        res = health.run_checks(conn, cfg["backends"], A, fail_threshold=cfg["alerts"].get("fail_threshold", 1), vault=vault)
        events.audit(conn, "panel", "health.run", {"n": len(res)})
        return [redact_obj(r.__dict__) for r in res]

    @app.get("/api/packs")
    def get_packs(status: str | None = None):
        return {"stages": packs.STAGES, "items": packs.list_packs(conn, status), "takeover": takeover}

    def pack_action(fn):
        try:
            return fn()
        except packs.PackError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/packs/{pack_id}/retry")
    def retry(pack_id: str):
        need_takeover(); return pack_action(lambda: packs.retry(conn, pack_id))

    @app.post("/api/packs/{pack_id}/pause")
    def pause(pack_id: str):
        need_takeover(); return pack_action(lambda: packs.pause(conn, pack_id))

    @app.post("/api/packs/{pack_id}/resume")
    def resume(pack_id: str):
        need_takeover(); return pack_action(lambda: packs.resume(conn, pack_id))

    @app.post("/api/packs/{pack_id}/skip")
    def skip(pack_id: str, body: dict = Body(default={})):
        need_takeover(); return pack_action(lambda: packs.skip(conn, pack_id, confirm=body.get("confirm") is True))

    @app.get("/api/alerts")
    def get_alerts(all: bool = False):
        return [dict(zip(["id", "ts", "key", "level", "message", "acked", "delivered"], r)) for r in A.list(all)]

    @app.post("/api/alerts/{alert_id}/ack")
    def ack(alert_id: int):
        A.ack(alert_id); events.audit(conn, "panel", "alert.ack", {"id": alert_id}); return {"ok": True}

    @app.get("/api/events")
    def get_events(source: str | None = None, level: str | None = None, limit: int = 100):
        q, a = "SELECT id,ts,source,level,code,message FROM events WHERE 1=1", []
        if source:
            q += " AND source=?"; a.append(source)
        if level:
            q += " AND level=?"; a.append(level)
        return rows(q + " ORDER BY id DESC LIMIT ?", (*a, min(max(limit, 1), 1000)))

    @app.get("/api/audit")
    def get_audit(limit: int = 100):
        return rows("SELECT id,ts,actor,action,change FROM audit ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 1000),))

    @app.get("/api/cleanup/plan")
    def cleanup_plan():
        pl = cleanup.plan(roots, extra)
        return {"roots": roots, "token": plan_token(pl), "bytes": pl.bytes,
                "delete": [{"path": str(p), "size": p.lstat().st_size} for p in pl.delete],
                "protected": [{"path": str(p), "reason": why} for p, why in pl.protected]}

    @app.post("/api/cleanup/apply")
    def cleanup_apply(body: dict = Body(...)):
        if body.get("confirm") is not True:
            raise HTTPException(400, "explicit confirm required")
        pl = cleanup.plan(roots, extra)
        if body.get("token") != plan_token(pl):
            raise HTTPException(409, "files changed since preview; preview again")
        events.audit(conn, "panel", "cleanup.start", {"files": len(pl.delete)})
        done = cleanup.apply(roots, dry_run=False, extra_protected=extra)
        events.audit(conn, "panel", "cleanup.done", {"deleted": len(done.delete), "bytes": done.bytes})
        events.emit(conn, "agent", "info", "cleanup.done", f"cleanup deleted {len(done.delete)} files ({done.bytes} bytes)")
        return {"deleted": len(done.delete), "bytes": done.bytes}

    if WEB_DIST.is_dir():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            return FileResponse(WEB_DIST / "index.html")
    return app
