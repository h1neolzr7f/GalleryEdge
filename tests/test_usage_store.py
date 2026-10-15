import json
from datetime import datetime, timezone

from galleryedge_db import dialect, schema
from galleryedge_db.client import connect
from galleryedge_edge import keys
from galleryedge_edge.app import App


def test_keys_in_separate_db(tmp_path):
    content = connect(f"file:{tmp_path}/c.db"); schema.init(content, dialect.detect(content))
    schema.ingest(content, [{"id": "w1", "title": "FAKE", "source_url": "https://x.invalid/1", "license": "CC0", "rating": "general"}])
    usage = connect(f"file:{tmp_path}/u.db"); keys.init_usage(usage)
    k = keys.create_key(usage, label="t")
    app = App(content, keys_db=usage)
    st, _, _ = keys.check(usage, k)
    assert st == 200
    assert content.execute("SELECT count(*) FROM usage_daily").rows == [(0,)]
    assert usage.execute("SELECT sum(count) FROM usage_daily").rows == [(1,)]
    assert app.keys_db is usage


def test_prune(tmp_path):
    u = connect(f"file:{tmp_path}/u.db"); keys.init_usage(u)
    u.execute("INSERT INTO usage_minute VALUES ('p','2020-01-01T00:00',3)"); u.execute("INSERT INTO usage_daily VALUES ('p','2020-01-01',3)")
    u.execute("INSERT INTO usage_minute VALUES ('p','2099-01-01T00:00',3)")
    keys.prune(u, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert u.execute("SELECT count(*) FROM usage_minute").rows == [(1,)] and u.execute("SELECT count(*) FROM usage_daily").rows == [(0,)]


def test_cli_never_prints_key(tmp_path, capsys):
    db = f"file:{tmp_path}/u.db"
    assert keys.main(["--db", db, "init"]) == 0
    out = tmp_path / "k.txt"
    assert keys.main(["--db", db, "create", "--out", str(out)]) == 0
    k = out.read_text().strip()
    assert k.startswith("ge_") and k not in capsys.readouterr().out
    assert oct(out.stat().st_mode & 0o777) == "0o600"



def test_atomic_quota_triggers_reject_extra_writes(tmp_path):
    import pytest
    from galleryedge_db.client import DBError
    usage = connect(f"file:{tmp_path}/quota.db")
    keys.init_usage(usage)
    usage.execute("INSERT INTO api_plans VALUES ('one',1,1)")
    k = keys.create_key(usage, "one")
    prefix = k.split("_")[1]
    t = datetime(2026, 10, 15, 1, 2, tzinfo=timezone.utc)
    assert keys.check(usage, k, t)[0] == 200
    assert keys.check(usage, k, t)[0] == 429
    with pytest.raises(DBError, match="ge_daily_quota"):
        usage.batch([
            ("INSERT INTO usage_daily VALUES (?,?,1) ON CONFLICT(prefix,day) DO UPDATE SET count=count+1",
             (prefix, "2026-10-15")),
            ("INSERT INTO usage_minute VALUES (?,?,1) ON CONFLICT(prefix,minute) DO UPDATE SET count=count+1",
             (prefix, "2026-10-15T01:02")),
        ])
    assert usage.execute("SELECT count FROM usage_daily WHERE prefix=?", [prefix]).rows == [(1,)]
    assert usage.execute("SELECT count FROM usage_minute WHERE prefix=?", [prefix]).rows == [(1,)]
