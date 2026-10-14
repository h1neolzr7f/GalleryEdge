"""WebDAV backend against a real local WsgiDAV server + promote-on-read (cold -> hot)."""
import socket
import sqlite3
import threading

import pytest

wsgidav = pytest.importorskip("wsgidav")
from cheroot import wsgi  # noqa: E402
from wsgidav.wsgidav_app import WsgiDAVApp  # noqa: E402

from galleryedge_storage import LocalBackend, MemoryBackend, Router, Rule, StorageError, WebDAVBackend, make_backend  # noqa: E402


@pytest.fixture
def dav(tmp_path):
    root = tmp_path / "dav"; root.mkdir()
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    app = WsgiDAVApp({"provider_mapping": {"/": str(root)}, "verbose": 0, "logging": {"enable": False},
                      "http_authenticator": {"domain_controller": None, "accept_basic": True, "accept_digest": False, "default_to_digest": False},
                      "simple_dc": {"user_mapping": {"*": {"fakeuser": {"password": "FAKE-dav-pass"}}}}})
    srv = wsgi.Server(("127.0.0.1", port), app)
    t = threading.Thread(target=srv.safe_start, daemon=True); t.start()
    import time
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close(); break
        except OSError:
            time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/", root
    srv.stop()


def test_crud_list_usage(dav):
    url, root = dav
    b = WebDAVBackend("drive", url, username="fakeuser", password="FAKE-dav-pass")
    b.put("packs/2026/a.tar", b"hello"); b.put("packs/b.bin", b"xy")
    assert b.get("packs/2026/a.tar") == b"hello" and (root / "packs/2026/a.tar").read_bytes() == b"hello"
    assert b.head("packs/2026/a.tar").size == 5 and b.head("nope") is None
    assert b.list() == ["packs/2026/a.tar", "packs/b.bin"] and b.list("packs/2026") == ["packs/2026/a.tar"]
    assert b.usage() == (7, 2)
    b.delete("packs/b.bin"); assert b.list() == ["packs/2026/a.tar"]
    with pytest.raises(StorageError):
        b.get("packs/b.bin")
    with pytest.raises(StorageError):
        b.put("../escape", b"x")


def test_wrong_password_and_read_only(dav):
    url, _ = dav
    with pytest.raises(StorageError):
        WebDAVBackend("d", url, username="fakeuser", password="wrong").put("a", b"x")
    with pytest.raises(StorageError):
        WebDAVBackend("d", url, read_only=True).put("a", b"x")


def test_make_backend_from_vault(dav):
    import json
    url, _ = dav
    class V:
        def resolve(self, ref): assert ref == "vault:dav"; return json.dumps({"username": "fakeuser", "password": "FAKE-dav-pass"})
    b = make_backend({"id": "drive", "type": "webdav", "url": url, "credential_ref": "vault:dav"}, V())
    b.put("k", b"v"); assert b.get("k") == b"v" and b.tier == "cold"


def test_router_writes_cold_and_promotes_on_read(dav, tmp_path):
    url, root = dav
    hot = LocalBackend("hot", str(tmp_path / "hot"), "hot")
    cold = WebDAVBackend("drive", url, "cold", username="fakeuser", password="FAKE-dav-pass")
    conn = sqlite3.connect(":memory:")
    events = []
    r = Router(conn, [hot, cold], [Rule("hot", priority=1), Rule("drive", priority=2)], is_up=lambda b: b != "hot",
               emit=lambda *a: events.append(a), promote_on_read=True)
    assert r.put("img/1.png", b"PNGDATA") == "drive"  # hot down -> cold fallback
    r.is_up = lambda b: True
    assert r.get("img/1.png") == b"PNGDATA"
    assert conn.execute("SELECT backend_id, tier FROM objects WHERE key='img/1.png'").fetchone() == ("hot", "hot")
    assert (tmp_path / "hot/img/1.png").read_bytes() == b"PNGDATA" and not (root / "img/1.png").exists()
    assert any(e[1] == "storage.promoted" for e in events)
    assert r.get("img/1.png") == b"PNGDATA"  # now served from hot


def test_promote_failure_does_not_break_read(tmp_path):
    conn = sqlite3.connect(":memory:")
    cold = MemoryBackend("cold", "cold"); hot = MemoryBackend("hot", "hot", fail=True)
    r = Router(conn, [hot, cold], [Rule("hot", priority=1), Rule("cold", priority=2)], promote_on_read=True)
    r.b["cold"].put("k", b"v")
    import hashlib
    conn.execute("INSERT INTO objects VALUES ('k','cold','cold',1,?,'stored','2026-01-01','2026-01-01')", (hashlib.sha256(b"v").hexdigest(),))
    assert r.get("k") == b"v"
    assert conn.execute("SELECT backend_id FROM objects").fetchone() == ("cold",)


def test_promote_skips_large_and_ledgers(tmp_path):
    conn = sqlite3.connect(":memory:")
    cold, hot = MemoryBackend("cold", "cold"), MemoryBackend("hot", "hot")
    r = Router(conn, [hot, cold], [Rule("hot", priority=1), Rule("cold", priority=2)], is_up=lambda b: b != "hot", promote_on_read=True, promote_max_bytes=3)
    r.put("big", b"12345"); r.is_up = lambda b: True
    r.get("big")
    assert conn.execute("SELECT backend_id FROM objects WHERE key='big'").fetchone() == ("cold",)
