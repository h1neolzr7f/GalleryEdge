"""ge-panel: run the local ops panel; manage admin accounts and local releases."""
from __future__ import annotations

import argparse
import getpass
import json
import sys

from galleryedge_ops import auth, config, db
from galleryedge_ops.vault import Vault, VaultError

from .server import create_app


def _read_password(prompt: str) -> str:
    if sys.stdin.isatty():
        a, b = getpass.getpass(prompt), getpass.getpass("repeat: ")
        if a != b:
            raise SystemExit("passwords do not match")
        return a
    return sys.stdin.readline().rstrip("\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ge-panel")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--demo", action="store_true", help="seed synthetic packs/events/alerts/releases into the panel DB")
    sp = ap.add_subparsers(dest="cmd")
    ad = sp.add_parser("admin", help="create the first admin (password from stdin / prompt)")
    ad.add_argument("action", choices=["create"]); ad.add_argument("username")
    rl = sp.add_parser("release", help="local versioned releases (no cloud deploy)")
    rl.add_argument("action", choices=["deploy", "rollback", "list"]); rl.add_argument("artifact", nargs="?"); rl.add_argument("--version")
    rl.add_argument("--migration", type=int, default=db.MIGRATIONS[-1][0]); rl.add_argument("--to"); rl.add_argument("--notes", default="")
    a = ap.parse_args(argv)
    cfg = config.load(a.config)
    vault = None
    try:
        vault = Vault(cfg.get("vault", {}).get("path") or f"{cfg['panel']['state_dir']}/credentials.vault")
    except VaultError:
        print("vault locked (set GE_VAULT_PASSPHRASE) — running without credentials", file=sys.stderr)
    conn = db.connect(cfg["database"]["primary"])
    db.migrate(conn)
    if a.cmd == "admin":
        try:
            auth.create_user(conn, a.username, _read_password(f"password for {a.username}: "), "admin")
        except auth.AuthError as e:
            print(f"error: {e}", file=sys.stderr); return 2
        print(f"admin {a.username} created"); return 0
    index_db = None
    if cfg["database"].get("index"):
        from galleryedge_db.client import connect as idx_connect
        index_db = idx_connect(cfg["database"]["index"], vault.resolve(cfg["database"].get("index_token_ref")) if vault else None)
    app = create_app(cfg, conn, vault, index_db=index_db)
    if a.cmd == "release":
        from galleryedge_ops.releases import ReleaseError
        st = app.state.release_store
        try:
            if a.action == "list":
                print(json.dumps({"current": st.current(), "releases": st.list()}, indent=1)); return 0
            if a.action == "deploy":
                if not (a.artifact and a.version):
                    ap.error("deploy needs ARTIFACT_DIR and --version")
                ev = st.deploy(a.artifact, a.version, migration=a.migration, notes=a.notes, backup_fn=app.state.backup_fn)
            else:
                ev = st.rollback(to=a.to, backup_fn=app.state.backup_fn)
        except ReleaseError as e:
            print(f"error: {e}", file=sys.stderr); return 2
        print(json.dumps(ev, indent=1)); return 0
    if a.demo:
        from .demo import seed
        seed(conn, app)
    if auth.user_count(conn) == 0:
        print("no accounts yet: run `ge-panel --config ... admin create <name>` first (login is always required)", file=sys.stderr)
    host, port = cfg["panel"].get("bind", "127.0.0.1"), cfg["panel"].get("port", 8790)
    import uvicorn
    print(f"GalleryEdge panel on http://{host}:{port}  (takeover={'on' if cfg['panel'].get('takeover') else 'off'})", flush=True)
    stop = app.state.start_sampler()
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
