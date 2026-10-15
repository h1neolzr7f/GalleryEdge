"""site-pack-v1: the contract between collection and ingest.

A pack is an uncompressed tar with:
  manifest.json   {"format":"site-pack-v1","pack_id","created_at","adapter","record_count","files":{name:{sha256,size}}}
  works.jsonl     one record per line (see REQUIRED / PROVENANCE_REQUIRED)
  media/<...>     optional, only when the crawl ran with fetch_media=true
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path

FORMAT = "site-pack-v1"
REQUIRED = ("id", "title", "source_url", "license", "rating")
PROVENANCE_REQUIRED = ("adapter", "fetched_at", "fetch_url", "robots", "content_sha256")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
MAX_PACK_MEMBERS = 100_000
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 256 * 1024 * 1024


class PackError(ValueError):
    pass


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def validate_record(r: dict) -> list[str]:
    p = [f"missing {k}" for k in REQUIRED if not r.get(k)]
    if r.get("id") and not ID_RE.match(str(r["id"])):
        p.append("bad id")
    if r.get("source_url") and not str(r["source_url"]).startswith(("http://", "https://")):
        p.append("source_url must be http(s)")
    prov = r.get("provenance") or {}
    p += [f"missing provenance.{k}" for k in PROVENANCE_REQUIRED if not prov.get(k)]
    if prov.get("robots") not in (None, "allowed", "not-applicable"):
        p.append("provenance.robots must be allowed/not-applicable")
    if not isinstance(r.get("tags", []), list):
        p.append("tags must be a list")
    return p


def write_pack(path: str | Path, pack_id: str, adapter: str, records: list[dict], media: dict[str, bytes] | None = None) -> dict:
    if not ID_RE.match(pack_id):
        raise PackError("bad pack_id")
    bad = {r.get("id"): validate_record(r) for r in records if validate_record(r)}
    if bad:
        raise PackError(f"invalid records: {bad}")
    files = {"works.jsonl": "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records).encode()}
    for name, data in (media or {}).items():
        if ".." in name or name.startswith("/"):
            raise PackError("bad media name")
        files[f"media/{name}"] = data
    man = {"format": FORMAT, "pack_id": pack_id, "adapter": adapter, "record_count": len(records),
           "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "files": {n: {"sha256": sha256(b), "size": len(b)} for n, b in files.items()}}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".partial")
    with tarfile.open(tmp, "w") as tf:
        for n, b in [("manifest.json", json.dumps(man, indent=1, sort_keys=True).encode()), *files.items()]:
            ti = tarfile.TarInfo(n); ti.size = len(b); ti.mode = 0o644
            tf.addfile(ti, io.BytesIO(b))
    tmp.replace(path)  # atomic: a half-written pack never looks complete
    return man


def read_pack(path: str | Path, *, include_media: bool = True) -> tuple[dict, list[dict], dict[str, bytes]]:
    """Verify *all* members. Metadata-only ingesters never materialize media bytes."""
    files: dict[str, bytes] = {}
    verified: dict[str, tuple[str, int]] = {}
    with tarfile.open(path, "r:") as tf:
        for i, m in enumerate(tf):
            if i >= MAX_PACK_MEMBERS:
                raise PackError("too many archive members")
            name = m.name
            if (not m.isfile() or name.startswith("/") or ".." in name.split("/")
                    or name not in ("manifest.json", "works.jsonl") and not name.startswith("media/")
                    or name in verified):
                raise PackError(f"unsafe or duplicate member {name!r}")
            if name == "manifest.json" and m.size > MAX_MANIFEST_BYTES:
                raise PackError("manifest too large")
            if name == "works.jsonl" and m.size > MAX_RECORD_BYTES:
                raise PackError("records too large")
            stream = tf.extractfile(m)
            if stream is None:
                raise PackError(f"unreadable member {name!r}")
            should_keep = name in ("manifest.json", "works.jsonl") or include_media
            buf = bytearray() if should_keep else None
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
                if buf is not None:
                    buf.extend(chunk)
            verified[name] = (digest.hexdigest(), total)
            if buf is not None:
                files[name] = bytes(buf)
    if "manifest.json" not in files or "works.jsonl" not in files:
        raise PackError("missing manifest.json or works.jsonl")
    try:
        man = json.loads(files.pop("manifest.json"))
    except (ValueError, TypeError):
        raise PackError("invalid manifest.json") from None
    if not isinstance(man, dict) or man.get("format") != FORMAT:
        raise PackError("unsupported pack format")
    manifest_files = man.get("files")
    if not isinstance(manifest_files, dict) or set(manifest_files) != set(verified) - {"manifest.json"}:
        raise PackError("manifest file list mismatch")
    for name, meta in manifest_files.items():
        if (not isinstance(meta, dict) or
                meta.get("sha256") != verified[name][0] or
                meta.get("size") != verified[name][1]):
            raise PackError(f"checksum mismatch: {name}")
    try:
        records = [json.loads(line) for line in files["works.jsonl"].decode("utf-8").splitlines() if line.strip()]
    except (ValueError, UnicodeDecodeError):
        raise PackError("invalid works.jsonl") from None
    if len(records) != man.get("record_count"):
        raise PackError("record_count mismatch")
    for r in records:
        if not isinstance(r, dict) or validate_record(r):
            raise PackError(f"invalid record: {r.get('id') if isinstance(r, dict) else '<non-object>'}")
    return man, records, {name[6:]: data for name, data in files.items() if name.startswith("media/")}
