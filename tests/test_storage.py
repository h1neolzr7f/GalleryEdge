import io
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from galleryedge_storage import (LocalBackend, MemoryBackend, Move, Router, Rule, S3Backend, StorageError, forecast,
                                 migrate, monthly_cost, plan_cold_migration, snapshot)

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


class FakeS3:
    """In-memory boto3-style client (MinIO/R2 stand-in). No network."""
    def __init__(self):
        self.o = {}
    def put_object(self, Bucket, Key, Body, Metadata=None):
        self.o[(Bucket, Key)] = (bytes(Body), dict(Metadata or {}))
    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.o:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.o[(Bucket, Key)][0])}
    def head_object(self, Bucket, Key):
        b, m = self.o[(Bucket, Key)]
        return {"ContentLength": len(b), "Metadata": m}
    def delete_object(self, Bucket, Key):
        self.o.pop((Bucket, Key), None)
    def list_objects_v2(self, Bucket, Prefix="", ContinuationToken=None):
        keys = sorted(k for b, k in self.o if b == Bucket and k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + 2]
        more = start + 2 < len(keys)
        return {"Contents": [{"Key": k} for k in page], "IsTruncated": more, **({"NextContinuationToken": str(start + 2)} if more else {})}


@pytest.fixture
def conn():
    return sqlite3.connect(":memory:")


@pytest.mark.parametrize("make", [lambda t: LocalBackend("l", str(t / "l")), lambda t: MemoryBackend("m"),
                                  lambda t: S3Backend("s3", FakeS3(), "bkt", prefix="ge/")])
def test_backend_contract(make, tmp_path):
    b = make(tmp_path)
    b.put("a/b.bin", b"hello"); b.put("a/c.bin", b"xy"); b.put("z.bin", b"1")
    assert b.get("a/b.bin") == b"hello" and b.head("a/b.bin").size == 5 and b.head("nope") is None
    assert b.list("a/") == ["a/b.bin", "a/c.bin"] and b.usage() == (8, 3)
    b.delete("a/b.bin"); assert b.head("a/b.bin") is None
    for bad in ["../x", "/abs", "a/../../x"]:
        with pytest.raises(StorageError):
            b.put(bad, b"x")


def test_routing_rules_and_failover(conn):
    hot, hot2, cold = MemoryBackend("hot"), MemoryBackend("hot2"), MemoryBackend("cold", "cold")
    r = Router(conn, [hot, hot2, cold], [Rule("hot", "img/", max_size=10, priority=1), Rule("hot2", "", priority=5), Rule("cold", "", priority=9)])
    assert r.put("img/a", b"small") == "hot"
    assert r.put("img/big", b"x" * 50) == "hot2"          # size rule
    hot2.fail = True
    events = []
    r.emit = lambda *a: events.append(a)
    assert r.put("other", b"y") == "cold"                 # failover
    assert events and events[0][1] == "storage.failover"
    r.is_up = lambda bid: bid != "cold"
    with pytest.raises(StorageError):
        r.put("other2", b"z")
    assert r.get("img/a", now=NOW) == b"small"
    hot.objs["img/a"] = b"tampered"
    with pytest.raises(StorageError, match="integrity"):
        r.get("img/a")


def setup_tiers(conn, tmp_path):
    hot, cold = LocalBackend("hot", str(tmp_path / "h")), LocalBackend("cold", str(tmp_path / "c"), "cold")
    r = Router(conn, [hot, cold], [Rule("hot")])
    old = NOW - timedelta(days=40)
    for k in ["p1/a.png", "p1/b.png", "p1/done.txt", "staging/p9/x.png", "p2/new.png"]:
        r.put(k, k.encode(), now=old if k != "p2/new.png" else NOW)
    conn.execute("UPDATE objects SET state='uploading' WHERE key='p1/b.png'")
    return r, hot, cold


def test_cold_migration_plan_protects(conn, tmp_path):
    r, hot, cold = setup_tiers(conn, tmp_path)
    moves, skipped = plan_cold_migration(r, "cold", older_than_days=30, now=NOW)
    assert [m.key for m in moves] == ["p1/a.png"]
    assert dict(skipped) == {"p1/b.png": "state=uploading", "p1/done.txt": "ledger", "staging/p9/x.png": "in-progress"}
    assert migrate(r, moves)["dry_run"] is True and hot.head("p1/a.png")
    res = migrate(r, moves, dry_run=False)
    assert res["moved"] == ["p1/a.png"] and hot.head("p1/a.png") is None and cold.get("p1/a.png") == b"p1/a.png"
    assert conn.execute("SELECT backend_id, tier FROM objects WHERE key='p1/a.png'").fetchone() == ("cold", "cold")
    assert r.get("p1/a.png") == b"p1/a.png"
    for k in ["p1/b.png", "p1/done.txt", "staging/p9/x.png", "p2/new.png"]:
        assert hot.head(k), k


def test_migration_never_deletes_unverified_source(conn, tmp_path):
    hot, cold = MemoryBackend("hot"), MemoryBackend("cold", "cold")
    r = Router(conn, [hot, cold], [Rule("hot")])
    r.put("k1", b"data1"); r.put("k2", b"data2")
    class Lossy(MemoryBackend):
        def put(self, key, data):  # silently corrupts
            super().put(key, data[:-1])
    lossy = Lossy("cold", "cold"); r.b["cold"] = lossy
    res = migrate(r, [Move("k1", "hot", "cold", 5)], dry_run=False)
    assert res["moved"] == [] and "verify" in res["failed"][0][1] and hot.objs["k1"] == b"data1"
    r.b["cold"] = cold
    hot.objs["k2"] = b"rotten"  # source corrupted -> do not propagate
    res = migrate(r, [Move("k2", "hot", "cold", 5)], dry_run=False)
    assert res["moved"] == [] and "source integrity" in res["failed"][0][1] and cold.head("k2") is None


def test_forecast_and_cost(conn):
    Router(conn, [], [])
    for d in range(10):
        snapshot(conn, {"bytes:hot": 1000 + d * 100}, now=NOW - timedelta(days=9 - d))
    f = forecast(conn, "bytes:hot", 3000, now=NOW)
    assert f["value"] == 1900 and round(f["per_day"]) == 100 and round(f["days_to_quota"]) == 11 and f["ratio"] == pytest.approx(1900 / 3000)
    assert forecast(conn, "none", 10, now=NOW)["value"] is None
    assert monthly_cost(25e9, 2e6, 0.015, 0.36, free_gb=10) == pytest.approx(15 * 0.015 + 2 * 0.36)



def test_local_backend_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_bytes(b"private")
    root = tmp_path / "local"
    b = LocalBackend("local", str(root))
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "object").symlink_to(target)
    for key in ("link/secret.txt", "object"):
        with pytest.raises(StorageError, match="escapes storage root"):
            b.get(key)
        with pytest.raises(StorageError, match="escapes storage root"):
            b.put(key, b"overwrite")
        with pytest.raises(StorageError, match="escapes storage root"):
            b.delete(key)
        with pytest.raises(StorageError, match="escapes storage root"):
            b.head(key)
    assert target.read_bytes() == b"private"
    assert b.list() == [] and b.usage() == (0, 0)
    b.put("safe/sub.txt", b"safe")
    assert b.get("safe/sub.txt") == b"safe"
