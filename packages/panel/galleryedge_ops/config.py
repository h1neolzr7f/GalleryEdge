"""Config center: single document, strict validation before activation."""
from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

import jsonschema

SECRETISH = re.compile(r"(?i)(token|secret|password|api_?key|passphrase)")


class ConfigError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def schema() -> dict:
    return json.loads(resources.files(__package__).joinpath("schemas/config.schema.json").read_text("utf-8"))


def _walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def validate(cfg: dict) -> dict:
    v = jsonschema.Draft202012Validator(schema())
    problems = [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in sorted(v.iter_errors(cfg), key=lambda e: list(map(str, e.path)))]
    ids = [b.get("id") for b in cfg.get("backends", []) if isinstance(b, dict)]
    if len(ids) != len(set(ids)):
        problems.append("backends: duplicate id")
    for b in cfg.get("backends", []) if isinstance(cfg.get("backends"), list) else []:
        if isinstance(b, dict):
            if b.get("type") == "local" and not b.get("path"):
                problems.append(f"backends/{b.get('id')}: local backend needs path")
            if b.get("type") in ("http", "libsql", "s3", "r2") and not b.get("url"):
                problems.append(f"backends/{b.get('id')}: {b.get('type')} backend needs url")
    for p, val in _walk(cfg):
        leaf = p.rsplit(".", 1)[-1]
        if SECRETISH.search(leaf) and not leaf.endswith("_ref") and isinstance(val, str) and val:
            problems.append(f"{p}: inline secret not allowed, use a vault:<name> reference")
    if problems:
        raise ConfigError(problems)
    return cfg


def load(path: str | Path) -> dict:
    p = Path(path)
    cfg = json.loads(p.read_text("utf-8"))
    validate(cfg)
    base = p.resolve().parent
    def rel(x: str) -> str:
        if x.startswith("file:"):
            return "file:" + str((base / x[5:]).resolve()) if not Path(x[5:]).is_absolute() else x
        return str((base / x).resolve()) if not Path(x).is_absolute() else x
    cfg["_base"] = str(base)
    cfg["panel"]["state_dir"] = rel(cfg["panel"].get("state_dir", "./var"))
    if cfg["database"]["primary"].startswith("file:") or not re.match(r"^\w+://", cfg["database"]["primary"]) and cfg["database"]["primary"] != ":memory:":
        cfg["database"]["primary"] = rel(cfg["database"]["primary"])
    for b in cfg["backends"]:
        if b.get("path"):
            b["path"] = rel(b["path"])
    if cfg.get("vault", {}).get("path"):
        cfg["vault"]["path"] = rel(cfg["vault"]["path"])
    cfg.setdefault("cleanup", {})["roots"] = [rel(r) for r in cfg.get("cleanup", {}).get("roots", [])]
    return cfg
