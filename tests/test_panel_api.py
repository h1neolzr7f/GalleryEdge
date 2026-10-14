import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from galleryedge_ops import config, db, packs
from galleryedge_panel.demo import seed
from galleryedge_panel.server import create_app

EX = json.loads((Path(__file__).parents[1] / "examples/config.example.json").read_text())
H = {"X-GE-Panel": "1"}


def make(tmp_path, takeover=True, **panel):
    cfg = copy.deepcopy(EX)
    cfg["panel"].update(takeover=takeover, **panel)
    p = tmp_path / "config.json"; p.write_text(json.dumps(cfg))
    cfg = config.load(p)
    for b in cfg["backends"]:
        Path(b["path"]).mkdir(parents=True, exist_ok=True)
    conn = db.connect(":memory:"); db.migrate(conn); seed(conn)
    return TestClient(create_app(cfg, conn)), conn, cfg


def test_status_and_health(tmp_path):
    c, conn, cfg = make(tmp_path)
    s = c.get("/api/status").json()
    assert s["packs"]["error"] == 2 and s["alerts_open"] == 2 and s["takeover"] is True
    Path(cfg["backends"][1]["path"]).rmdir()
    res = c.post("/api/health/run", headers=H).json()
    assert [r["ok"] for r in res] == [True, False]
    s = c.get("/api/status").json()
    assert {b["id"]: b["status"] for b in s["backends"]} == {"local-hot": "up", "local-cold": "down"}


def test_pack_board_retry_and_guards(tmp_path):
    c, conn, _ = make(tmp_path)
    items = c.get("/api/packs").json()["items"]
    assert len(items) == 8
    r = c.post("/api/packs/demo-pack-0005/retry", headers=H)
    assert r.status_code == 200 and r.json()["status"] == "pending" and r.json()["attempts"] == 1
    assert c.post("/api/packs/demo-pack-0005/retry", headers=H).status_code == 409
    assert c.post("/api/packs/demo-pack-0006/skip", headers=H, json={}).status_code == 409
    assert c.post("/api/packs/demo-pack-0006/skip", headers=H, json={"confirm": True}).json()["status"] == "skipped"
    assert c.post("/api/packs/demo-pack-0007/resume", headers=H).json()["status"] == "pending"
    assert c.post("/api/packs/demo-pack-0007/retry").status_code == 403  # no CSRF header
    assert [a["action"] for a in c.get("/api/audit").json()][:1] == ["pack.resume"]


def test_observe_mode_blocks_writes(tmp_path):
    c, _, _ = make(tmp_path, takeover=False)
    r = c.post("/api/packs/demo-pack-0005/retry", headers=H)
    assert r.status_code == 403 and "takeover" in r.json()["detail"]


def test_alerts_ack_and_events_filter(tmp_path):
    c, _, _ = make(tmp_path)
    al = c.get("/api/alerts").json()
    assert len(al) == 2
    assert c.post(f"/api/alerts/{al[0]['id']}/ack", headers=H).json() == {"ok": True}
    assert len(c.get("/api/alerts").json()) == 1 and len(c.get("/api/alerts?all=true").json()) == 2
    ev = c.get("/api/events?source=ingest").json()
    assert ev and all(e["source"] == "ingest" for e in ev)
    assert all(e["level"] == "error" for e in c.get("/api/events?level=error").json())


def test_cleanup_preview_then_confirm(tmp_path):
    c, _, cfg = make(tmp_path)
    root = Path(cfg["cleanup"]["roots"][0]); v = root / "pack-a"; v.mkdir(parents=True)
    (v / ".verified_remote").write_text(""); (v / "img.png").write_bytes(b"12345"); (root / "done.txt").write_text("ledger")
    (root / "pending.tar").write_bytes(b"x")
    plan = c.get("/api/cleanup/plan").json()
    assert [d["path"].endswith("img.png") for d in plan["delete"]] == [True] and plan["bytes"] == 5
    reasons = {Path(p["path"]).name: p["reason"] for p in plan["protected"]}
    assert reasons["done.txt"] == "ledger" and reasons["pending.tar"] == "tar not uploaded"
    assert c.post("/api/cleanup/apply", headers=H, json={"token": plan["token"]}).status_code == 400
    (v / "new.png").write_bytes(b"1")  # plan changed after preview
    assert c.post("/api/cleanup/apply", headers=H, json={"token": plan["token"], "confirm": True}).status_code == 409
    plan = c.get("/api/cleanup/plan").json()
    r = c.post("/api/cleanup/apply", headers=H, json={"token": plan["token"], "confirm": True}).json()
    assert r["deleted"] == 2 and (root / "done.txt").exists() and (root / "pending.tar").exists()


def test_localhost_only(tmp_path):
    c, _, _ = make(tmp_path)
    assert c.get("/api/status", headers={"Host": "evil.example"}).status_code == 403
    remote = TestClient(c.app, client=("203.0.113.9", 5555))
    assert remote.get("/api/status").status_code == 403
    r = c.get("/api/status")
    assert r.headers["X-Frame-Options"] == "DENY"


def test_allow_remote_opt_in(tmp_path):
    c, _, _ = make(tmp_path, bind="0.0.0.0", allow_remote=True)
    assert TestClient(c.app, client=("203.0.113.9", 5555)).get("/api/status").status_code == 200
