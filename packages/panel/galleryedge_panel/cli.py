"""ge-panel: run the local ops panel."""
from __future__ import annotations

import argparse
import sys

from galleryedge_ops import config, db
from galleryedge_ops.vault import Vault, VaultError

from .server import create_app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ge-panel")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--demo", action="store_true", help="seed synthetic packs/events/alerts into the panel DB")
    a = ap.parse_args(argv)
    cfg = config.load(a.config)
    vault = None
    try:
        vault = Vault(cfg.get("vault", {}).get("path") or f"{cfg['panel']['state_dir']}/credentials.vault")
    except VaultError:
        print("vault locked (set GE_VAULT_PASSPHRASE) — running without credentials", file=sys.stderr)
    conn = db.connect(cfg["database"]["primary"])
    db.migrate(conn)
    if a.demo:
        from .demo import seed
        seed(conn)
    host, port = cfg["panel"].get("bind", "127.0.0.1"), cfg["panel"].get("port", 8790)
    import uvicorn
    print(f"GalleryEdge panel on http://{host}:{port}  (takeover={'on' if cfg['panel'].get('takeover') else 'off'})")
    uvicorn.run(create_app(cfg, conn, vault), host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
