"""Opt-in audit probes: assertions describe VULNERABLE behavior, not passing security.

Run from the repository root:
    python -m pytest -q docs/audits/2026-10-15-round3/reproduce.py

No cloud calls, live credentials or real user data. Files use pytest tmp_path.
Do not add this module to the normal regression suite unchanged after fixes.
"""
import copy
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from galleryedge_db import dialect, schema
from galleryedge_db.client import HranaClient, connect
from galleryedge_ops import auth, backup, config, db, totp
from galleryedge_pack import read_pack, validate_record, write_pack

ROOT = Path(__file__).resolve().parents[3]
PW = "FAKE-audit-password-7821"


def record(i):
    return {
        "id": f"audit-{i}", "title": f"Audit {i}", "source_url": "https://source.invalid/a",
        "license": "CC0", "rating": "general", "tags": [],
        "provenance": {"adapter": "audit", "fetched_at": "2026-10-15T00:00:00Z",
                       "fetch_url": "https://source.invalid/a", "robots": "allowed", "content_sha256": "0" * 64},
    }


@pytest.mark.parametrize("url", ["https://localhost.db.invalid", "https://db.invalid/localhost", "https://127.0.0.1.db.invalid"])
def test_https_is_downgraded_for_non_loopback_urls(url):
    # Constructor alone proves the rewrite; no packet leaves the machine.
    assert HranaClient(url, "FAKE-token").base == "http://" + url[8:]


def test_same_totp_code_creates_two_sessions_under_concurrency(tmp_path, monkeypatch):
    p = str(tmp_path / "panel.db")
    c = db.connect(p)
    db.migrate(c)
    auth.create_user(c, "owner", PW, "admin")
    sec = auth.totp_setup(c, "owner")["secret"]
    c.execute("UPDATE user_totp SET enabled=1")
    c.commit()
    # Freeze only the time input and coordinate the two genuine verifiers after
    # both have read last_step. Each worker uses its own real SQLite connection.
    step = totp.now_step()
    code = totp.code_at(sec, step)
    barrier = threading.Barrier(2)
    original = totp.verify

    def coordinated(secret, otp, **kw):
        ans = original(secret, otp, t=step * totp.STEP, **kw)
        barrier.wait(timeout=10)
        return ans

    monkeypatch.setattr(totp, "verify", coordinated)

    def attempt(_):
        worker = db.connect(p)
        try:
            return bool(auth.login(worker, "owner", PW, code, require_totp=True))
        finally:
            worker.close()

    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(attempt, range(2))) == [True, True]
    assert c.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2
    c.close()


def test_cf_uploader_does_not_bind_or_validate_the_guarded_script(tmp_path):
    from galleryedge_ops.cfrelease import wrangler_uploader
    cfg = tmp_path / "wrangler.toml"
    cfg.write_text('name = "unrelated-worker"\nmain = "worker.js"\n')
    captured = []

    def fake_run(args, **kw):
        captured.append(args)
        return type("Result", (), {"returncode": 0})()

    with patch("galleryedge_ops.cfrelease.subprocess.run", fake_run):
        wrangler_uploader("wrangler", str(cfg), {}, str(tmp_path))("audit-tag", "audit")
    assert len(captured) == 1
    assert "--name" not in captured[0]
    assert captured[0][captured[0].index("--config") + 1] == str(cfg)
    # No real wrangler/cloud operation is performed.


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode-bit probe")
def test_panel_db_and_backup_are_world_readable_under_standard_umask(tmp_path):
    previous = os.umask(0o022)
    try:
        path = tmp_path / "panel.db"
        c = db.connect(str(path))
        db.migrate(c)
        auth.create_user(c, "owner", PW, "admin")
        secret = auth.totp_setup(c, "owner")["secret"]
        c.close()
        arc = tmp_path / "backup.tar.gz"
        backup.create(arc, config_path=None, db_path=str(path), vault_path=None)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert stat.S_IMODE(arc.stat().st_mode) == 0o644
    assert secret.encode() in path.read_bytes()
    restored = tmp_path / "restored"
    backup.restore(arc, restored)
    assert secret.encode() in (restored / "panel.db").read_bytes()


def test_invalid_record_passes_pack_validation_then_partially_ingests(tmp_path):
    from galleryedge_ingest import ingest_pack
    rows = [record(i) for i in range(201)]
    rows[-1]["tags"] = [42]
    assert validate_record(rows[-1]) == []
    arc = tmp_path / "input.tar"
    write_pack(arc, "audit-pack", "audit", rows)
    assert len(read_pack(arc)[1]) == 201
    idx = connect(":memory:")
    schema.init(idx, dialect.Fts5Dialect())
    with pytest.raises(TypeError):
        ingest_pack(idx, arc)
    assert idx.execute("SELECT count(*) FROM works").rows[0][0] == 200


def test_fresh_crawl_state_silently_overwrites_existing_pack(tmp_path):
    from galleryedge_adapters import SyntheticAdapter, SyntheticFetcher
    from galleryedge_crawl import CrawlConfig, Crawler
    out = tmp_path / "packs"

    def crawl(seed):
        return Crawler(SyntheticAdapter(1, seed=seed),
                       CrawlConfig("audit@example.invalid", ["demo-source.invalid"], min_interval_s=0),
                       state_path=str(tmp_path / f"state-{seed}.db"), out_dir=str(out), fetcher=SyntheticFetcher()).run()

    a = crawl(42)
    p = Path(a["packs"][0])
    old = read_pack(p)[1]
    b = crawl(99)
    assert a["packs"] == b["packs"]
    assert old != read_pack(p)[1]
    assert len(list(out.glob("*.tar"))) == 1


