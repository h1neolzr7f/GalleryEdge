"""ge-panel: run the local ops panel; manage admin accounts and local releases."""
from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
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
    ap.add_argument("--setup", action="store_true", help="first-run wizard: create config/admin/vault in a directory (opens http://127.0.0.1:8799)")
    ap.add_argument("--setup-dir", default="./galleryedge-data", help="with --setup: where to write config.json and var/")
    ap.add_argument("--setup-port", type=int, default=8799, help="with --setup: wizard listen port (localhost only)")
    sp = ap.add_subparsers(dest="cmd")
    ad = sp.add_parser("admin", help="create the first admin (password from stdin / prompt)")
    ad.add_argument("action", choices=["create"]); ad.add_argument("username")
    rl = sp.add_parser("release", help="local versioned releases (no cloud deploy)")
    rl.add_argument("action", choices=["deploy", "rollback", "list"]); rl.add_argument("artifact", nargs="?"); rl.add_argument("--version")
    rl.add_argument("--migration", type=int, default=db.MIGRATIONS[-1][0]); rl.add_argument("--to"); rl.add_argument("--notes", default="")
    a = ap.parse_args(argv)
    if a.setup:
        return run_setup_wizard(a.setup_dir, a.setup_port)
    # first-run: no config yet → drop into the wizard automatically
    if not Path(a.config).exists() and a.cmd is None:
        print(f"还没有配置文件 {a.config}，正在打开首次安装向导…", flush=True)
        return run_setup_wizard(str(Path(a.config).resolve().parent / "galleryedge-data"), 8799)
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
        tok = vault.resolve(cfg["database"].get("index_token_ref")) if vault and cfg["database"].get("index_token_ref") else None
        tf = cfg["database"].get("index_token_file")
        if not tok and tf:  # 0600 file with a raw token or JSON {"ro_token": ...}; only a READ-ONLY token belongs here
            raw = Path(tf).read_text("utf-8").strip()
            tok = json.loads(raw).get("ro_token") if raw.startswith("{") else raw
        index_db = idx_connect(cfg["database"]["index"], tok, prefix=cfg["database"].get("index_prefix", ""))
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


def run_setup_wizard(root: str, port: int) -> int:
    """Localhost-only first-run wizard (no existing config required)."""
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn
    from galleryedge_ops.setup import SetupError, apply, wizard_defaults

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    root = str(Path(root).resolve())

    @app.middleware("http")
    async def local_only(request, call_next):
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1", "testclient", "testserver"):
            return JSONResponse({"error": "setup wizard is localhost-only"}, status_code=403)
        return await call_next(request)

    @app.get("/api/setup/defaults")
    def defaults():
        return wizard_defaults(root)

    @app.post("/api/setup/apply")
    def do_apply(body: dict = Body(...)):
        try:
            return apply(body.get("root") or root,
                         username=str(body.get("username") or "").strip(),
                         password=str(body.get("password") or ""),
                         vault_passphrase=str(body.get("vault_passphrase") or ""),
                         port=int(body.get("port") or 8790),
                         turso_token_file=(body.get("turso_token_file") or None),
                         turso_url=(body.get("turso_url") or None))
        except (SetupError, auth.AuthError, VaultError, ValueError) as e:
            raise HTTPException(400, str(e))

    @app.get("/")
    def page():
        html = Path(__file__).resolve().parent / "static" / "setup.html"
        return HTMLResponse(html.read_text("utf-8"))

    print(f"GalleryEdge 首次安装向导 → http://127.0.0.1:{port}/  （仅本机，数据目录 {root}）", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


