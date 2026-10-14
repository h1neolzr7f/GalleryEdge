"""Phase 7: aitag bridge (read-only), table prefix, CJK search. Synthetic fixtures only."""
import hashlib
import json
import sqlite3

import pytest
from galleryedge_aitag import convert, inspect_manifest, iter_records, main
from galleryedge_db import dialect, schema
from galleryedge_db.client import connect, prefix_sql
from galleryedge_db.text import cjk_space, fts5_query
from galleryedge_pack import PackError, read_pack

ROWS = [(1, "原神胡桃", "masterpiece, 1girl", "artist:fakeartist", '[{"name":"原神"},{"name":"胡桃"}]', "images/2022-10-09/1_1.webp"),
        (2, "夜の街", "city, night", "by someone", '[{"name":"夜景"}]', "images/2022-10-09/2_1.webp"),
        (3, "", "landscape", "", "not json", "images/x/3_1.webp")]


def fake_import(tmp_path):
    d = tmp_path / "pack-work" / "aitag-nai-9999"; d.mkdir(parents=True)
    p = d / "import.sqlite"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE works(id INTEGER PRIMARY KEY, title TEXT, prompt TEXT, user_id INTEGER, image_count INTEGER, cover_r2_key TEXT,"
              " created_at TEXT, rating TEXT, artist_names TEXT, tags_json TEXT, cover_sha256 TEXT)")
    for i, t, pr, a, tj, k in ROWS:
        c.execute("INSERT INTO works VALUES (?,?,?,?,?,?,?,?,?,?,?)", (i, t, pr, 7, 2, k, "2022-10-09T00:00:00", "unknown", a, tj, None))
    c.commit(); c.close()
    return p


def test_inspect_manifest(tmp_path):
    m = tmp_path / "aitag-nai-9999.MANIFEST.json"
    m.write_text(json.dumps({"format": "aitag-nai-site-pack-v1", "name": "aitag-nai-9999", "created_at": "x", "work_count": 3, "image_count": 6,
                             "tar": "a.tar", "tar_bytes": 1, "sha256": "0" * 64}))
    r = inspect_manifest(m)
    assert r["ok"] and r["site_pack_v1_gaps"]
    m.write_text(json.dumps({"format": "other"}))
    assert not inspect_manifest(m)["ok"]
    assert main(["inspect", str(m)]) == 1


def test_records_and_readonly(tmp_path):
    p = fake_import(tmp_path)
    before = hashlib.sha256(p.read_bytes()).hexdigest(); files_before = sorted(x.name for x in p.parent.iterdir())
    recs = list(iter_records(p, "aitag-nai-9999", fetched_at="2026-10-14T00:00:00+00:00"))
    assert [r["id"] for r in recs] == ["pixiv-1", "pixiv-2", "pixiv-3"]
    r = recs[0]
    assert r["source_url"] == "https://www.pixiv.net/artworks/1" and r["artist"] == "fakeartist" and r["tags"] == ["原神", "胡桃"]
    assert r["license"] and r["provenance"]["robots"] == "not-applicable"
    assert recs[1]["artist"] == "someone" and recs[2]["title"] == "pixiv 3" and recs[2]["tags"] == []
    out = tmp_path / "out" / "ge.tar"
    res = convert(p, out, "aitag-nai-9999", limit=2)
    assert res["records"] == 2
    man, got, media = read_pack(out)
    assert man["pack_id"] == "ge-aitag-nai-9999" and len(got) == 2 and media == {}
    assert hashlib.sha256(p.read_bytes()).hexdigest() == before
    assert sorted(x.name for x in p.parent.iterdir()) == files_before  # no -wal/-shm created
    with pytest.raises(PackError):
        convert(p, out, "aitag-nai-9999")  # never overwrite
    with pytest.raises(PackError):
        convert(p, p.parent / "x.tar", "aitag-nai-9999")  # never write into the source dir


def test_prefix_sql():
    assert prefix_sql("SELECT * FROM works w JOIN work_tags t ON 1 WHERE works_fts MATCH ?", "ge_") == \
        "SELECT * FROM ge_works w JOIN ge_work_tags t ON 1 WHERE ge_works_fts MATCH ?"
    assert prefix_sql("SELECT workshop FROM images", "ge_") == "SELECT workshop FROM images"
    with pytest.raises(ValueError):
        prefix_sql("x", "bad;--")


