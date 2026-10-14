import copy
import json
from pathlib import Path

import pytest
from galleryedge_ops import config

EX = json.loads((Path(__file__).parents[1] / "examples/config.example.json").read_text())


def test_example_valid():
    config.validate(copy.deepcopy(EX))


@pytest.mark.parametrize("mutate,needle", [
    (lambda c: c.update(version=2), "version"),
    (lambda c: c["panel"].update(bind="0.0.0.0"), "panel/bind"),
    (lambda c: c["backends"].append({"id": "local-hot", "type": "local", "path": "x"}), "duplicate"),
    (lambda c: c["backends"].append({"id": "h", "type": "http"}), "needs url"),
    (lambda c: c["database"].update(auth_token="FAKE_inline"), "auth_token"),
    (lambda c: c["alerts"].update(webhook_url_ref="https://x"), "webhook_url_ref"),
    (lambda c: c.update(unknown=1), "unknown"),
])
def test_rejections_explain_field(mutate, needle):
    c = copy.deepcopy(EX)
    mutate(c)
    with pytest.raises(config.ConfigError) as e:
        config.validate(c)
    assert any(needle in p for p in e.value.problems), e.value.problems


def test_remote_bind_allowed_only_when_opted_in():
    c = copy.deepcopy(EX)
    c["panel"].update(bind="0.0.0.0", allow_remote=True)
    config.validate(c)


def test_load_resolves_relative_paths(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(EX))
    cfg = config.load(p)
    assert cfg["backends"][0]["path"] == str(tmp_path / "var/storage/hot")
    assert cfg["database"]["primary"] == "file:" + str(tmp_path / "var/panel.db")
