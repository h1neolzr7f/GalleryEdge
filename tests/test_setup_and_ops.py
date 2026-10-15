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


@pytest.mark.parametrize("bad", [
    {"username": "x"}, {"password": "too-short"},
    {"vault_passphrase": "too-short"}, {"port": 0}, {"port": 70000}
])
def test_setup_fails_validation_without_partial_files(tmp_path, bad):
    root = tmp_path / "bad-install"
    kw = {"username": "owner", "password": "FAKE-Strong-Password-8899",
          "vault_passphrase": "FAKE-Vault-Phrase-1234", "port": 8790}
    kw.update(bad)
    with pytest.raises((setup.SetupError, auth.AuthError)):
        setup.apply(root, **kw)
    assert not root.exists(), "failed preflight must not leave behind config/secret material"


def test_setup_invalid_token_file_fails_before_creating_tree(tmp_path):
    root = tmp_path / "bad-token-install"
    token = tmp_path / "invalid-token.txt"
    token.write_text("unsafe")
    token.chmod(0o644)
    with pytest.raises(setup.SetupError, match="0600"):
        setup.apply(root, username="owner", password="FAKE-Strong-Password-8899",
                    vault_passphrase="FAKE-Vault-Phrase-1234", turso_token_file=str(token))
    assert not root.exists()