@pytest.mark.parametrize("wid", ["source:123", "source.123"])
def test_accepted_record_ids_have_unreachable_public_routes(wid):
    from galleryedge_edge.app import App
    idx = connect(":memory:")
    schema.init(idx, dialect.Fts5Dialect())
    r = record(0)
    r["id"] = wid
    assert validate_record(r) == []
    schema.ingest(idx, [r])
    app = App(idx)
    assert app.work(wid) is not None
    statuses = []
    list(app({"PATH_INFO": "/work/" + wid}, lambda s, h: statuses.append(s)))
    assert statuses == ["404 Not Found"]


def make_panel(tmp_path):
    from fastapi.testclient import TestClient
    from galleryedge_panel.server import create_app
    cfg = copy.deepcopy(json.loads((ROOT / "examples/config.example.json").read_text()))
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    cfg = config.load(p)
    for b in cfg["backends"]:
        Path(b["path"]).mkdir(parents=True, exist_ok=True)
    c = db.connect(":memory:")
    db.migrate(c)
    auth.create_user(c, "owner", PW, "admin")
    cfg["backends"][0]["read_only"] = True
    # A formerly writable cleanup root can remain configured when its backend
    # is changed to read_only; the configuration validator accepts this overlap.
    cfg["cleanup"]["roots"] = [cfg["backends"][0]["path"]]
    app = create_app(cfg, c)
    app.state.job_runner.stop()
    client = TestClient(app)
    h = {"X-GE-Panel": "1"}
    assert client.post("/api/auth/login", headers=h, json={"username": "owner", "password": PW}).status_code == 200
    return client, cfg, h


def test_cleanup_deletes_from_a_read_only_backend(tmp_path):
    client, cfg, h = make_panel(tmp_path)
    p = Path(cfg["backends"][0]["path"]) / "pack"
    p.mkdir()
    (p / ".verified_remote").touch()
    media = p / "image.png"
    media.write_bytes(b"FAKE-media")
    plan = client.get("/api/cleanup/plan").json()
    assert str(media) in [r["path"] for r in plan["delete"]]
    ans = client.post("/api/cleanup/apply", headers=h, json={"confirm": True, "token": plan["token"]})
    assert ans.status_code == 200
    assert not media.exists()


def test_incremental_crawl_never_retries_previously_failed_urls(tmp_path):
    from galleryedge_adapters import SyntheticAdapter, SyntheticFetcher
    from galleryedge_crawl import CrawlConfig, Crawler, Response

    class FailOnce(SyntheticFetcher):
        healthy = False
        def get(self, url):
            if url.endswith("/robots.txt"):
                return super().get(url)
            return super().get(url) if self.healthy else Response(url, 500, {}, b"error")

    fetcher = FailOnce()
    crawler = Crawler(SyntheticAdapter(1), CrawlConfig("audit@example.invalid", ["demo-source.invalid"], min_interval_s=0),
                      state_path=str(tmp_path / "state.db"), out_dir=str(tmp_path / "packs"), fetcher=fetcher)
    first = crawler.run()
    assert first["records"] == 0
    fetcher.healthy = True
    crawler.run_incremental()
    assert crawler.st.db.execute("SELECT count(*) FROM frontier").fetchone()[0] == 0
    assert crawler.st.db.execute("SELECT count(*) FROM seen WHERE status=0").fetchone()[0] == 1
    assert crawler.stats["records"] == 0


def test_failed_fetches_do_not_consume_the_page_budget(tmp_path):
    from galleryedge_crawl import CrawlConfig, Crawler, Link, Response

    class Adapter:
        name = "budget-audit"
        def start_urls(self):
            return ["https://source.invalid/list"]
        def parse(self, response):
            return [Link(f"/bad/{i}") for i in range(10)]

    class Fetcher:
        calls = 0
        def get(self, url):
            if url.endswith("/robots.txt"):
                return Response(url, 200, {}, b"User-agent: *\nAllow: /\n")
            self.calls += 1
            return Response(url, 200 if url.endswith("/list") else 500, {}, b"body")

    fetcher = Fetcher()
    crawler = Crawler(Adapter(), CrawlConfig("audit@example.invalid", ["source.invalid"], max_pages=2, min_interval_s=0),
                      state_path=str(tmp_path / "state.db"), out_dir=str(tmp_path / "packs"), fetcher=fetcher)
    result = crawler.run()
    assert result["pages_total"] == 1
    assert fetcher.calls == 11


@pytest.mark.skipif(not hasattr(schema, "USAGE_GUARDS"), reason="specific to PR #28 quota patch")
def test_negative_control_pr28_namespaced_quota_is_enforced():
    from galleryedge_db.client import DBError, PrefixedClient
    from galleryedge_edge import keys
    inner = connect(":memory:")
    a, b = PrefixedClient(inner, "one_"), PrefixedClient(inner, "two_")
    keys.init_usage(a)
    keys.init_usage(b)
    key = keys.create_key(b)
    b.execute("UPDATE api_plans SET rpd=1,rpm=1")
    prefix = key.split("_")[1]
    # Negative control: the suspected name collision was RULED OUT in PR #28.
    with pytest.raises(DBError, match="ge_daily_quota"):
        b.execute("INSERT INTO usage_daily VALUES (?,?,?)", [prefix, "2026-10-15", 2])
    triggers = inner.execute("SELECT name,tbl_name FROM sqlite_master WHERE type='trigger'").rows
    assert len(triggers) == 8
    assert {t.split("_")[0] for _, t in triggers} == {"one", "two"}
