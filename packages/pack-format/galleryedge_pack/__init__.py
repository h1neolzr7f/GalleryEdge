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


def read_pack(path: str | Path) -> tuple[dict, list[dict], dict[str, bytes]]:
    files: dict[str, bytes] = {}
    with tarfile.open(path, "r:") as tf:
        for m in tf.getmembers():
            if not m.isfile() or m.name.startswith("/") or ".." in m.name.split("/"):
                raise PackError(f"unsafe member {m.name!r}")
            files[m.name] = tf.extractfile(m).read()
    try:
        man = json.loads(files.pop("manifest.json"))
    except KeyError:
        raise PackError("missing manifest.json") from None
    if man.get("format") != FORMAT:
        raise PackError("unsupported pack format")
    if set(man["files"]) != set(files):
        raise PackError("manifest file list mismatch")
    for n, meta in man["files"].items():
        if sha256(files[n]) != meta["sha256"] or len(files[n]) != meta["size"]:
            raise PackError(f"checksum mismatch: {n}")
    records = [json.loads(l) for l in files["works.jsonl"].decode().splitlines() if l.strip()]
    if len(records) != man["record_count"]:
        raise PackError("record_count mismatch")
    for r in records:
        if validate_record(r):
            raise PackError(f"invalid record {r.get('id')}: {validate_record(r)}")
    return man, records, {n[6:]: b for n, b in files.items() if n.startswith("media/")}
