import json
import os
import time
import uuid
from pathlib import Path

import pytest
from conftest import PW
from galleryedge_db import client as C
from galleryedge_db import dialect, schema
from galleryedge_ops import db as pdb
from galleryedge_ops.vault import Vault
from galleryedge_storage import StorageError, add_to_monthly_counter, make_backend
from test_storage import FakeS3


def test_factory_reads_credentials_from_vault_only(tmp_path):
    v = Vault(tmp_path / "v", PW, scrypt_n=2**10)
    v.set("r2-main", json.dumps({"access_key_id": "FAKE_AKID", "secret_access_key": "FAKE_SECRET_KEY_123"}))
    seen = {}
    def factory(**kw):
        seen.update(kw); return FakeS3()
    b = make_backend({"id": "r2", "type": "r2", "url": "https://acct.r2.invalid", "bucket": "bk", "credential_ref": "vault:r2-main"}, v, client_factory=factory)
    assert seen["aws_access_key_id"] == "FAKE_AKID" and seen["endpoint_url"] == "https://acct.r2.invalid"
    b.put("x", b"1"); assert b.get("x") == b"1"
    with pytest.raises(StorageError, match="vault locked"):
        make_backend({"id": "r2", "type": "r2", "bucket": "bk", "credential_ref": "vault:r2-main"}, None, client_factory=factory)
    with pytest.raises(StorageError, match="bucket"):
        make_backend({"id": "r2", "type": "r2"}, v, client_factory=factory)
    assert make_backend({"id": "l", "type": "local", "path": str(tmp_path / "l")}).usage() == (0, 0)


@pytest.mark.skipif(not os.environ.get("GE_TEST_S3_ENDPOINT"), reason="no S3-compatible server (set GE_TEST_S3_ENDPOINT)")
def test_s3_compatible_server_integration(tmp_path):
    boto3 = pytest.importorskip("boto3")
    ep = os.environ["GE_TEST_S3_ENDPOINT"]
    v = Vault(tmp_path / "v", PW, scrypt_n=2**10)
    v.set("minio", json.dumps({"access_key_id": os.environ["GE_TEST_S3_KEY"], "secret_access_key": os.environ["GE_TEST_S3_SECRET"]}))
    bucket = "ge-" + uuid.uuid4().hex[:8]
    b = make_backend({"id": "minio", "type": "s3", "url": ep, "region": "us-east-1", "bucket": bucket, "credential_ref": "vault:minio"}, v)
    b.c.create_bucket(Bucket=bucket)
    for i in range(5):
        b.put(f"p/{i}.bin", bytes([i]) * (i + 1))
    assert b.list("p/") == [f"p/{i}.bin" for i in range(5)]
    h = b.head("p/3.bin")
    assert h.size == 4 and len(h.sha256) == 64 and b.get("p/3.bin") == b"\x03" * 4
    assert b.usage() == (15, 5)
    from galleryedge_storage import LocalBackend, Router, Rule, migrate, plan_cold_migration
    import sqlite3
    from datetime import datetime, timedelta, timezone
    conn = sqlite3.connect(":memory:")
    hot = LocalBackend("hot", str(tmp_path / "hot"))
    r = Router(conn, [hot, b], [Rule("hot")])
    r.put("old/a.png", b"abc", now=datetime.now(timezone.utc) - timedelta(days=90))
    b.tier = "cold"
    moves, _ = plan_cold_migration(r, "minio", older_than_days=30)
    assert migrate(r, moves, dry_run=False)["moved"] == ["old/a.png"] and hot.head("old/a.png") is None and r.get("old/a.png") == b"abc"
    b.delete("p/0.bin"); assert b.head("p/0.bin") is None


def test_ingest_reports_rows_to_panel(tmp_path, libsql):
    from galleryedge_ingest import main as ingest_main
    from galleryedge_pack import write_pack
    from galleryedge_demo.generate import generate
    recs = []
    for w in generate(12, 5):
        recs.append({**w, "provenance": {"adapter": "t", "fetched_at": "now", "fetch_url": w["source_url"], "robots": "not-applicable", "content_sha256": "0" * 64}})
    write_pack(tmp_path / "a.tar", "t-00001", "t", recs)
    panel = "file:" + str(tmp_path / "panel.db")
    url = libsql.base  # real libSQL server reports rows_written
    assert ingest_main(["--db", url, "--panel-db", panel, str(tmp_path / "a.tar")]) == 0
    conn = pdb.connect(panel)
    v1 = conn.execute("SELECT value FROM usage_snapshots WHERE meter='turso.rows_written' ORDER BY ts DESC").fetchone()[0]
    assert v1 > 12  # works + tags + fts triggers
    time.sleep(1.1)
    ingest_main(["--db", url, "--panel-db", panel, str(tmp_path / "a.tar")])
    v2 = conn.execute("SELECT value FROM usage_snapshots WHERE meter='turso.rows_written' ORDER BY ts DESC").fetchone()[0]
    assert v2 > v1
    assert conn.execute("SELECT count(*) FROM events WHERE code='ingest.pack'").fetchone()[0] == 2


def test_monthly_counter_resets(tmp_path):
    from datetime import datetime, timezone
    import sqlite3
    conn = sqlite3.connect(":memory:")
    assert add_to_monthly_counter(conn, "m", 5, now=datetime(2026, 9, 30, 23, tzinfo=timezone.utc)) == 5
    assert add_to_monthly_counter(conn, "m", 3, now=datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc)) == 8
    assert add_to_monthly_counter(conn, "m", 2, now=datetime(2026, 10, 1, 0, 5, tzinfo=timezone.utc)) == 2


def test_scheduled_sampler(tmp_path):
    from test_panel_api import make
    c, conn, cfg = make(tmp_path)
    stop = c.app.state.start_sampler(0.001)  # ~60ms
    try:
        for _ in range(100):
            if conn.execute("SELECT count(*) FROM audit WHERE actor='scheduler'").fetchone()[0] >= 2:
                break
            time.sleep(0.05)
    finally:
        stop.set()
    assert conn.execute("SELECT count(*) FROM audit WHERE actor='scheduler'").fetchone()[0] >= 2
    assert conn.execute("SELECT count(*) FROM events WHERE code='sampler.started'").fetchone()[0] == 1
