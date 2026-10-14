import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from conftest import PW
from galleryedge_ops import masking
from galleryedge_ops.vault import Vault, VaultError

SECRET = "FAKE_SECRET_value_abcdef123456"


def mk(tmp_path, **kw):
    return Vault(tmp_path / "c.vault", PW, scrypt_n=2**10, **kw)


def test_roundtrip_and_no_plaintext_on_disk(tmp_path):
    v = mk(tmp_path)
    v.set("r2", SECRET, expires_days=30)
    raw = (tmp_path / "c.vault").read_text()
    assert SECRET not in raw
    assert Vault(tmp_path / "c.vault", PW).get("r2") == SECRET
    assert (tmp_path / "c.vault").stat().st_mode & 0o777 == 0o600


def test_wrong_passphrase_and_tamper(tmp_path):
    mk(tmp_path).set("a", SECRET)
    with pytest.raises(VaultError):
        Vault(tmp_path / "c.vault", "FAKE-wrong-passphrase")
    d = json.loads((tmp_path / "c.vault").read_text())
    d["entries"]["b"] = d["entries"]["a"]  # AAD binds ciphertext to its name
    (tmp_path / "c.vault").write_text(json.dumps(d))
    with pytest.raises(VaultError):
        Vault(tmp_path / "c.vault", PW).get("b")


def test_short_passphrase_rejected(tmp_path):
    with pytest.raises(VaultError):
        Vault(tmp_path / "x", "short")


def test_rotate_versions_and_list_is_masked(tmp_path):
    v = mk(tmp_path)
    v.set("k", SECRET)
    with pytest.raises(VaultError):
        v.rotate("k", SECRET)
    assert v.rotate("k", SECRET + "X", expires_days=7) == 2
    assert v.get("k") == SECRET + "X"
    assert SECRET not in json.dumps(v.list())
    with pytest.raises(VaultError):
        v.rotate("missing", "FAKE_value_1234")


def test_expiring(tmp_path):
    v = mk(tmp_path)
    v.set("soon", "FAKE_aaaaaaaa1", expires_days=3)
    v.set("later", "FAKE_aaaaaaaa2", expires_days=90)
    v.set("never", "FAKE_aaaaaaaa3")
    names = [e["name"] for e in v.expiring(14)]
    assert names == ["soon"]
    future = datetime.now(timezone.utc) + timedelta(days=5)
    assert v.expiring(14, now=future)[0]["expired"] is True


def test_rekey(tmp_path):
    v = mk(tmp_path)
    v.set("k", SECRET)
    v.rekey("FAKE-new-passphrase-456")
    with pytest.raises(VaultError):
        Vault(tmp_path / "c.vault", PW)
    assert Vault(tmp_path / "c.vault", "FAKE-new-passphrase-456").get("k") == SECRET


def test_resolve_ref(tmp_path):
    v = mk(tmp_path)
    v.set("tok", SECRET)
    assert v.resolve("vault:tok") == SECRET and v.resolve(None) is None
    with pytest.raises(VaultError):
        v.resolve("tok")


def test_redaction_patterns():
    s = masking.redact("Authorization: Bearer abcdefghijklmnop token=zzzzzzzzzz ghp_" + "a" * 36)
    assert "abcdefghijklmnop" not in s and "zzzzzzzzzz" not in s and "a" * 36 not in s
    masking.register_secret("FAKE_known_secret_999")
    assert "FAKE_known_secret_999" not in masking.redact("x FAKE_known_secret_999 y")
    assert masking.redact_obj({"api_key": "v", "n": [{"password": "p"}]}) == {"api_key": "****", "n": [{"password": "****"}]}
    assert masking.mask_value("abcdefghijk") == "ab****jk" and masking.mask_value("abc") == "****"


def test_logging_filter_masks(caplog, tmp_path):
    v = mk(tmp_path)
    v.set("k", "FAKE_logged_secret_777")
    lg = logging.getLogger("ge-test")
    lg.addFilter(masking.RedactingFilter())
    with caplog.at_level(logging.INFO, logger="ge-test"):
        lg.info("using %s", v.get("k"))
    assert "FAKE_logged_secret_777" not in caplog.text and "****" in caplog.text
