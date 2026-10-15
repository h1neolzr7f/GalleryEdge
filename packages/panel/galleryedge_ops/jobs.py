"""Panel background jobs: crawl / ingest / backup / keys / CF release, with progress in the events log.

Jobs are rows in ``jobs``; a daemon thread runs them one at a time. Secrets go only into 0600 files
or the vault — job payloads never store raw tokens, and event messages are redacted.
"""
from __future__ import annotations

import json
import os
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import events

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|error|cancelled
  progress REAL NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}',
  result TEXT,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""

KINDS = ("crawl", "ingest", "keys_create", "keys_revoke", "keys_init",
         "cf_deploy", "cf_rollback", "cf_list", "db_dump", "db_restore", "storage_snapshot", "storage_migrate")


def migrate(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def enqueue(conn, kind: str, actor: str, payload: dict) -> int:
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind}")
    migrate(conn)
    cur = conn.execute(
        "INSERT INTO jobs(kind,status,payload,actor,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        (kind, "queued", json.dumps(payload, ensure_ascii=False), actor, _now(), _now()),
    )
    conn.commit()
    jid = cur.lastrowid
    events.emit(conn, "jobs", "info", f"job.queued.{kind}", f"任务 #{jid} 已排队", {"id": jid, "kind": kind})
    events.audit(conn, actor, f"job.enqueue.{kind}", {"id": jid})
    return int(jid)


def list_jobs(conn, limit: int = 40) -> list[dict]:
    migrate(conn)
    cols = ("id", "kind", "status", "progress", "message", "payload", "result", "actor", "created_at", "updated_at")
    return [dict(zip(cols, r)) for r in conn.execute(
        "SELECT id,kind,status,progress,message,payload,result,actor,created_at,updated_at FROM jobs ORDER BY id DESC LIMIT ?",
        (limit,),
    )]


def get_job(conn, jid: int) -> dict | None:
    migrate(conn)
    r = conn.execute(
        "SELECT id,kind,status,progress,message,payload,result,actor,created_at,updated_at FROM jobs WHERE id=?",
        (jid,),
    ).fetchone()
    if not r:
        return None
    cols = ("id", "kind", "status", "progress", "message", "payload", "result", "actor", "created_at", "updated_at")
    return dict(zip(cols, r))


def _set(conn, jid: int, **kw) -> None:
    cols, vals = [], []
    for k, v in kw.items():
        cols.append(f"{k}=?"); vals.append(v)
    cols.append("updated_at=?"); vals.append(_now()); vals.append(jid)
    conn.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE id=?", vals)
    conn.commit()


