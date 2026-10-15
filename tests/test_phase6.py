"""Phase 6: auth, invites, roles, config center, vault, releases, self-check, backup drill."""
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from galleryedge_ops import auth, db, releases
from galleryedge_ops.vault import Vault
from galleryedge_panel import cli
from galleryedge_panel.server import create_app

from test_panel_api import ADMIN_PW, H, make

VPW = "FAKE-vault-passphrase-1"


# ---------- auth core ----------
def mem():
    c = db.connect(":memory:"); db.migrate(c); return c


def test_password_policy_and_hash():
    for bad in ("short", "aaaaaaaaaaaaaaa", "bob-is-my-password"):
        with pytest.raises(auth.AuthError):
            auth.check_password_policy(bad, "bob")
    h = auth.hash_password("FAKE-correct-horse-1", n=2**10)
    assert "FAKE" not in h and auth.verify_password("FAKE-correct-horse-1", h) and not auth.verify_password("x", h)


def test_invite_only_register_and_single_use():
    c = mem(); auth.create_user(c, "root", ADMIN_PW, "admin")
    with pytest.raises(auth.AuthError):
        auth.register(c, "inv_nope", "eve", "FAKE-eve-password-1")
    code = auth.create_invite(c, "root", "viewer")
    assert code not in json.dumps(auth.list_invites(c))  # only hash stored
    auth.register(c, code, "bob", "FAKE-viewer-pass-77")
    with pytest.raises(auth.AuthError):
        auth.register(c, code, "carol", "FAKE-viewer-pass-77")
    assert {u["username"]: u["role"] for u in auth.list_users(c)} == {"root": "admin", "bob": "viewer"}


def test_expired_invite_rejected():
    c = mem(); auth.create_user(c, "root", ADMIN_PW, "admin")
    code = auth.create_invite(c, "root", "viewer", ttl_days=0)
    with pytest.raises(auth.AuthError):
        auth.register(c, code, "bob", "FAKE-viewer-pass-77")


def test_lockout_and_sessions():
    c = mem(); auth.create_user(c, "root", ADMIN_PW, "admin")
    for _ in range(5):
        with pytest.raises(auth.AuthError):
            auth.login(c, "root", "FAKE-wrong-pass-000")
    with pytest.raises(auth.AuthError, match="too many"):
        auth.login(c, "root", ADMIN_PW)
    c2 = mem(); auth.create_user(c2, "root", ADMIN_PW, "admin")
    t = auth.login(c2, "root", ADMIN_PW)
    assert auth.session_user(c2, t)["username"] == "root"
    auth.logout(c2, t); assert auth.session_user(c2, t) is None


def test_last_admin_and_self_protection():
    c = mem(); auth.create_user(c, "root", ADMIN_PW, "admin"); auth.create_user(c, "bob", "FAKE-viewer-pass-77")
    with pytest.raises(auth.AuthError):
        auth.set_status(c, "root", "root", "disabled")
    auth.set_status(c, "root", "bob", "disabled")
    with pytest.raises(auth.AuthError):
        auth.login(c, "bob", "FAKE-viewer-pass-77")


def test_change_password_revokes_old():
    c = mem(); auth.create_user(c, "root", ADMIN_PW, "admin")
    with pytest.raises(auth.AuthError):
        auth.change_password(c, "root", "FAKE-wrong-pass-000", "FAKE-new-pass-55555")
    auth.change_password(c, "root", ADMIN_PW, "FAKE-new-pass-55555")
    auth.login(c, "root", "FAKE-new-pass-55555")


# ---------- HTTP ----------
def test_api_requires_session(tmp_path):
    c, conn, cfg = make(tmp_path)
    anon = TestClient(c.app)
    assert anon.get("/api/status").status_code == 401
    assert anon.get("/api/auth/state").json()["user"] is None
    assert anon.post("/api/auth/login", headers=H, json={"username": "admin", "password": "FAKE-bad-pass-000"}).status_code == 401
    r = anon.post("/api/auth/login", headers=H, json={"username": "admin", "password": ADMIN_PW})
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=strict" in sc
    assert anon.post("/api/auth/logout", headers=H).status_code == 200
    assert anon.get("/api/status").status_code == 401


