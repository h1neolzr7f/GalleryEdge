import io
import json
import shutil
import sys
from pathlib import Path

from conftest import PW
from galleryedge_ops import cli

EX = Path(__file__).parents[1] / "examples/config.example.json"


def run(capsys, *args):
    rc = cli.main(list(args))
    return rc, capsys.readouterr()


def test_end_to_end(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GE_VAULT_PASSPHRASE", PW)
    cfg = tmp_path / "config.json"; shutil.copy(EX, cfg)
    (tmp_path / "var/storage/hot").mkdir(parents=True); (tmp_path / "var/storage/cold").mkdir()
    c = str(cfg)
    rc, o = run(capsys, "config", "validate", c); assert rc == 0 and '"valid": true' in o.out
    rc, o = run(capsys, "--config", c, "health"); assert rc == 0
    shutil.rmtree(tmp_path / "var/storage/cold")
    rc, o = run(capsys, "--config", c, "health"); assert rc == 1
    rc, o = run(capsys, "--config", c, "alerts"); assert "backend.down.local-cold" in o.out
    monkeypatch.setattr(sys, "stdin", io.StringIO("FAKE_cli_secret_value_42\n"))
    rc, o = run(capsys, "--config", c, "vault", "set", "r2", "--expires-days", "3"); assert rc == 0
    rc, o = run(capsys, "--config", c, "vault", "list"); assert "FAKE_cli_secret_value_42" not in o.out and "FA****42" in o.out
    rc, o = run(capsys, "--config", c, "vault", "get", "r2"); assert "FAKE_cli_secret_value_42" not in o.out
    rc, o = run(capsys, "--config", c, "health"); assert rc == 1
    rc, o = run(capsys, "--config", c, "alerts"); assert "credential.expiring.r2" in o.out
    rc, o = run(capsys, "--config", c, "events", "--limit", "500"); assert "FAKE_cli_secret_value_42" not in o.out
    arc = tmp_path / "bk.tar.gz"
    rc, o = run(capsys, "--config", c, "backup", "create", str(arc)); assert rc == 0
    rc, o = run(capsys, "backup", "restore", str(arc), "--to", str(tmp_path / "restore")); assert rc == 0
    assert (tmp_path / "restore/panel.db").exists()
    (tmp_path / "var/work").mkdir()
    (tmp_path / "var/work/done.txt").write_text("l")
    rc, o = run(capsys, "--config", c, "cleanup", "apply"); assert json.loads(o.out)["dry_run"] is True
    rc, o = run(capsys, "--config", c, "audit"); assert "cleanup.done" in o.out and "vault.set" in o.out


def test_invalid_config_exit_code(tmp_path, capsys):
    p = tmp_path / "c.json"; p.write_text('{"version":1}')
    rc, o = run(capsys, "config", "validate", str(p)); assert rc == 2 and "problems" in o.out
