"""Encrypted credential vault (AES-256-GCM, scrypt-derived key).

Plaintext never touches disk or logs. Each entry tracks created/rotated/expiry
so ``expiring()`` can drive reminders. ``rekey()`` changes the passphrase.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .masking import mask_value, register_secret

ENV_PASSPHRASE = "GE_VAULT_PASSPHRASE"


class VaultError(Exception):
    pass


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _ub64(s: str) -> bytes:
    return base64.b64decode(s)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Vault:
    def __init__(self, path: str | Path, passphrase: str | None = None, *, scrypt_n: int = 2**15):
        self.path = Path(path)
        pw = passphrase if passphrase is not None else os.environ.get(ENV_PASSPHRASE)
        if not pw or len(pw) < 12:
            raise VaultError(f"vault passphrase missing or shorter than 12 chars (set {ENV_PASSPHRASE})")
        self._pw = pw.encode()
        if self.path.exists():
            self.doc = json.loads(self.path.read_text("utf-8"))
        else:
            self.doc = {"format": "galleryedge-vault-v1", "kdf": {"salt": _b64(os.urandom(16)), "n": scrypt_n, "r": 8, "p": 1}, "check": None, "entries": {}}
        self._key = self._derive(self.doc["kdf"])
        self._verify_or_init_check()

    def _derive(self, kdf: dict) -> bytes:
        return Scrypt(salt=_ub64(kdf["salt"]), length=32, n=kdf["n"], r=kdf["r"], p=kdf["p"]).derive(self._pw)

    def _enc(self, plaintext: bytes, aad: bytes, key: bytes | None = None) -> dict:
        nonce = os.urandom(12)
        return {"nonce": _b64(nonce), "ct": _b64(AESGCM(key or self._key).encrypt(nonce, plaintext, aad))}

    def _dec(self, blob: dict, aad: bytes) -> bytes:
        try:
            return AESGCM(self._key).decrypt(_ub64(blob["nonce"]), _ub64(blob["ct"]), aad)
        except InvalidTag as e:
            raise VaultError("wrong passphrase or tampered vault") from e

    def _verify_or_init_check(self) -> None:
        if self.doc.get("check"):
            self._dec(self.doc["check"], b"check")
        else:
            self.doc["check"] = self._enc(b"galleryedge", b"check")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".vault-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.doc, f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def set(self, name: str, value: str, *, expires_days: int | None = None, note: str = "") -> None:
        if not value:
            raise VaultError("empty secret")
        now = _now()
        prev = self.doc["entries"].get(name)
        e = {**self._enc(value.encode(), name.encode()),
             "created_at": prev["created_at"] if prev else now.isoformat(timespec="seconds"),
             "rotated_at": now.isoformat(timespec="seconds"),
             "expires_at": (now + timedelta(days=expires_days)).isoformat(timespec="seconds") if expires_days else None,
             "version": (prev["version"] + 1) if prev else 1,
             "note": note,
             "mask": mask_value(value)}
        if prev:
            e["previous"] = {k: prev[k] for k in ("nonce", "ct", "version")}
        self.doc["entries"][name] = e
        register_secret(value)
        self.save()

    def rotate(self, name: str, new_value: str, *, expires_days: int | None = None) -> int:
        if name not in self.doc["entries"]:
            raise VaultError(f"no such credential: {name}")
        if new_value == self.get(name):
            raise VaultError("rotation must change the value")
        self.set(name, new_value, expires_days=expires_days, note=self.doc["entries"][name].get("note", ""))
        return self.doc["entries"][name]["version"]

    def get(self, name: str) -> str:
        e = self.doc["entries"].get(name)
        if not e:
            raise VaultError(f"no such credential: {name}")
        v = self._dec(e, name.encode()).decode()
        register_secret(v)
        return v

    def resolve(self, ref: str | None) -> str | None:
        if not ref:
            return None
        if not ref.startswith("vault:"):
            raise VaultError("references must look like vault:<name>")
        return self.get(ref[6:])

    def delete(self, name: str) -> None:
        self.doc["entries"].pop(name, None)
        self.save()

    def list(self) -> list[dict]:
        return [{"name": n, "mask": e["mask"], "version": e["version"], "rotated_at": e["rotated_at"], "expires_at": e["expires_at"]}
                for n, e in sorted(self.doc["entries"].items())]

    def expiring(self, within_days: int = 14, *, now: datetime | None = None) -> list[dict]:
        now = now or _now()
        out = []
        for item in self.list():
            if item["expires_at"]:
                left = (datetime.fromisoformat(item["expires_at"]) - now).total_seconds() / 86400
                if left <= within_days:
                    out.append({**item, "days_left": round(left, 1), "expired": left <= 0})
        return out

    def rekey(self, new_passphrase: str) -> None:
        if len(new_passphrase) < 12:
            raise VaultError("new passphrase too short")
        plain = {n: self.get(n) for n in self.doc["entries"]}
        kdf = {**self.doc["kdf"], "salt": _b64(os.urandom(16))}
        self._pw = new_passphrase.encode()
        self._key = self._derive(kdf)
        self.doc["kdf"] = kdf
        self.doc["check"] = self._enc(b"galleryedge", b"check")
        for n, v in plain.items():
            e = self.doc["entries"][n]
            e.update(self._enc(v.encode(), n.encode()))
            e.pop("previous", None)
        self.save()
