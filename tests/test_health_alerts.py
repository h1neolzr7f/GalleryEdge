import io
import json
import urllib.error

from galleryedge_ops import health
from galleryedge_ops.alerts import Alerter, verify_signature


class Resp(io.BytesIO):
    def __init__(self, status=200):
        super().__init__(b"")
        self.status = status
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_kill_local_backend_triggers_alert_then_recovery(conn, tmp_path):
    d = tmp_path / "hot"
    d.mkdir()
    b = [{"id": "hot", "type": "local", "path": str(d)}]
    A = Alerter(conn)
    assert health.run_checks(conn, b, A)[0].ok
    assert A.list() == []
    d.rmdir()  # "kill" the backend
    assert not health.run_checks(conn, b, A)[0].ok
    keys = [r[2] for r in A.list()]
    assert keys == ["backend.down.hot"]
    health.run_checks(conn, b, A)  # still down -> no duplicate alert
    assert len(A.list()) == 1
    d.mkdir()
    health.run_checks(conn, b, A)
    assert [r[2] for r in A.list()][0] == "backend.recovered.hot"
    assert conn.execute("SELECT status FROM backend_status").fetchone()[0] == "up"


def test_fail_threshold(conn, tmp_path):
    b = [{"id": "x", "type": "local", "path": str(tmp_path / "nope")}]
    A = Alerter(conn)
    health.run_checks(conn, b, A, fail_threshold=3)
    health.run_checks(conn, b, A, fail_threshold=3)
    assert A.list() == []
    health.run_checks(conn, b, A, fail_threshold=3)
    assert len(A.list()) == 1


def test_quota_warning(conn, tmp_path):
    (tmp_path / "f").write_bytes(b"x" * 900)
    A = Alerter(conn)
    health.run_checks(conn, [{"id": "q", "type": "local", "path": str(tmp_path), "quota_bytes": 1000}], A)
    assert [r[2] for r in A.list()] == ["quota.q"]


def test_http_and_object_store_probe():
    ok = health.probe_http({"id": "w", "type": "http", "url": "http://x.invalid"}, opener=lambda r, timeout: Resp(200))
    assert ok.ok
    def forbid(r, timeout):
        raise urllib.error.HTTPError(r.full_url, 403, "f", {}, None)
    assert health.probe_http({"id": "s", "type": "r2", "url": "http://x.invalid"}, opener=forbid).ok
    assert not health.probe_http({"id": "w", "type": "http", "url": "http://x.invalid"}, opener=forbid).ok
    def boom(r, timeout):
        raise OSError("down")
    assert not health.probe_http({"id": "w", "type": "http", "url": "http://x.invalid"}, opener=boom).ok


def test_libsql_probe_local_file(tmp_path):
    assert health.probe_libsql({"id": "db", "type": "libsql", "url": str(tmp_path / "p.db")}).ok


def test_webhook_signed_payload_and_dedupe(conn):
    sent = []
    def opener(req, timeout):
        sent.append(req)
        return Resp(204)
    A = Alerter(conn, webhook_url="http://hook.invalid", hmac_secret="FAKE_hmac_key", opener=opener)
    assert A.fire("k", "warn", "token=FAKE_should_be_masked here")
    assert not A.fire("k", "warn", "again")  # deduped
    assert len(sent) == 1
    body = sent[0].data
    assert b"FAKE_should_be_masked" not in body
    assert verify_signature("FAKE_hmac_key", body, sent[0].headers["X-galleryedge-signature"])
    assert A.list()[0][6] == "panel,webhook"


def test_webhook_failure_still_records_panel_alert(conn):
    def boom(req, timeout):
        raise OSError("no route")
    A = Alerter(conn, webhook_url="http://hook.invalid", opener=boom)
    assert A.fire("k", "error", "m")
    assert A.list()[0][6] == "panel"
    assert conn.execute("SELECT code FROM events WHERE code='webhook.failed'").fetchone()