class JobRunner:
    def __init__(self, conn, cfg: dict, vault=None):
        self.conn, self.cfg, self.vault = conn, cfg, vault
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # Process death interrupts running work. Do not leave a permanent
        # "running" status or auto-replay potentially non-idempotent operations.
        migrate(self.conn)
        self.conn.execute(
            "UPDATE jobs SET status='error', message=?, updated_at=? WHERE status='running'",
            ("任务被上次进程退出中断；请检查结果后手动重试", _now()),
        )
        self.conn.commit()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ge-jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(0.5):
            with self._lock:
                migrate(self.conn)
                row = self.conn.execute(
                    "SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1"
                ).fetchone()
                if not row:
                    continue
                jid = row[0]
                job = get_job(self.conn, jid)
                _set(self.conn, jid, status="running", progress=0.05, message="开始…")
            try:
                result = self._run(job)
                _set(self.conn, jid, status="done", progress=1.0, message="完成", result=json.dumps(result, ensure_ascii=False, default=str))
                events.emit(self.conn, "jobs", "info", f"job.done.{job['kind']}", f"任务 #{jid} 完成", {"id": jid, "kind": job["kind"]})
            except Exception as e:
                _set(self.conn, jid, status="error", message=str(e)[:400], result=json.dumps({"error": str(e)[:400]}))
                events.emit(self.conn, "jobs", "error", f"job.error.{job['kind']}", f"任务 #{jid} 失败: {e}", {"id": jid})

    def _run(self, job: dict) -> dict:
        kind, payload = job["kind"], json.loads(job["payload"] or "{}")
        jid = job["id"]
        state = Path(self.cfg["panel"]["state_dir"])
        (state / "jobs").mkdir(parents=True, exist_ok=True)

        if kind == "crawl":
            return self._crawl(jid, payload, state)
        if kind == "ingest":
            return self._ingest(jid, payload)
        if kind == "keys_init":
            return self._keys_init(payload)
        if kind == "keys_create":
            return self._keys_create(payload, state)
        if kind == "keys_revoke":
            return self._keys_revoke(payload)
        if kind == "cf_list":
            return self._cf(payload, "list")
        if kind == "cf_deploy":
            return self._cf(payload, "deploy")
        if kind == "cf_rollback":
            return self._cf(payload, "rollback")
        if kind == "db_dump":
            return self._db_dump(payload, state)
        if kind == "db_restore":
            return self._db_restore(payload)
        if kind == "storage_snapshot":
            return {"ok": True, "note": "snapshot via /api/costs/snapshot"}
        if kind == "storage_migrate":
            return {"ok": True, "note": "migrate via /api/storage/migrate"}
        raise ValueError(kind)

    def _progress(self, jid, p, msg):
        try:
            _set(self.conn, jid, progress=p, message=str(msg)[:300])
        except Exception:
            pass  # progress is best-effort; never fail the job for a UI update

    def _crawl(self, jid, payload, state):
        from galleryedge_adapters import ADAPTERS
        from galleryedge_crawl import CrawlConfig, Crawler

        adapter_name = payload.get("adapter") or "synthetic"
        if adapter_name not in ADAPTERS:
            raise ValueError(f"unknown adapter {adapter_name}")
        contact = (payload.get("contact") or "").strip()
        out = state / "packs"
        st = state / "crawl" / f"{adapter_name}.db"
        st.parent.mkdir(parents=True, exist_ok=True)
        out.mkdir(parents=True, exist_ok=True)
        max_pages = min(int(payload.get("max_pages") or 100), 5000)
        min_interval = float(payload.get("min_interval") or 1.0)

        if adapter_name == "synthetic":
            from galleryedge_adapters import SyntheticFetcher
            adapter, fetcher, hosts = ADAPTERS["synthetic"](int(payload.get("n") or 50)), SyntheticFetcher(), ["demo-source.invalid"]
            contact = contact or "https://github.com/h1neolzr7f/GalleryEdge"
        elif adapter_name == "wikimedia-commons":
            from galleryedge_adapters.commons import SFW_CATEGORIES
            cats = [c.strip() for c in (payload.get("categories") or "").split(",") if c.strip()] or list(SFW_CATEGORIES)[:3]
            bad = [c for c in cats if c not in SFW_CATEGORIES]
            if bad:
                raise ValueError(f"分类不在白名单: {bad}")
            if min_interval < 1.0:
                min_interval = 1.0
            adapter = ADAPTERS["wikimedia-commons"](cats, max_category_pages=int(payload.get("category_pages") or 1),
                                                    subcat_depth=int(payload.get("subcat_depth") or 0))
            fetcher, hosts = None, ["commons.wikimedia.org"]
        elif adapter_name == "openverse":
            if min_interval < 3.0:
                min_interval = 3.0
            qs = [q.strip() for q in (payload.get("queries") or "").split(",") if q.strip()] or None
            adapter = ADAPTERS["openverse"](qs) if qs else ADAPTERS["openverse"]()
            fetcher, hosts = None, ["api.openverse.org"]
        else:
            raise ValueError(f"adapter {adapter_name} not available from the panel")

        if not contact or ("@" not in contact and not contact.startswith("http")):
            raise ValueError("请填写联系邮箱或网址（会放进 User-Agent）")

        cfg = CrawlConfig(contact=contact, allowed_hosts=hosts, min_interval_s=min_interval, max_pages=max_pages, pack_size=int(payload.get("pack_size") or 200))

        def on_event(level, code, msg, payload=None):
            events.emit(self.conn, "crawler", level, code, msg, payload)
            if code == "crawler.heartbeat":
                pages = (payload or {}).get("pages") or 0
                self._progress(jid, min(0.95, pages / max(max_pages, 1)), msg)

        self._progress(jid, 0.1, f"开始抓取 {adapter_name}…")
        stats = Crawler(adapter, cfg, state_path=str(st), out_dir=str(out), fetcher=fetcher, on_event=on_event,
                        progress=lambda m: self._progress(jid, 0.5, m)).run()
        return {"records": stats.get("records"), "packs": stats.get("packs"), "skip_reasons": stats.get("skip_reasons"), "pages": stats.get("pages_total")}

    def _ingest(self, jid, payload):
        from galleryedge_db.client import connect
        from galleryedge_db import dialect, schema
        from galleryedge_ingest import ingest_pack

        packs = payload.get("packs") or []
        if not packs:
            # default: all tars under state/packs
            packs = sorted(str(p) for p in (Path(self.cfg["panel"]["state_dir"]) / "packs").glob("*.tar"))
        if not packs:
            raise ValueError("没有可入库的包；请先抓取")
        idx = self.cfg["database"].get("index")
        if not idx:
            # local demo DB
            idx = "file:" + str(Path(self.cfg["panel"]["state_dir"]) / "index.db")
        tok = None
        tf = self.cfg["database"].get("index_token_file")
        if tf and Path(tf).exists():
            raw = Path(tf).read_text().strip()
            tok = (json.loads(raw).get("rw_token") or json.loads(raw).get("ro_token")) if raw.startswith("{") else raw
        elif self.vault and self.cfg["database"].get("index_token_ref"):
            tok = self.vault.resolve(self.cfg["database"]["index_token_ref"])
        db = connect(idx, tok, prefix=self.cfg["database"].get("index_prefix", ""))
        schema.init(db, dialect.detect(db))
        total = 0
        for i, p in enumerate(packs):
            self._progress(jid, (i + 0.5) / len(packs), f"入库 {Path(p).name}")
            st = {}
            ingest_pack(db, p, st)
            total += st.get("records") or 0
        return {"packs": len(packs), "records": total, "db": idx.split("@")[-1] if "@" in idx else idx}

    def _usage_db(self, payload):
        from galleryedge_db.client import connect
        from galleryedge_edge import keys
        url = payload.get("usage_db") or self.cfg.get("database", {}).get("usage")
        tf = payload.get("token_file") or self.cfg.get("database", {}).get("usage_token_file")
        if not url:
            # local fallback
            url = "file:" + str(Path(self.cfg["panel"]["state_dir"]) / "usage.db")
        tok = None
        if tf and Path(tf).exists():
            raw = Path(tf).read_text().strip()
            d = json.loads(raw) if raw.startswith("{") else {}
            tok = d.get("usage_rw_token") or d.get("rw_token") or (raw if not d else None)
        return connect(url, tok), keys

    def _keys_init(self, payload):
        db, keys = self._usage_db(payload)
        keys.init_usage(db)
        return {"ok": True}

    def _keys_create(self, payload, state):
        db, keys = self._usage_db(payload)
        keys.init_usage(db)
        out = Path(payload.get("out") or (state / "api-keys" / f"key-{secrets_token()}.txt"))
        out.parent.mkdir(parents=True, exist_ok=True)
        key = keys.create_key(db, payload.get("plan") or "demo", payload.get("label") or "")
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key + "\n")
        return {"prefix": key.split("_")[1], "out": str(out), "note": "密钥已写入文件（0600），面板不会显示全文"}

    def _keys_revoke(self, payload):
        db, keys = self._usage_db(payload)
        prefix = (payload.get("prefix") or "").strip()
        if not prefix:
            raise ValueError("需要 prefix")
        keys.revoke(db, prefix)
        return {"revoked": prefix}

    def _cf(self, payload, action):
        from .cfrelease import CloudflareTarget, CFError, wrangler_uploader
        script = payload.get("script") or "galleryedge-staging"
        cred = payload.get("cred_file")
        if not cred or not Path(cred).exists():
            raise ValueError("请填写 Cloudflare 凭据文件路径（JSON，含 api_token / account_id）")
        c = json.loads(Path(cred).read_text())
        if payload.get("cred_key"):
            c = c[payload["cred_key"]]
        up = None
        if action == "deploy":
            wc = payload.get("wrangler_config")
            if not wc:
                raise ValueError("deploy 需要 wrangler_config 路径")
            env = {**os.environ, "CLOUDFLARE_API_TOKEN": c["api_token"], "CLOUDFLARE_ACCOUNT_ID": c["account_id"], "WRANGLER_SEND_METRICS": "false"}
            up = wrangler_uploader(payload.get("wrangler") or "wrangler", wc, env, str(Path(wc).parent))
        t = CloudflareTarget(c["account_id"], script, c["api_token"], uploader=up,
                             log_path=Path(self.cfg["panel"]["state_dir"]) / "cf-release.jsonl")
        if action == "list":
            return {"current": t.current_version(), "versions": t.versions()[:15]}
        if action == "deploy":
            return t.deploy(payload.get("tag") or "panel", payload.get("message") or "")
        return t.rollback(payload.get("to"))

    def _db_dump(self, payload, state):
        from galleryedge_db.backup import dump
        from galleryedge_db.client import connect
        idx = self.cfg["database"].get("index")
        if not idx:
            raise ValueError("未配置内容库 database.index")
        tf = self.cfg["database"].get("index_token_file")
        tok = None
        if tf and Path(tf).exists():
            raw = Path(tf).read_text().strip()
            tok = (json.loads(raw).get("ro_token") or json.loads(raw).get("rw_token")) if raw.startswith("{") else raw
        out = Path(payload.get("out") or (state / "backups" / f"index-{_now().replace(':', '')}.jsonl.gz"))
        out.parent.mkdir(parents=True, exist_ok=True)
        man = dump(connect(idx, tok, prefix=self.cfg["database"].get("index_prefix", "")), out)
        return {"out": str(out), "tables": man["tables"]}

    def _db_restore(self, payload):
        from galleryedge_db.backup import restore
        from galleryedge_db.client import connect
        src = payload.get("src")
        target = payload.get("target_db")
        if not src or not target:
            raise ValueError("需要 src（备份文件）和 target_db（空库 URL）")
        tf = payload.get("token_file")
        tok = None
        if tf and Path(tf).exists():
            raw = Path(tf).read_text().strip()
            tok = (json.loads(raw).get("rw_token") if raw.startswith("{") else raw)
        return restore(connect(target, tok, prefix=payload.get("table_prefix") or ""), src)


def secrets_token() -> str:
    import secrets as S
    return S.token_hex(4)
