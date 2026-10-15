"""First-run setup wizard + visual ops job queue."""
import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from galleryedge_ops import auth, db, jobs, setup
from galleryedge_ops.vault import Vault
from galleryedge_panel.cli import run_setup_wizard
from galleryedge_panel.server import create_app
from galleryedge_ops import config

from test_panel_api import ADMIN_PW, H, make


def test_apply_creates_tree(tmp_path, monkeypatch):
    monkeypatch.delenv("GE_VAULT_PASSPHRASE", raising=False)
    root = tmp_path / "data"
    r = setup.apply(root, username="dick", password=ADMIN_PW, vault_passphrase="FAKE-vault-pass-phrase-99", port=8793)
    assert (root / "config.json").exists()
    assert (root / "var" / "vault-passphrase.txt").stat().st_mode & 0o777 == 0o600
    assert "FAKE" not in json.dumps(r)  # no secret material in return
    cfg = config.load(root / "config.json")
    assert cfg["panel"]["bind"] == "127.0.0.1"
    conn = db.connect(cfg["database"]["primary"]); db.migrate(conn)
    assert auth.user_count(conn) == 1
    with pytest.raises(setup.SetupError, match="already"):
        setup.apply(root, username="dick", password=ADMIN_PW, vault_passphrase="FAKE-vault-pass-phrase-99")


def test_apply_turso_token_file_must_be_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("GE_VAULT_PASSPHRASE", "FAKE-vault-pass-phrase-99")
    tf = tmp_path / "tok.json"
    tf.write_text(json.dumps({"db_url": "libsql://x.invalid", "ro_token": "t"}))
    os.chmod(tf, 0o644)
    with pytest.raises(setup.SetupError, match="0600"):
        setup.apply(tmp_path / "d", username="dick", password=ADMIN_PW, vault_passphrase="FAKE-vault-pass-phrase-99",
                    turso_token_file=str(tf))


def test_wizard_http_localhost_only(tmp_path, monkeypatch):
    monkeypatch.setenv("GE_VAULT_PASSPHRASE", "FAKE-vault-pass-phrase-99")
    # Build the wizard app the same way as run_setup_wizard, but without uvicorn
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.responses import JSONResponse
    from galleryedge_ops.setup import apply, wizard_defaults
    app = FastAPI()
    root = str(tmp_path / "w")

    @app.middleware("http")
    async def local_only(request, call_next):
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1", "testclient", "testserver"):
            return JSONResponse({"error": "setup wizard is localhost-only"}, status_code=403)
        return await call_next(request)

    @app.get("/api/setup/defaults")
    def defaults():
        return wizard_defaults(root)

    @app.post("/api/setup/apply")
    def do_apply(body: dict = Body(...)):
        try:
            return apply(body.get("root") or root, username=body["username"], password=body["password"],
                         vault_passphrase=body["vault_passphrase"], port=int(body.get("port") or 8790))
        except Exception as e:
            raise HTTPException(400, str(e))

    c = TestClient(app)
    assert c.get("/api/setup/defaults").json()["root"] == str(Path(root).resolve())
    r = c.post("/api/setup/apply", json={"username": "dick", "password": ADMIN_PW, "vault_passphrase": "FAKE-vault-pass-phrase-99", "port": 8794})
    assert r.status_code == 200 and r.json()["admin"] == "dick"
    remote = TestClient(app, client=("203.0.113.9", 1))
    assert remote.get("/api/setup/defaults").status_code == 403


def test_ops_crawl_synthetic_job(tmp_path):
    c, conn, cfg = make(tmp_path)
    # attach job runner like create_app does — make() already used create_app which now starts runner
    cat = c.get("/api/ops/catalog").json()
    assert "synthetic" in [a["id"] for a in cat["adapters"]] and cat["commons_categories"]
    r = c.post("/api/ops/crawl", headers=H, json={"adapter": "synthetic", "n": 5, "max_pages": 20, "confirm": True})
    assert r.status_code == 200 and "id" in r.json()
    jid = r.json()["id"]
    for _ in range(80):
        j = c.get(f"/api/ops/jobs/{jid}").json()
        if j["status"] in ("done", "error"):
            break
        time.sleep(0.25)
    assert j["status"] == "done", j
    assert json.loads(j["result"])["records"] >= 1
    packs = c.get("/api/ops/packs").json()["packs"]
    assert packs


