import pytest

from galleryedge_ops.cfrelease import CFError, CloudflareTarget


class FakeCF:
    """In-memory Cloudflare Versions/Deployments API."""
    def __init__(self):
        self.versions = [{"id": "v-1", "number": 1, "annotations": {"workers/tag": "1.0.0"}, "metadata": {"created_on": "2026-10-14T01:00:00Z"}}]
        self.deps = [{"created_on": "2026-10-14T01:00:00Z", "versions": [{"version_id": "v-1", "percentage": 100}]}]
        self.calls = []
    def __call__(self, method, url, token, body=None):
        assert token == "FAKE-cf-token"
        self.calls.append((method, url, body))
        assert "/workers/scripts/galleryedge-staging/" in url
        if url.endswith("/versions"):
            return {"result": {"items": self.versions}}
        if url.endswith("/deployments") and method == "GET":
            return {"result": {"deployments": self.deps}}
        if url.endswith("/deployments") and method == "POST":
            self.deps.append({"created_on": f"2026-10-14T0{len(self.deps) + 1}:00:00Z", "versions": body["versions"]})
            return {"result": {}}
        raise AssertionError(url)
    def upload(self, tag, msg):
        n = len(self.versions) + 1
        self.versions.append({"id": f"v-{n}", "number": n, "annotations": {"workers/tag": tag}})


def T(fake, **kw):
    return CloudflareTarget("acct", "galleryedge-staging", "FAKE-cf-token", http=fake, uploader=fake.upload, **kw)


def test_refuses_non_galleryedge_scripts():
    for bad in ("nai", "nai-gallery", "", "galleryedge"):
        with pytest.raises(CFError):
            CloudflareTarget("acct", bad, "t", http=FakeCF())


def test_deploy_then_rollback_and_log(tmp_path):
    f = FakeCF(); t = T(f, log_path=tmp_path / "cf.jsonl")
    ev = t.deploy("1.1.0")
    assert ev == {**ev, "from": "v-1", "to": "v-2", "action": "deploy"} and t.current_version() == "v-2"
    rb = t.rollback()
    assert rb["from"] == "v-2" and rb["to"] == "v-1" and t.current_version() == "v-1"
    posts = [b for m, u, b in f.calls if m == "POST"]
    assert posts[-1]["versions"] == [{"version_id": "v-1", "percentage": 100}]
    assert len((tmp_path / "cf.jsonl").read_text().splitlines()) == 2
    with pytest.raises(CFError):
        t.rollback()  # already oldest
    with pytest.raises(CFError):
        t.rollback(to="v-404")


def test_token_never_in_output(tmp_path, capsys, monkeypatch):
    import json
    from galleryedge_ops import cfrelease
    f = FakeCF()
    monkeypatch.setattr(cfrelease, "_http", f)
    cred = tmp_path / "cf.json"; cred.write_text(json.dumps({"index": {"api_token": "FAKE-cf-token", "account_id": "acct"}}))
    assert cfrelease.main(["--script", "galleryedge-staging", "--cred-file", str(cred), "--cred-key", "index", "list"]) == 0
    out = capsys.readouterr().out
    assert "FAKE-cf-token" not in out and json.loads(out)["current"] == "v-1"


def test_db_dump_restore_roundtrip(tmp_path):
    from galleryedge_db import backup, dialect, schema
    from galleryedge_db.client import connect
    src = connect(f"file:{tmp_path}/a.db"); schema.init(src, dialect.detect(src))
    schema.ingest(src, [{"id": f"w{i}", "title": f"FAKE 胡桃 {i}", "source_url": f"https://x.invalid/{i}", "license": "CC0",
                         "rating": "general", "tags": ["flower", "花"]} for i in range(450)])
    man = backup.dump(src, tmp_path / "d.jsonl.gz")
    assert man["tables"]["works"]["rows"] == 450
    dst = connect(f"file:{tmp_path}/b.db")
    res = backup.restore(dst, tmp_path / "d.jsonl.gz")
    assert res["restored"]["works"] == 450
    assert dst.execute("SELECT count(*) FROM works_fts WHERE works_fts MATCH ?", ['"胡 桃"']).rows[0][0] == 450
    with pytest.raises(ValueError, match="not empty"):
        backup.restore(dst, tmp_path / "d.jsonl.gz")


def test_restore_detects_tampering(tmp_path):
    import gzip
    from galleryedge_db import backup, dialect, schema
    from galleryedge_db.client import connect
    src = connect(f"file:{tmp_path}/a.db"); schema.init(src, dialect.detect(src))
    schema.ingest(src, [{"id": "w1", "title": "FAKE", "source_url": "https://x.invalid/1", "license": "CC0", "rating": "general"}])
    p = tmp_path / "d.jsonl.gz"; backup.dump(src, p)
    data = gzip.open(p, "rt").read().replace("FAKE", "EVIL")
    with gzip.open(p, "wt") as f:
        f.write(data)
    with pytest.raises(ValueError, match="checksum"):
        backup.restore(connect(f"file:{tmp_path}/c.db"), p)
