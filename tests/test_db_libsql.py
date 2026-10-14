import json

import pytest
from galleryedge_db import client as C
from galleryedge_db import dialect, schema
from galleryedge_demo.generate import generate
from galleryedge_ops import health


def both(request, libsql):
    return libsql if request.param == "libsql" else C.SqliteClient(":memory:")


@pytest.fixture(params=["sqlite", "libsql"])
def db(request):
    if request.param == "libsql":
        return request.getfixturevalue("libsql")
    return C.SqliteClient(":memory:")


def test_roundtrip_types(db):
    db.execute("CREATE TABLE t(i INTEGER, f REAL, s TEXT, b BLOB, n)")
    db.execute("INSERT INTO t VALUES (?,?,?,?,?)", (7, 1.5, "中文 ok", b"\x00\x01\xff", None))
    r = db.execute("SELECT * FROM t")
    assert r.columns == ["i", "f", "s", "b", "n"] and r.rows == [(7, 1.5, "中文 ok", b"\x00\x01\xff", None)]


def test_batch_is_transactional(db):
    db.execute("CREATE TABLE t(a INTEGER PRIMARY KEY)")
    with pytest.raises(C.DBError):
        db.batch([("INSERT INTO t VALUES (1)", ()), ("INSERT INTO t VALUES (1)", ())])
    assert db.execute("SELECT count(*) FROM t").rows == [(0,)]


def test_schema_ingest_fts(db):
    d = dialect.detect(db)
    assert d.name == "fts5"
    schema.init(db, d)
    works = generate(120, seed=3)
    assert schema.ingest(db, works, batch_size=50) == 120
    schema.ingest(db, works[:10])  # idempotent upsert
    assert db.execute("SELECT count(*) FROM works").rows == [(120,)]
    w = works[5]
    sql, args = d.search_sql(w["title"].split()[1])
    ids = [r[0] for r in db.execute(sql, args).rows]
    assert w["id"] in ids
    db.execute("UPDATE works SET title='zzuniqueword' WHERE id=?", (w["id"],))
    sql, args = d.search_sql("zzunique")
    assert [r[0] for r in db.execute(sql, args).rows] == [w["id"]]


def test_libsql_url_scheme_and_health_probe(libsql_url):
    c = C.connect(libsql_url.replace("http://", "libsql://"))
    assert c.execute("SELECT 1").rows == [(1,)]
    assert "token" not in repr(C.HranaClient(libsql_url, "FAKE_token_value"))
    assert health.probe_libsql({"id": "db", "type": "libsql", "url": libsql_url}).ok
    assert not health.probe_libsql({"id": "db", "type": "libsql", "url": "http://127.0.0.1:1"}).ok


def test_bearer_token_sent():
    seen = {}
    class R:
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def read(s): return json.dumps({"results": [{"type": "ok", "response": {"type": "execute", "result": {"cols": [], "rows": []}}}, {"type": "ok", "response": {"type": "close"}}]}).encode()
    def op(req, timeout):
        seen["auth"] = req.headers["Authorization"]; seen["url"] = req.full_url; return R()
    C.HranaClient("libsql://db-x.turso.invalid", "FAKE_tok", opener=op).execute("SELECT 1")
    assert seen == {"auth": "Bearer FAKE_tok", "url": "https://db-x.turso.invalid/v2/pipeline"}


def test_turso_native_dialect_sql_only():
    d = dialect.TursoFtsDialect()
    assert "USING fts" in d.ddl()
    sql, args = d.search_sql('rain" OR 1=1 --')
    assert '"' not in args[0] and "?" in sql


def test_fts5_query_sanitizes():
    assert dialect.fts5_query('a" OR b) NEAR(') == '"a"* "OR"* "b"* "NEAR"*'


def test_split_sql_triggers():
    parts = [p for p in C.split_sql(dialect.Fts5Dialect().ddl()) if p.strip()]
    assert len(parts) == 4 and all("END" in p for p in parts[1:])
