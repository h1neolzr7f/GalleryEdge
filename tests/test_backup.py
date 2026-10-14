import io
import sqlite3
import tarfile

import pytest
from conftest import PW
from galleryedge_ops import backup, db, events
from galleryedge_ops.vault import Vault


def setup(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"version":1}')
    dbp = tmp_path / "panel.db"
    c = db.connect(str(dbp)); db.migrate(c); events.audit(c, "t", "hello"); c.close()
    v = Vault(tmp_path / "c.vault", PW, scrypt_n=2**10); v.set("k", "FAKE_secret_for_backup")
    return cfg, dbp, tmp_path / "c.vault"


def test_backup_restore_to_clean_dir(tmp_path):
    cfg, dbp, vp = setup(tmp_path)
    arc = tmp_path / "b.tar.gz"
    man = backup.create(arc, config_path=cfg, db_path=str(dbp), vault_path=vp)
    assert set(man["files"]) == {"config.json", "panel.db", "credentials.vault"}
    assert backup.verify(arc)["format"] == backup.FORMAT
    out = tmp_path / "restored"
    assert backup.restore(arc, out) == ["config.json", "credentials.vault", "panel.db"]
    c = sqlite3.connect(out / "panel.db")
    assert c.execute("SELECT action FROM audit").fetchone()[0] == "hello"
    assert Vault(out / "credentials.vault", PW).get("k") == "FAKE_secret_for_backup"
    assert b"FAKE_secret_for_backup" not in arc.read_bytes()


def test_restore_refuses_non_empty_target(tmp_path):
    cfg, dbp, vp = setup(tmp_path)
    arc = tmp_path / "b.tar.gz"
    backup.create(arc, config_path=cfg, db_path=str(dbp), vault_path=vp)
    (tmp_path / "t").mkdir(); (tmp_path / "t" / "x").write_text("keep")
    with pytest.raises(backup.BackupError):
        backup.restore(arc, tmp_path / "t")
    assert (tmp_path / "t" / "x").read_text() == "keep"


def _rewrite(arc, out, mutate):
    with tarfile.open(arc) as src, tarfile.open(out, "w:gz") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read()
            m, data = mutate(m, data)
            m.size = len(data)
            dst.addfile(m, io.BytesIO(data))


def test_tampered_and_traversal_rejected(tmp_path):
    cfg, dbp, vp = setup(tmp_path)
    arc = tmp_path / "b.tar.gz"
    backup.create(arc, config_path=cfg, db_path=None, vault_path=None)
    bad = tmp_path / "bad.tar.gz"
    _rewrite(arc, bad, lambda m, d: (m, b'{"version":2}' if m.name == "config.json" else d))
    with pytest.raises(backup.BackupError, match="checksum"):
        backup.verify(bad)
    trav = tmp_path / "trav.tar.gz"
    def ren(m, d):
        if m.name == "config.json":
            m.name = "../evil"
        return m, d
    _rewrite(arc, trav, ren)
    with pytest.raises(backup.BackupError, match="unsafe"):
        backup.restore(trav, tmp_path / "r")
    assert not (tmp_path / "evil").exists()


def test_nothing_to_backup(tmp_path):
    with pytest.raises(backup.BackupError):
        backup.create(tmp_path / "x.tgz", config_path=None, db_path=None, vault_path=None)
