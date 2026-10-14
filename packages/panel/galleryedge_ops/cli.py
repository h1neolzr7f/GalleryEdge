"""ge-ops command line."""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from pathlib import Path

from . import alerts as alerts_mod
from . import backup, cleanup, config, db, events, health, masking
from .vault import Vault, VaultError


def _ctx(a):
    cfg = config.load(a.config)
    state = Path(cfg["panel"]["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    vpath = cfg.get("vault", {}).get("path") or str(state / "credentials.vault")
    vault = None
    try:
        vault = Vault(vpath)
    except VaultError:
        if a.cmd == "vault":
            raise
    token = vault.resolve(cfg["database"].get("auth_token_ref")) if vault else None
    conn = db.connect(cfg["database"]["primary"], token)
    db.migrate(conn)
    return cfg, conn, vault, vpath


def _alerter(cfg, conn, vault):
    al = cfg["alerts"]
    url = vault.resolve(al.get("webhook_url_ref")) if vault and al.get("webhook_url_ref") else None
    sec = vault.resolve(al.get("webhook_hmac_ref")) if vault and al.get("webhook_hmac_ref") else None
    return alerts_mod.Alerter(conn, webhook_url=url, hmac_secret=sec, dedupe_minutes=al.get("dedupe_minutes", 30))


def out(obj):
    print(masking.redact(json.dumps(obj, ensure_ascii=False, indent=1, default=str)))


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    masking.install()
    ap = argparse.ArgumentParser(prog="ge-ops")
    ap.add_argument("--config", default="config.json")
    sp = ap.add_subparsers(dest="cmd", required=True)
    c = sp.add_parser("config"); c.add_argument("action", choices=["validate"]); c.add_argument("file")
    sp.add_parser("health")
    al = sp.add_parser("alerts"); al.add_argument("--all", action="store_true"); al.add_argument("--ack", type=int)
    v = sp.add_parser("vault"); v.add_argument("action", choices=["set", "get", "list", "rotate", "expiring", "delete", "rekey"])
    v.add_argument("name", nargs="?"); v.add_argument("--expires-days", type=int); v.add_argument("--reveal", action="store_true")
    b = sp.add_parser("backup"); b.add_argument("action", choices=["create", "verify", "restore"]); b.add_argument("archive"); b.add_argument("--to")
    cl = sp.add_parser("cleanup"); cl.add_argument("action", choices=["plan", "apply"]); cl.add_argument("--yes", action="store_true")
    au = sp.add_parser("audit"); au.add_argument("--limit", type=int, default=50)
    ev = sp.add_parser("events"); ev.add_argument("--limit", type=int, default=50)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "config":
            try:
                config.validate(json.loads(Path(a.file).read_text("utf-8")))
            except config.ConfigError as e:
                out({"valid": False, "problems": e.problems}); return 2
            out({"valid": True}); return 0
        if a.cmd == "backup" and a.action in ("verify", "restore"):
            if a.action == "verify":
                out(backup.verify(a.archive)); return 0
            if not a.to:
                ap.error("--to is required for restore")
            out({"restored": backup.restore(a.archive, a.to), "to": a.to}); return 0
        cfg, conn, vault, vpath = _ctx(a)
        if a.cmd == "health":
            res = health.run_checks(conn, cfg["backends"], _alerter(cfg, conn, vault),
                                    fail_threshold=cfg["alerts"].get("fail_threshold", 1), vault=vault)
            if vault:
                A = _alerter(cfg, conn, vault)
                for e in vault.expiring(cfg.get("vault", {}).get("remind_days", 14)):
                    when = "已过期 / expired" if e["expired"] else f"{e['days_left']} 天后到期 / expires in {e['days_left']}d"
                    A.fire(f"credential.expiring.{e['name']}", "warn", f"凭据 {e['name']} {when}，请轮换 / rotate it")
            out([r.__dict__ for r in res]); return 0 if all(r.ok for r in res) else 1
        if a.cmd == "alerts":
            A = _alerter(cfg, conn, vault)
            if a.ack:
                A.ack(a.ack); events.audit(conn, "cli", "alert.ack", {"id": a.ack})
            out(A.list(a.all)); return 0
        if a.cmd == "vault":
            if a.action == "list":
                out(vault.list()); return 0
            if a.action == "expiring":
                out(vault.expiring(cfg.get("vault", {}).get("remind_days", 14))); return 0
            if a.action == "rekey":
                vault.rekey(getpass.getpass("new passphrase: ")); events.audit(conn, "cli", "vault.rekey"); out({"ok": True}); return 0
            if not a.name:
                ap.error("name required")
            if a.action in ("set", "rotate"):
                val = getpass.getpass(f"value for {a.name}: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
                if a.action == "set":
                    vault.set(a.name, val, expires_days=a.expires_days)
                else:
                    vault.rotate(a.name, val, expires_days=a.expires_days)
                events.audit(conn, "cli", f"vault.{a.action}", {"name": a.name, "expires_days": a.expires_days})
                out({"ok": True, "name": a.name}); return 0
            if a.action == "delete":
                vault.delete(a.name); events.audit(conn, "cli", "vault.delete", {"name": a.name}); out({"ok": True}); return 0
            if a.action == "get":
                val = vault.get(a.name)
                events.audit(conn, "cli", "vault.get", {"name": a.name, "reveal": a.reveal})
                if a.reveal:
                    sys.stdout.write(val + "\n")
                else:
                    out({"name": a.name, "mask": masking.mask_value(val)})
                return 0
        if a.cmd == "backup":
            db_path = cfg["database"]["primary"]
            db_path = db_path[5:] if db_path.startswith("file:") else (None if "://" in db_path else db_path)
            man = backup.create(a.archive, config_path=a.config, db_path=db_path, vault_path=vpath)
            events.audit(conn, "cli", "backup.create", {"archive": a.archive, "files": list(man["files"])})
            out(man); return 0
        if a.cmd == "cleanup":
            roots = cfg["cleanup"]["roots"]
            dry = not (a.action == "apply" and a.yes)
            events.audit(conn, "cli", "cleanup.start", {"dry_run": dry, "roots": roots})
            pl = cleanup.apply(roots, dry_run=dry, extra_protected=cfg["cleanup"].get("extra_protected"))
            events.audit(conn, "cli", "cleanup.done", {"dry_run": dry, "deleted": len(pl.delete), "bytes": pl.bytes})
            out({"dry_run": dry, "delete": [str(p) for p in pl.delete], "bytes": pl.bytes,
                 "protected": [[str(p), why] for p, why in pl.protected]}); return 0
        if a.cmd in ("audit", "events"):
            out(events.recent(conn, a.cmd, a.limit)); return 0
    except (VaultError, backup.BackupError, config.ConfigError) as e:
        print(masking.redact(f"error: {e}"), file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
