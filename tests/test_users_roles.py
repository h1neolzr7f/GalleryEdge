import pytest
from galleryedge_ops import auth, db

from test_panel_api import ADMIN_PW, H, make


def mem():
    c = db.connect(":memory:"); db.migrate(c); return c


def test_set_role_rules():
    c = mem(); auth.create_user(c, "root", ADMIN_PW, "admin"); auth.create_user(c, "bob", "FAKE-viewer-pass-77", "viewer")
    t = auth.login(c, "bob", "FAKE-viewer-pass-77")
    auth.set_role(c, "root", "bob", "admin")
    assert {u["username"]: u["role"] for u in auth.list_users(c)}["bob"] == "admin"
    assert auth.session_user(c, t) is None  # sessions dropped on role change
    with pytest.raises(auth.AuthError, match="own role"):
        auth.set_role(c, "root", "root", "viewer")
    auth.set_role(c, "bob", "root", "viewer")  # another admin may demote root while bob stays admin
    with pytest.raises(auth.AuthError, match="last admin"):
        auth.set_role(c, "root", "bob", "viewer")
    with pytest.raises(auth.AuthError):
        auth.set_role(c, "bob", "bob", "superuser")


def test_revoke_invite():
    c = mem(); auth.create_user(c, "root", ADMIN_PW, "admin")
    code = auth.create_invite(c, "root", "viewer")
    iid = auth.list_invites(c)[0]["id"]
    auth.revoke_invite(c, "root", iid)
    assert auth.list_invites(c)[0]["used_by"] == "revoked:root"
    with pytest.raises(auth.AuthError):
        auth.register(c, code, "eve", "FAKE-eve-password-1")
    with pytest.raises(auth.AuthError, match="already"):
        auth.revoke_invite(c, "root", iid)
    with pytest.raises(auth.AuthError):
        auth.revoke_invite(c, "root", "x")


def test_api_role_and_revoke(tmp_path):
    c, conn, cfg = make(tmp_path)
    auth.create_user(conn, "bob", "FAKE-viewer-pass-77", "viewer")
    assert c.post("/api/users/bob/role", headers=H, json={"role": "admin"}).status_code == 200
    assert c.post("/api/users/admin/role", headers=H, json={"role": "viewer"}).status_code == 400  # own role
    c.post("/api/invites", headers=H, json={"role": "viewer"})
    iid = c.get("/api/users").json()["invites"][0]["id"]
    assert c.post(f"/api/invites/{iid}/revoke", headers=H).status_code == 200
    assert c.get("/api/users").json()["invites"][0]["used_by"].startswith("revoked:")
