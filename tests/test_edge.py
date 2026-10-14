import json
from datetime import datetime, timedelta, timezone

import pytest
from galleryedge_db import client as C
from galleryedge_edge import keys
from galleryedge_edge.app import App
from galleryedge_edge.demo import build, main as demo_main


def call(app, path, key=None, method="GET"):
    p, _, q = path.partition("?")
    env = {"PATH_INFO": p, "QUERY_STRING": q, "REQUEST_METHOD": method}
    if key:
        env["HTTP_X_API_KEY"] = key
    out = {}
    def sr(status, headers):
        out["status"] = int(status.split()[0]); out["headers"] = dict(headers)
    body = b"".join(app(env, sr)).decode()
    return out["status"], out["headers"], body


@pytest.fixture(params=["sqlite", "libsql"])
def site(request):
    url = ":memory:" if request.param == "sqlite" else request.getfixturevalue("libsql_url")
    if request.param == "libsql":
        request.getfixturevalue("libsql")  # clean tables
    db, d = build(url, 80, 11)
    return App(db), db


def test_index_cards_and_filters(site):
    app, db = site
    s, h, b = call(app, "/")
    assert s == 200 and b.count('class="card"') == 24 and "合成数据" in b
    assert "default-src 'self'" in h["Content-Security-Policy"]
    tag = db.execute("SELECT tag FROM work_tags LIMIT 1").rows[0][0]
    s, _, b = call(app, f"/?tag={tag}")
    n = db.execute("SELECT count(*) FROM work_tags WHERE tag=?", [tag]).rows[0][0]
    assert f"共 {n} 件" in b
    artist = db.execute("SELECT artist FROM works LIMIT 1").rows[0][0]
    res = app.search(artist=artist)
    assert res["total"] >= 1 and all(w["artist"] == artist for w in res["items"])


def test_fulltext_and_work_page_prompt_copy(site):
    app, db = site
    wid, title = db.execute("SELECT id,title FROM works ORDER BY id LIMIT 1").rows[0]
    db.execute("UPDATE works SET title='qqvzuniq harbor' WHERE id=?", [wid])
    assert [w["id"] for w in app.search(q="qqvzuniq")["items"]] == [wid]
    s, _, b = call(app, f"/work/{wid}")
    assert s == 200 and 'id="prompt"' in b and "navigator.clipboard.writeText" in b and "synthetic demo" in b
    s, h, b = call(app, f"/cover/{wid}.svg")
    assert s == 200 and h["Content-Type"] == "image/svg+xml" and b.startswith("<svg")
    assert call(app, "/work/nope")[0] == 404


def test_html_escaping(site):
    app, db = site
    db.execute("UPDATE works SET title=? WHERE id='demo-000000'", ['<script>alert(1)</script>'])
    _, _, b = call(app, "/work/demo-000000")
    assert "<script>alert(1)" not in b and "&lt;script&gt;" in b
    _, _, b = call(app, '/?q="><img src=x>')
    assert '"><img' not in b


def test_api_requires_key_and_works(site):
    app, db = site
    assert call(app, "/api/v1/search")[0] == 401
    assert call(app, "/api/v1/search", "ge_bad_key")[0] == 401
    k = keys.create_key(db)
    assert db.execute("SELECT count(*) FROM api_keys WHERE hash LIKE ?", [k]).rows[0][0] == 0
    s, h, b = call(app, "/api/v1/search?q=harbor", k)
    assert s == 200 and "items" in json.loads(b) and h["X-RateLimit-Remaining-Day"] == "999"
    s, _, b = call(app, "/api/v1/works/demo-000001", k)
    assert s == 200 and json.loads(b)["license"]
    assert call(app, "/api/v1/works/zzz", k)[0] == 404
    assert json.loads(call(app, "/api/v1/tags", k)[2])["items"]
    assert call(app, "/api/v1/search?page=x", k)[0] == 400
    keys.revoke(db, k.split("_")[1])
    assert call(app, "/api/v1/search", k)[0] == 403
    assert call(app, "/", method="POST")[0] == 405


def test_quota_minute_and_day():
    db, _ = build(":memory:", 5, 1)
    db.execute("INSERT INTO api_plans VALUES ('tiny', 2, 3)")
    k = keys.create_key(db, "tiny")
    t = datetime(2026, 1, 1, 10, 0, 5, tzinfo=timezone.utc)
    assert keys.check(db, k, t)[0] == 200 and keys.check(db, k, t)[0] == 200
    s, why, h = keys.check(db, k, t)
    assert s == 429 and "rate" in why and h["Retry-After"] == "55"
    assert keys.check(db, k, t + timedelta(minutes=1))[0] == 200
    s, why, _ = keys.check(db, k, t + timedelta(minutes=2))
    assert s == 429 and "daily" in why
    assert keys.check(db, k, t + timedelta(days=1))[0] == 200
    with pytest.raises(ValueError):
        keys.create_key(db, "nope")


def test_healthz_and_cli(tmp_path, capsys):
    url = "file:" + str(tmp_path / "d.db")
    assert demo_main(["--db", url, "seed", "-n", "30"]) == 0
    assert demo_main(["--db", url, "key"]) == 0
    k = capsys.readouterr().out.strip().splitlines()[-1]
    app = App(C.connect(url))
    assert json.loads(call(app, "/healthz")[2]) == {"ok": True, "fts": "fts5"}
    assert call(app, "/api/v1/search", k)[0] == 200