def test_cjk_query():
    assert cjk_space("原神胡桃 girl") == "原 神 胡 桃 girl"
    assert fts5_query("胡桃 girl") == '"胡 桃" "girl"*'


def test_prefixed_ingest_leaves_existing_tables_alone(tmp_path):
    dbp = tmp_path / "shared.db"
    c = sqlite3.connect(dbp); c.execute("CREATE TABLE works(id INTEGER PRIMARY KEY, title TEXT)"); c.execute("INSERT INTO works VALUES (1,'prod')"); c.commit(); c.close()
    db = connect(f"file:{dbp}", prefix="ge_")
    schema.init(db, dialect.detect(db))
    out = tmp_path / "o" / "p.tar"; convert(fake_import(tmp_path), out, "aitag-nai-9999")
    _, recs, _ = read_pack(out)
    schema.ingest(db, [{**r, "pack_id": "ge-aitag-nai-9999"} for r in recs])
    raw = connect(f"file:{dbp}")
    assert raw.execute("SELECT * FROM works").rows == [(1, "prod")]
    assert raw.execute("SELECT count(*) FROM ge_works").rows == [(3,)]
    d = dialect.DIALECTS["fts5"]
    for q, want in (("胡桃", ["pixiv-1"]), ("原神", ["pixiv-1"]), ("夜", ["pixiv-2"]), ("街", ["pixiv-2"]), ("landsc", ["pixiv-3"]), ("桃胡", [])):
        s, a = d.search_sql(q)
        assert [r[0] for r in db.execute(s, a).rows] == want, q


def test_read_only_backend_is_never_written(tmp_path):
    from galleryedge_ops.health import probe_local
    from galleryedge_storage import LocalBackend, StorageError, make_backend
    d = tmp_path / "real"; d.mkdir(); (d / "done.txt").write_text("ledger\n")
    before = sorted(p.name for p in d.iterdir())
    r = probe_local({"id": "obs", "type": "local", "path": str(d), "read_only": True})
    assert r.ok and r.used_bytes == 7
    assert sorted(p.name for p in d.iterdir()) == before
    b = make_backend({"id": "obs", "type": "local", "path": str(d), "read_only": True})
    with pytest.raises(StorageError):
        b.put("x", b"1")
    with pytest.raises(StorageError):
        b.delete("done.txt")
    assert (d / "done.txt").exists()
    LocalBackend("m", str(tmp_path / "missing"), read_only=True)
    assert not (tmp_path / "missing").exists()


def test_panel_observes_read_only_backend(tmp_path):
    from test_panel_api import H, make
    d = tmp_path / "real"; d.mkdir(); (d / "packed_works.txt").write_text("1\n")
    c, conn, cfg = make(tmp_path, takeover=False)
    cfg["backends"].append({"id": "aitag-real", "type": "local", "path": str(d), "read_only": True})
    from fastapi.testclient import TestClient
    from galleryedge_panel.server import create_app
    from galleryedge_ops import auth
    app = create_app(cfg, conn); cl = TestClient(app)
    cl.post("/api/auth/login", headers=H, json={"username": "admin", "password": auth and "FAKE-owner-pass-9876"})
    res = {r["id"]: r for r in cl.post("/api/health/run", headers=H).json()}
    assert res["aitag-real"]["ok"]
    snap = cl.post("/api/costs/snapshot", headers=H, json={}).json()
    assert any(m["meter"] == "bytes:aitag-real" and m["value"] == 2 for m in snap["meters"])
    assert sorted(p.name for p in d.iterdir()) == ["packed_works.txt"]


def test_fts_readable_and_rebuild(tmp_path):
    db = connect(f"file:{tmp_path}/r.db")
    d = dialect.detect(db); schema.init(db, d)
    schema.ingest(db, [{"id": "a1", "title": "FAKE 湖", "source_url": "https://x.invalid/a", "license": "CC0", "rating": "general", "tags": ["Blue sky"]}])
    assert db.execute("SELECT count(*), max(tags_text) FROM works_fts").rows == [(1, "Blue sky")]  # content columns resolve
    assert schema.rebuild_fts(db, d) == 1
    s, a = d.search_sql("湖")
    assert [r[0] for r in db.execute(s, a).rows] == ["a1"]
    db.execute("DELETE FROM works WHERE id='a1'")
    assert db.execute(s, a).rows == []