def test_ops_requires_confirm(tmp_path):
    c, conn, cfg = make(tmp_path)
    assert c.post("/api/ops/crawl", headers=H, json={"adapter": "synthetic"}).status_code == 400


def test_cf_rejects_non_galleryedge(tmp_path):
    c, conn, cfg = make(tmp_path)
    r = c.post("/api/ops/cf/deploy", headers=H, json={"script": "nai", "confirm": True, "cred_file": "/tmp/x"})
    assert r.status_code == 400 and "galleryedge" in r.text


def test_keys_create_writes_0600_file(tmp_path):
    c, conn, cfg = make(tmp_path)
    c.post("/api/ops/keys/init", headers=H, json={"confirm": True})
    time.sleep(0.4)
    r = c.post("/api/ops/keys/create", headers=H, json={"label": "t", "confirm": True})
    jid = r.json()["id"]
    for _ in range(30):
        j = c.get(f"/api/ops/jobs/{jid}").json()
        if j["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert j["status"] == "done", j
    out = json.loads(j["result"])["out"]
    assert Path(out).stat().st_mode & 0o777 == 0o600
    key = Path(out).read_text().strip()
    assert key.startswith("ge_") and key not in j["message"] and key not in j["result"]



def test_real_setup_wizard_rejects_rebinding_and_cross_origin(tmp_path, monkeypatch):
    """Test the actual wizard middleware, not a hand-written duplicate."""
    import uvicorn
    from galleryedge_ops import setup as setup_module

    seen = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.append(app))
    root = tmp_path / "safe-install"
    applied = []
    def fake_apply(target, **kwargs):
        applied.append(str(target))
        return {"ok": True}
    monkeypatch.setattr(setup_module, "apply", fake_apply)
    assert run_setup_wizard(str(root), 8799) == 0
    app = seen[0]
    client = TestClient(app)
    payload = {"root": str(tmp_path / "untrusted-location"), "username": "admin",
               "password": ADMIN_PW, "vault_passphrase": "FAKE-test-vault"}

    assert client.post("/api/setup/apply", json=payload).status_code == 403
    assert client.get("/api/setup/defaults", headers={"Host": "rebind.example"}).status_code == 403
    assert client.post("/api/setup/apply", json=payload,
                       headers={"X-GE-Panel": "1", "Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/setup/apply", json=payload,
                       headers={"X-GE-Panel": "1"}).status_code == 200
    assert applied == [str(root.resolve())]


def test_abandoned_running_job_is_marked_interrupted(tmp_path):
    conn = db.connect(str(tmp_path / "jobs.db"))
    db.migrate(conn)
    jobs.migrate(conn)
    jid = jobs.enqueue(conn, "crawl", "admin", {"adapter": "synthetic"})
    conn.execute("UPDATE jobs SET status='running' WHERE id=?", (jid,))
    conn.commit()
    runner = jobs.JobRunner(conn, {"panel": {"state_dir": str(tmp_path)}})
    runner.start()
    try:
        job = jobs.get_job(conn, jid)
        assert job["status"] == "error"
        assert "中断" in job["message"]
    finally:
        runner.stop()



def test_setup_does_not_leak_vault_passphrase_into_npm(tmp_path, monkeypatch):
    import subprocess
    import shutil
    from galleryedge_ops import setup
    secret = "FAKE-only-for-setup-very-long"
    monkeypatch.setenv("GE_VAULT_PASSPHRASE", "FAKE-preexisting-value")
    monkeypatch.setattr(shutil, "which", lambda program: "/usr/bin/npm")
    seen = []
    def fake_run(cmd, **kw):
        assert "GE_VAULT_PASSPHRASE" not in kw["env"]
        assert secret not in repr(kw)
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    data = tmp_path / "isolated"
    setup.apply(data, username="admin", password=ADMIN_PW, vault_passphrase=secret)
    assert seen == [["npm", "ci"], ["npm", "run", "build"]]
    assert os.environ["GE_VAULT_PASSPHRASE"] == "FAKE-preexisting-value"
    assert (data / "var/vault-passphrase.txt").read_text().strip() == secret
