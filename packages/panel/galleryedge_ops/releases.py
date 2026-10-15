"""Versioned releases with one-click rollback, against a LOCAL target directory.

Simulates the deploy pipeline (Worker bundle / panel build) without any cloud call:
each release is an immutable copy under ``releases/<version>/`` with a checksummed
manifest; ``current`` is switched atomically; every switch first takes a panel backup so
a rollback across a DB migration can be restored. A real wrangler deploy would plug in as
another target with the same interface.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

VER_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


class ReleaseError(Exception):
    pass


def _tree_hash(d: Path) -> dict[str, str]:
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file() and p.name != "release.json"}


class ReleaseStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        (self.root / "releases").mkdir(parents=True, exist_ok=True)
        self.log = self.root / "history.jsonl"

    def _write_current(self, version: str):
        tmp = self.root / ".current.tmp"
        tmp.write_text(version)
        os.replace(tmp, self.root / "current")

    def current(self) -> str | None:
        p = self.root / "current"
        return p.read_text().strip() if p.exists() else None

    def manifest(self, version: str) -> dict:
        return json.loads((self.root / "releases" / version / "release.json").read_text())

    def list(self) -> list[dict]:
        cur = self.current()
        out = []
        for d in sorted((self.root / "releases").iterdir(), key=lambda p: p.stat().st_mtime):
            if (d / "release.json").exists():
                m = self.manifest(d.name)
                out.append({**{k: m[k] for k in ("version", "created_at", "migration", "files", "notes")}, "current": d.name == cur})
        return out

    def history(self) -> list[dict]:
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def verify(self, version: str) -> bool:
        d = self.root / "releases" / version
        return _tree_hash(d) == self.manifest(version)["sha256"]

    def deploy(self, artifact_dir: str | Path, version: str, *, migration: int, notes: str = "", backup_fn=None, actor: str = "cli") -> dict:
        if not VER_RE.match(version):
            raise ReleaseError("version must look like 1.2.3")
        src = Path(artifact_dir)
        if not src.is_dir():
            raise ReleaseError("artifact dir missing")
        dst = self.root / "releases" / version
        if dst.exists():
            raise ReleaseError(f"release {version} already exists (releases are immutable)")
        tmp = self.root / "releases" / f".{version}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, tmp)
        sha = _tree_hash(tmp)
        man = {"version": version, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "migration": migration,
               "files": len(sha), "sha256": sha, "notes": notes}
        (tmp / "release.json").write_text(json.dumps(man, indent=1))
        os.replace(tmp, dst)
        return self.switch(version, actor=actor, action="deploy", backup_fn=backup_fn)

    def switch(self, version: str, *, actor: str, action: str, backup_fn=None) -> dict:
        if not isinstance(version, str) or not VER_RE.fullmatch(version):
            raise ReleaseError("invalid release version")
        if not (self.root / "releases" / version / "release.json").exists():
            raise ReleaseError(f"unknown release {version}")
        if not self.verify(version):
            raise ReleaseError(f"release {version} failed checksum verification; refusing to activate")
        prev = self.current()
        backup = backup_fn(prev or "none") if backup_fn else None
        self._write_current(version)
        mig_prev = self.manifest(prev)["migration"] if prev else None
        mig_new = self.manifest(version)["migration"]
        ev = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": action, "actor": actor, "from": prev, "to": version,
              "backup": str(backup) if backup else None,
              "db_restore_required": mig_prev is not None and mig_new < mig_prev}
        with self.log.open("a") as f:
            f.write(json.dumps(ev) + "\n")
        return ev

    def rollback(self, *, to: str | None = None, actor: str = "cli", backup_fn=None) -> dict:
        cur = self.current()
        if not cur:
            raise ReleaseError("nothing deployed")
        if to is None:
            order = [r["version"] for r in sorted(self.list(), key=lambda r: r["created_at"])]
            i = order.index(cur)
            if i == 0:
                raise ReleaseError("no previous release to roll back to")
            to = order[i - 1]
        if to == cur:
            raise ReleaseError("already on that release")
        return self.switch(to, actor=actor, action="rollback", backup_fn=backup_fn)
