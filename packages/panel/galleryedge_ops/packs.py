"""Pack task board state machine (the runner advances; the panel observes / retries)."""
from __future__ import annotations

import json

from .events import audit, emit, now

STAGES = ["download", "verify", "extract", "manifest", "index", "thumbs", "upload", "sync", "done"]
STATUSES = ["pending", "running", "paused", "error", "skipped", "done"]


class PackError(ValueError):
    pass


def _row(conn, pack_id):
    r = conn.execute("SELECT * FROM packs WHERE pack_id=?", (pack_id,))
    cols = [d[0] for d in r.description]
    row = r.fetchone()
    if not row:
        raise PackError(f"no such pack: {pack_id}")
    return dict(zip(cols, row))


def list_packs(conn, status: str | None = None) -> list[dict]:
    q, a = "SELECT * FROM packs", ()
    if status:
        q, a = q + " WHERE status=?", (status,)
    r = conn.execute(q + " ORDER BY updated_at DESC, pack_id", a)
    cols = [d[0] for d in r.description]
    return [dict(zip(cols, x)) for x in r.fetchall()]


def upsert(conn, pack_id: str, stage: str = "download", status: str = "pending", **fields) -> dict:
    if stage not in STAGES or status not in STATUSES:
        raise PackError("bad stage/status")
    cur = conn.execute("SELECT 1 FROM packs WHERE pack_id=?", (pack_id,)).fetchone()
    data = {"stage": stage, "status": status, "updated_at": now(), **fields}
    if "checkpoint" in data:
        data["checkpoint_json"] = json.dumps(data.pop("checkpoint"))
    if cur:
        conn.execute(f"UPDATE packs SET {','.join(k + '=?' for k in data)} WHERE pack_id=?", (*data.values(), pack_id))
    else:
        data["pack_id"] = pack_id
        conn.execute(f"INSERT INTO packs({','.join(data)}) VALUES ({','.join('?' * len(data))})", tuple(data.values()))
    conn.commit()
    return _row(conn, pack_id)


def advance(conn, pack_id: str, *, progress: float | None = None, error: str | None = None) -> dict:
    """Runner API: report progress, finish the current stage, or fail it."""
    p = _row(conn, pack_id)
    if p["status"] in ("paused", "skipped", "done"):
        raise PackError(f"pack is {p['status']}")
    if error:
        emit(conn, "ingest", "error", "pack.error", f"{pack_id} failed at {p['stage']}: {error}")
        return upsert(conn, pack_id, p["stage"], "error", error=error[:500])
    if progress is not None and progress < 1:
        return upsert(conn, pack_id, p["stage"], "running", progress=max(0.0, progress))
    nxt = STAGES[STAGES.index(p["stage"]) + 1]
    emit(conn, "ingest", "info", "pack.stage", f"{pack_id}: {p['stage']} -> {nxt}")
    return upsert(conn, pack_id, nxt, "done" if nxt == "done" else "running", progress=1.0 if nxt == "done" else 0.0, error=None)


def retry(conn, pack_id: str, actor: str = "panel") -> dict:
    p = _row(conn, pack_id)
    if p["status"] != "error":
        raise PackError("only failed packs can be retried")
    audit(conn, actor, "pack.retry", {"pack_id": pack_id, "stage": p["stage"]})
    return upsert(conn, pack_id, p["stage"], "pending", error=None, progress=0.0, attempts=p["attempts"] + 1)


def pause(conn, pack_id: str, actor: str = "panel") -> dict:
    p = _row(conn, pack_id)
    if p["status"] not in ("pending", "running"):
        raise PackError("only pending/running packs can be paused")
    audit(conn, actor, "pack.pause", {"pack_id": pack_id})
    return upsert(conn, pack_id, p["stage"], "paused")


def resume(conn, pack_id: str, actor: str = "panel") -> dict:
    p = _row(conn, pack_id)
    if p["status"] != "paused":
        raise PackError("pack is not paused")
    audit(conn, actor, "pack.resume", {"pack_id": pack_id})
    return upsert(conn, pack_id, p["stage"], "pending")


def skip(conn, pack_id: str, *, confirm: bool, actor: str = "panel") -> dict:
    if not confirm:
        raise PackError("skip requires explicit confirmation")
    p = _row(conn, pack_id)
    if p["status"] == "done":
        raise PackError("pack already done")
    audit(conn, actor, "pack.skip", {"pack_id": pack_id, "stage": p["stage"]})
    return upsert(conn, pack_id, p["stage"], "skipped")


def summary(conn) -> dict:
    return {s: n for s, n in conn.execute("SELECT status, count(*) FROM packs GROUP BY status").fetchall()}