def test_invite_flow_and_viewer_readonly(tmp_path):
    c, conn, cfg = make(tmp_path)
    code = c.post("/api/invites", headers=H, json={"role": "viewer"}).json()["code"]
    v = TestClient(c.app)
    assert v.post("/api/auth/register", headers=H, json={"code": code, "username": "bob", "password": "FAKE-viewer-pass-77"}).status_code == 200
    v.post("/api/auth/login", headers=H, json={"username": "bob", "password": "FAKE-viewer-pass-77"})
    assert v.get("/api/status").status_code == 200
    assert v.post("/api/health/run", headers=H).status_code == 403
    assert v.post("/api/invites", headers=H, json={}).status_code == 403
    assert v.post("/api/auth/password", headers=H, json={"old": "FAKE-viewer-pass-77", "new": "FAKE-viewer-pass-88"}).status_code == 200
    users = c.get("/api/users").json()
    assert {u["username"] for u in users["users"]} == {"admin", "bob"} and "pass" not in json.dumps(users).lower().replace("password_changed", "")
    assert c.post("/api/users/bob/disabled", headers=H).status_code == 200
    assert v.get("/api/status").status_code == 401


def test_config_center_masks_and_vault(tmp_path, monkeypatch):
    c, conn, cfg = make(tmp_path)
    vault = Vault(tmp_path / "v.vault", VPW, scrypt_n=2**10)
    vault.set("r2", '{"access_key_id":"FAKEAKID","secret_access_key":"FAKE-SECRET-VALUE-XYZ"}')
    app = create_app(cfg, conn, vault)
    cl = TestClient(app); cl.post("/api/auth/login", headers=H, json={"username": "admin", "password": ADMIN_PW})
    body = cl.get("/api/config").json()
    assert body["vault_unlocked"] and [x["name"] for x in body["credentials"]] == ["r2"]
    assert "FAKE-SECRET-VALUE-XYZ" not in json.dumps(body)
    r = cl.post("/api/vault/r2", headers=H, json={"value": "FAKE-ROTATED-SECRET-1", "expires_days": 30}).json()
    assert r["version"] == 2 and "ROTATED" not in json.dumps(r)
    assert cl.post("/api/vault/bad name", headers=H, json={"value": "x"}).status_code in (400, 404)
    assert vault.get("r2") == "FAKE-ROTATED-SECRET-1"
    acts = [a for (a,) in conn.execute("SELECT action FROM audit")]
    assert "vault.rotate" in acts
    # no vault -> 409
    assert c.post("/api/vault/x", headers=H, json={"value": "y"}).status_code == 409


def test_config_put_validates_and_keeps_history(tmp_path):
    c, conn, cfg = make(tmp_path)
    raw = c.get("/api/config").json()["config"]
    bad = dict(raw); bad.pop("panel")
    assert c.put("/api/config", headers=H, json={"config": bad}).status_code == 422
    raw["tiering"] = {**raw.get("tiering", {}), "hot_to_cold_days": 45}
    r = c.put("/api/config", headers=H, json={"config": raw}).json()
    assert r["restart_required"]
    assert json.loads(Path(cfg["_path"]).read_text())["tiering"]["hot_to_cold_days"] == 45
    assert (Path(cfg["panel"]["state_dir"]) / "config-history" / r["previous"]).exists()


# ---------- releases ----------
def art(tmp_path, ver):
    d = tmp_path / f"art-{ver}"; d.mkdir(); (d / "worker.js").write_text(f"// {ver}\n"); return d


def test_release_deploy_rollback_and_integrity(tmp_path):
    st = releases.ReleaseStore(tmp_path / "deploy")
    backups = []
    st.deploy(art(tmp_path, "1.0.0"), "1.0.0", migration=2, backup_fn=backups.append)
    st.deploy(art(tmp_path, "1.1.0"), "1.1.0", migration=3, backup_fn=backups.append)
    assert st.current() == "1.1.0"
    with pytest.raises(releases.ReleaseError):
        st.deploy(art(tmp_path, "x"), "1.1.0", migration=3)  # immutable
    with pytest.raises(releases.ReleaseError):
        st.deploy(tmp_path, "../evil", migration=3)
    ev = st.rollback(backup_fn=backups.append)
    assert ev["to"] == "1.0.0" and ev["db_restore_required"] is True and st.current() == "1.0.0"
    assert backups  # backup taken before each switch
    (tmp_path / "deploy" / "releases" / "1.1.0" / "worker.js").write_text("tampered")
    assert not st.verify("1.1.0")
    with pytest.raises(releases.ReleaseError):
        st.rollback(to="1.1.0")
    assert [h["action"] for h in st.history()][-1] == "rollback"


