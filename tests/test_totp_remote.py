"""TOTP 2FA + remote mode guard + read-only staging content view."""
import time

import pytest
from fastapi.testclient import TestClient
from galleryedge_ops import auth, db, totp
from galleryedge_panel.server import create_app

from test_panel_api import ADMIN_PW, H, make


def test_rfc6238_vector():
    # RFC 6238 appendix B, SHA-1 secret "12345678901234567890" at T=59 => 94287082 (8 digits) -> last 6 = 287082
    import base64
    sec = base64.b32encode(b"12345678901234567890").decode()
    assert totp.code_at(sec, 59 // 30) == "287082"
    assert totp.verify(sec, "287082", t=59) == 1 and totp.verify(sec, "287082", t=59, last_step=1) is None  # no replay
    assert totp.verify(sec, "abc", t=59) is None


def enrol(c, conn, user="admin"):
    s = c.post("/api/auth/totp/setup", headers=H).json()["secret"]
    assert c.post("/api/auth/totp/enable", headers=H, json={"code": totp.code_at(s, totp.now_step())}).status_code == 200
    return s


def test_enrol_then_login_requires_code(tmp_path):
    c, conn, cfg = make(tmp_path)
    s = enrol(c, conn)
    assert c.get("/api/auth/totp").json() == {"enabled": True}
    r = c.post("/api/auth/login", headers=H, json={"username": "admin", "password": ADMIN_PW})
    assert r.status_code == 401 and "totp_required" in r.text
    # the enable code's step is consumed: replay of the same step is refused, next step works
    conn.execute("UPDATE user_totp SET last_step=last_step-1"); conn.commit()
    r = c.post("/api/auth/login", headers=H, json={"username": "admin", "password": ADMIN_PW, "otp": totp.code_at(s, totp.now_step())})
    assert r.status_code == 200
    r = c.post("/api/auth/login", headers=H, json={"username": "admin", "password": ADMIN_PW, "otp": "000000"})
    assert r.status_code == 401


def remote_client(tmp_path, **kw):
    c, conn, cfg = make(tmp_path, allow_remote=True, **kw)
    rc = TestClient(c.app, client=("203.0.113.7", 50000))
    return c, rc, conn


def test_remote_login_refused_without_2fa(tmp_path):
    c, rc, conn = remote_client(tmp_path)
    r = rc.post("/api/auth/login", headers=H, json={"username": "admin", "password": ADMIN_PW})
    assert r.status_code == 401 and "2FA" in r.text
    assert rc.get("/api/status").status_code == 401  # nothing without a session


def test_remote_login_with_2fa_and_no_remote_enrolment(tmp_path):
    c, rc, conn = remote_client(tmp_path)
    s = enrol(c, conn)  # local enrolment
    conn.execute("UPDATE user_totp SET last_step=last_step-1"); conn.commit()
    r = rc.post("/api/auth/login", headers=H, json={"username": "admin", "password": ADMIN_PW, "otp": totp.code_at(s, totp.now_step())})
    assert r.status_code == 200
    assert rc.get("/api/status").status_code == 200
    # enrolment endpoints refuse remote callers (prevents a stolen password from enrolling a new device)
    assert rc.post("/api/auth/totp/setup", headers=H).status_code in (400, 403)


def test_content_view_read_only(tmp_path):
    from galleryedge_db import dialect, schema
    from galleryedge_db.client import connect
    idx = connect(f"file:{tmp_path}/idx.db"); schema.init(idx, dialect.detect(idx))
    schema.ingest(idx, [{"id": "w1", "title": "FAKE 胡桃 flower", "source_url": "https://x.invalid/1", "license": "CC0", "rating": "general", "author": "FAKE"}])
    c, conn, cfg = make(tmp_path)
    app = create_app(cfg, conn, index_db=idx)
    t = TestClient(app); t.post("/api/auth/login", headers=H, json={"username": "admin", "password": ADMIN_PW})
    j = t.get("/api/content", params={"q": "胡桃"}).json()
    assert j["read_only"] and j["works"] == 1 and j["search"][0][0] == "w1"
    assert t.get("/api/content", params={"q": "flower"}).json()["search"][0][0] == "w1"
