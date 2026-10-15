"""First-run setup: create config, vault, admin account — no CLI walls for beginners.

``ge-panel --setup`` boots a localhost-only wizard that writes a ready-to-run tree
under a chosen directory. Secrets (vault passphrase, API tokens) are only written
to 0600 files or the encrypted vault — never printed.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from . import auth, config, db
from .vault import Vault, VaultError

DEFAULT_CFG = {
    "version": 1,
    "panel": {"bind": "127.0.0.1", "port": 8790, "allow_remote": False, "takeover": False, "state_dir": "var"},
    "database": {"primary": "file:var/panel.db"},
    "backends": [
        {"id": "local-hot", "type": "local", "path": "var/hot", "tier": "hot"},
        {"id": "local-cold", "type": "local", "path": "var/cold", "tier": "cold"},
    ],
    "alerts": {"panel": True, "dedupe_minutes": 30, "fail_threshold": 1},
    "vault": {"path": "var/credentials.vault"},
    "cleanup": {"roots": ["var/hot", "var/cold"]},
    "tiering": {"hot_to_cold_days": 30, "forecast_alert_days": 14, "promote_on_read": False},
    "turso": {"rows_written_quota_month": 10_000_000, "rows_read_quota_month": 500_000_000},
}


class SetupError(ValueError):
    pass


def _abs(root: Path, p: str) -> str:
    path = Path(p)
    return str(path if path.is_absolute() else (root / path).resolve())


def apply(root: str | Path, *, username: str, password: str, vault_passphrase: str,
          port: int = 8790, turso_token_file: str | None = None, turso_url: str | None = None) -> dict:
    """Create a ready-to-run GalleryEdge tree. Returns paths (never secrets)."""
    root = Path(root).resolve()
    # Reject invalid setup input before creating any persistent directories,
    # configuration files or credential material.
    if not auth.USER_RE.fullmatch(username or ""):
        raise SetupError("username: 3-32 chars, lowercase letters, digits, _ . -")
    auth.check_password_policy(password, username)
    if not isinstance(vault_passphrase, str) or len(vault_passphrase) < 12:
        raise SetupError("vault passphrase must be at least 12 characters")
    try:
        port = int(port)
    except (ValueError, TypeError):
        raise SetupError("port must be between 1 and 65535") from None
    if not 1 <= port <= 65535:
        raise SetupError("port must be between 1 and 65535")
    if (root / "config.json").exists() and auth.user_count(db.connect(f"file:{(root / 'var' / 'panel.db')}")) > 0:
        raise SetupError("already set up (config.json + admin exist); open the panel instead")
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    cfg["panel"]["port"] = int(port)
    cfg["panel"]["state_dir"] = _abs(root, "var")
    cfg["database"]["primary"] = "file:" + _abs(root, "var/panel.db")
    cfg["vault"]["path"] = _abs(root, "var/credentials.vault")
    for b in cfg["backends"]:
        b["path"] = _abs(root, b["path"])
    cfg["cleanup"]["roots"] = [b["path"] for b in cfg["backends"]]

    if turso_token_file:
        tf = Path(turso_token_file).expanduser().resolve()
        if not tf.is_file():
            raise SetupError(f"token file not found: {tf}")
        if (tf.stat().st_mode & 0o077) != 0:
            raise SetupError(f"token file must be mode 0600 (got {oct(tf.stat().st_mode & 0o777)})")
        raw = tf.read_text("utf-8").strip()
        data = json.loads(raw) if raw.startswith("{") else {"ro_token": raw}
        url = turso_url or data.get("db_url") or data.get("url")
        if not url:
            raise SetupError("turso token file needs db_url (or pass turso_url)")
        if not (data.get("ro_token") or data.get("rw_token")):
            raise SetupError("turso token file needs ro_token (preferred) or rw_token")
        cfg["database"]["index"] = url
        cfg["database"]["index_token_file"] = str(tf)

    cfg = config.validate(cfg)
    root.mkdir(parents=True, exist_ok=True)
    for d in ("var", "var/hot", "var/cold", "var/packs", "var/crawl", "var/backups", "var/jobs"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # vault passphrase → 0600 env hint file (user can also export GE_VAULT_PASSPHRASE)
    vp = root / "var" / "vault-passphrase.txt"
    if not vp.exists():
        fd = os.open(vp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(vault_passphrase + "\n")
    vault = Vault(cfg["vault"]["path"], vault_passphrase)
    vault.set("setup-marker", "ok")
    os.environ["GE_VAULT_PASSPHRASE"] = vault_passphrase

    conn = db.connect(cfg["database"]["primary"])
    db.migrate(conn)
    if auth.user_count(conn) == 0:
        auth.create_user(conn, username, password, "admin")
    elif not any(u["username"] == username for u in auth.list_users(conn)):
        raise SetupError("panel DB already has users; pick a fresh directory")

    # try building the web UI if node is available (non-fatal)
    web = Path(__file__).resolve().parents[1] / "web"
    built = (web / "dist" / "index.html").exists()
    build_note = "frontend already built" if built else "frontend not built yet (optional)"
    if not built:
        import shutil, subprocess
        if shutil.which("npm") and (web / "package.json").exists():
            r = subprocess.run(["npm", "ci"], cwd=web, capture_output=True, text=True, timeout=180)
            if r.returncode == 0:
                r = subprocess.run(["npm", "run", "build"], cwd=web, capture_output=True, text=True, timeout=180)
                build_note = "frontend built" if r.returncode == 0 else "frontend build failed (panel API still works)"
            else:
                build_note = "npm ci failed (panel API still works)"

    return {
        "root": str(root),
        "config": str(root / "config.json"),
        "panel_url": f"http://127.0.0.1:{port}/",
        "admin": username,
        "vault": cfg["vault"]["path"],
        "has_index": bool(cfg["database"].get("index")),
        "frontend": build_note,
        "next": "打开面板登录；保险库口令已写入 var/vault-passphrase.txt（0600），也可设环境变量 GE_VAULT_PASSPHRASE",
    }


def wizard_defaults(root: str | Path) -> dict:
    root = Path(root).resolve()
    return {
        "root": str(root),
        "suggested_username": "admin",
        "suggested_port": 8790,
        "has_config": (root / "config.json").exists(),
        "steps": ["welcome", "admin", "vault", "turso", "finish"],
    }