def test_release_api_requires_confirm(tmp_path):
    c, conn, cfg = make(tmp_path)
    st = c.app.state.release_store
    st.deploy(art(tmp_path, "0.5.0"), "0.5.0", migration=2)
    st.deploy(art(tmp_path, "0.6.0"), "0.6.0", migration=3)
    assert c.post("/api/releases/rollback", headers=H, json={}).status_code == 400
    r = c.post("/api/releases/rollback", headers=H, json={"confirm": True}).json()
    assert r["to"] == "0.5.0"
    assert c.get("/api/releases").json()["current"] == "0.5.0"


def test_cli_admin_and_release(tmp_path, monkeypatch, capsys):
    import copy
    from test_panel_api import EX
    cfg = copy.deepcopy(EX); cfg["panel"]["state_dir"] = str(tmp_path / "st"); cfg["database"]["primary"] = str(tmp_path / "p.db")
    p = tmp_path / "c.json"; p.write_text(json.dumps(cfg))
    monkeypatch.delenv("GE_VAULT_PASSPHRASE", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("FAKE-cli-admin-pw-1\n"))
    assert cli.main(["--config", str(p), "admin", "create", "owner"]) == 0
    assert cli.main(["--config", str(p), "release", "deploy", str(art(tmp_path, "a")), "--version", "1.0.0"]) == 0
    assert cli.main(["--config", str(p), "release", "deploy", str(art(tmp_path, "b")), "--version", "1.0.1"]) == 0
    assert cli.main(["--config", str(p), "release", "rollback"]) == 0
    capsys.readouterr()
    assert cli.main(["--config", str(p), "release", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["current"] == "1.0.0"
    assert "FAKE-cli-admin-pw-1" not in Path(tmp_path / "p.db").read_bytes().decode("latin1")


# ---------- self-check / drill ----------
def test_selfcheck_and_drill(tmp_path):
    c, conn, cfg = make(tmp_path)
    s = c.get("/api/selfcheck").json()
    ids = {x["id"]: x["status"] for x in s["checks"]}
    assert ids["panel.local"] == "ok" and ids["auth.admin"] == "ok" and "score" in s
    assert ids["backup.drill"] != "ok"
    d = c.post("/api/backup/drill", headers=H).json()
    assert d["ok"] and d["files"]
    ids = {x["id"]: x["status"] for x in c.get("/api/selfcheck").json()["checks"]}
    assert ids["backup.drill"] == "ok"



def test_invite_can_only_be_claimed_once(tmp_path):
    """An invite's claim and account creation must be atomic."""
    import pytest
    from galleryedge_ops import auth, db
    conn = db.connect(str(tmp_path / "auth-invites.db"))
    db.migrate(conn)
    auth.create_user(conn, "owner", "FAKE-Admin-Password-Strong-99", "admin")
    code = auth.create_invite(conn, "owner", "viewer")
    auth.register(conn, code, "personone", "FAKE-Password-Strong-88")
    with pytest.raises(auth.AuthError, match="invalid or expired"):
        auth.register(conn, code, "persontwo", "FAKE-Password-Strong-88")
    assert conn.execute("SELECT count(*) FROM users WHERE username IN ('personone','persontwo')").fetchone()[0] == 1


def test_failed_registration_keeps_invitation(tmp_path):
    import pytest
    from galleryedge_ops import auth, db
    conn = db.connect(str(tmp_path / "auth-rollback.db"))
    db.migrate(conn)
    auth.create_user(conn, "owner", "FAKE-Admin-Password-Strong-99", "admin")
    code = auth.create_invite(conn, "owner")
    with pytest.raises(auth.AuthError, match="username taken"):
        auth.register(conn, code, "owner", "FAKE-Password-Strong-88")
    auth.register(conn, code, "visitor", "FAKE-Password-Strong-88")
    assert auth.user_count(conn) == 2


def test_missing_user_password_check_uses_normal_cost_hash(monkeypatch, tmp_path):
    from galleryedge_ops import auth, db
    conn = db.connect(str(tmp_path / "auth-timing.db"))
    db.migrate(conn)
    observed = []
    original = auth.verify_password
    def spy(password, stored):
        observed.append(stored)
        return original(password, stored)
    monkeypatch.setattr(auth, "verify_password", spy)
    with pytest.raises(auth.AuthError):
        auth.login(conn, "absentuser", "FAKE-Password-Strong-88")
    assert observed == [auth._DUMMY_PASSWORD_HASH]
    assert auth._DUMMY_PASSWORD_HASH.split("$")[1] == str(2**14)
