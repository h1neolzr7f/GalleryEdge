"""One-shot backup/restore of config, panel DB and (encrypted) vault."""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

FORMAT = "galleryedge-backup-v1"


class BackupError(Exception):
    pass


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sqlite_snapshot(db_path: str) -> bytes:
    src = sqlite3.connect(db_path)
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "snap.db"
        dst = sqlite3.connect(out)
        src.backup(dst)
        dst.close()
        src.close()
        return out.read_bytes()


def create(out_path: str | Path, *, config_path: str | Path | None, db_path: str | None, vault_path: str | Path | None) -> dict:
    members: dict[str, bytes] = {}
    if config_path:
        members["config.json"] = Path(config_path).read_bytes()
    if db_path and Path(db_path).exists():
        members["panel.db"] = _sqlite_snapshot(db_path)
    if vault_path and Path(vault_path).exists():
        members["credentials.vault"] = Path(vault_path).read_bytes()  # already encrypted
    if not members:
        raise BackupError("nothing to back up")
    manifest = {"format": FORMAT, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "files": {n: {"sha256": _sha(b), "size": len(b)} for n, b in members.items()}}
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tf:
        for name, data in [("manifest.json", json.dumps(manifest, indent=1).encode()), *members.items()]:
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode = len(data), 0o600
            tf.addfile(ti, io.BytesIO(data))
    return manifest


def _read(archive: str | Path) -> tuple[dict, dict[str, bytes]]:
    files: dict[str, bytes] = {}
    with tarfile.open(archive, "r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile() or "/" in m.name or m.name.startswith(".") or ".." in m.name:
                raise BackupError(f"unsafe member: {m.name!r}")
            files[m.name] = tf.extractfile(m).read()
    if "manifest.json" not in files:
        raise BackupError("missing manifest")
    man = json.loads(files.pop("manifest.json"))
    if man.get("format") != FORMAT:
        raise BackupError("unknown backup format")
    if set(man["files"]) != set(files):
        raise BackupError("manifest/file list mismatch")
    for n, meta in man["files"].items():
        if _sha(files[n]) != meta["sha256"]:
            raise BackupError(f"checksum mismatch: {n}")
    if "panel.db" in files:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.db"
            p.write_bytes(files["panel.db"])
            c = sqlite3.connect(p)
            ok = c.execute("PRAGMA integrity_check").fetchone()[0]
            c.close()
            if ok != "ok":
                raise BackupError("panel.db integrity check failed")
    return man, files


def verify(archive: str | Path) -> dict:
    return _read(archive)[0]


def restore(archive: str | Path, target_dir: str | Path) -> list[str]:
    _, files = _read(archive)
    t = Path(target_dir)
    if t.exists() and any(t.iterdir()):
        raise BackupError("restore target must be empty (refusing to overwrite)")
    t.mkdir(parents=True, exist_ok=True)
    for n, b in files.items():
        (t / n).write_bytes(b)
        (t / n).chmod(0o600)
    return sorted(files)
