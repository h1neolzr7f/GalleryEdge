"""Rule-based cleanup of local copies that are CONFIRMED uploaded.

Deletable:
  * files inside a pack directory that carries a ``.verified_remote`` marker
  * ``<name>.tar`` whose ``<name>.tar.uploaded`` marker records the same size and sha256
Never deletable (checked again right before each unlink):
  * ledgers: done.txt, seen.txt, packed_works.txt, prompts.jsonl (anywhere)
  * markers, manifests (manifest*.json), vault files (*.vault)
  * any tar without a matching upload marker
  * anything under a ``staging`` directory or a directory holding ``.inprogress`` / ``.lock``
  * symlinks, and anything resolving outside the configured roots
Default mode is dry-run.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

LEDGERS = frozenset({"done.txt", "seen.txt", "packed_works.txt", "prompts.jsonl"})
PROTECTED_GLOBS = ("manifest*.json", "*.vault", ".verified_remote", "*.uploaded", ".inprogress", ".lock")
IN_PROGRESS_MARKERS = (".inprogress", ".lock")


@dataclass
class Plan:
    delete: list[Path] = field(default_factory=list)
    protected: list[tuple[Path, str]] = field(default_factory=list)
    bytes: int = 0


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def protect_reason(p: Path, roots: list[Path], extra: tuple[str, ...] = ()) -> str | None:
    if p.is_symlink():
        return "symlink"
    rp = p.resolve()
    if not any(rp.is_relative_to(r) and rp != r for r in roots):
        return "outside roots"
    if p.name in LEDGERS:
        return "ledger"
    for g in PROTECTED_GLOBS + tuple(extra):
        if fnmatch.fnmatch(p.name, g):
            return f"protected pattern {g}"
    root = next(r for r in roots if rp.is_relative_to(r))
    cur = rp.parent
    while True:
        if cur.name == "staging":
            return "in-progress staging"
        if any((cur / m).exists() for m in IN_PROGRESS_MARKERS):
            return "in-progress marker"
        if cur == root:
            break
        cur = cur.parent
    if p.suffix == ".tar":
        m = p.with_name(p.name + ".uploaded")
        if not m.exists():
            return "tar not uploaded"
        try:
            meta = json.loads(m.read_text("utf-8"))
        except (ValueError, OSError):
            return "tar upload marker unreadable"
        if meta.get("size") != p.stat().st_size or meta.get("sha256") != _sha256(p):
            return "tar upload marker mismatch"
        return None
    if not any((a / ".verified_remote").exists() for a in [rp.parent, *rp.parent.parents] if a.is_relative_to(root)):
        return "pack not verified remote"
    return None


def plan(roots: list[str | Path], extra_protected: list[str] | None = None) -> Plan:
    rs = [Path(r).resolve() for r in roots]
    extra = tuple(extra_protected or ())
    pl = Plan()
    for r in rs:
        if not r.is_dir():
            continue
        for dirpath, dirnames, files in os.walk(r, followlinks=False):
            for f in files:
                p = Path(dirpath) / f
                why = protect_reason(p, rs, extra)
                if why:
                    pl.protected.append((p, why))
                else:
                    pl.delete.append(p)
                    pl.bytes += p.lstat().st_size
    return pl


def apply(roots: list[str | Path], *, dry_run: bool = True, extra_protected: list[str] | None = None, on_delete=None) -> Plan:
    pl = plan(roots, extra_protected)
    if dry_run:
        return pl
    rs = [Path(r).resolve() for r in roots]
    done = Plan(protected=pl.protected)
    for p in pl.delete:
        if protect_reason(p, rs, tuple(extra_protected or ())) is None:  # re-check at delete time
            size = p.lstat().st_size
            p.unlink()
            done.delete.append(p)
            done.bytes += size
            if on_delete:
                on_delete(p)
    for r in rs:  # prune now-empty dirs (never the roots)
        for dirpath, _, _ in sorted(os.walk(r), key=lambda t: -len(t[0])):
            d = Path(dirpath)
            anc = [a for a in [d, *d.parents] if a.is_relative_to(r) and a != r]
            if (d != r and not any(d.iterdir()) and not any(a.name == "staging" for a in anc)
                    and any((a / ".verified_remote").exists() for a in anc[1:])):
                d.rmdir()
    return done
