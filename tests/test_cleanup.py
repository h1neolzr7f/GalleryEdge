import hashlib
import json

from galleryedge_ops import cleanup

LEDGERS = ["done.txt", "seen.txt", "packed_works.txt", "prompts.jsonl"]


def build(root):
    v = root / "pack-0001"; (v / "images").mkdir(parents=True); (v / "thumbs").mkdir()
    (v / ".verified_remote").write_text("ok")
    (v / "images" / "a.png").write_bytes(b"img"); (v / "thumbs" / "a.webp").write_bytes(b"th")
    (v / "manifest.json").write_text("{}")
    for l in LEDGERS:
        (v / l).write_text("ledger")
        (root / l).write_text("ledger")
    u = root / "pack-0002" / "images"; u.mkdir(parents=True); (u / "b.png").write_bytes(b"x")  # not verified
    s = root / "staging" / "pack-0003"; s.mkdir(parents=True); (s / "c.png").write_bytes(b"x")
    ip = root / "pack-0004"; ip.mkdir(); (ip / ".verified_remote").write_text(""); (ip / ".inprogress").write_text("")
    (ip / "d.png").write_bytes(b"x")
    up = root / "up.tar"; up.write_bytes(b"tar-up")
    (root / "up.tar.uploaded").write_text(json.dumps({"size": 6, "sha256": hashlib.sha256(b"tar-up").hexdigest()}))
    (root / "pending.tar").write_bytes(b"tar-pending")
    bad = root / "bad.tar"; bad.write_bytes(b"changed!")
    (root / "bad.tar.uploaded").write_text(json.dumps({"size": 6, "sha256": "0" * 64}))
    (root / "creds.vault").write_text("enc")
    return root


def names(paths):
    return sorted(str(p.relative_to(p.parents[len(p.parts) - 1])) for p in paths)


def test_plan_only_deletes_confirmed_uploaded(tmp_path):
    root = build(tmp_path / "w")
    pl = cleanup.plan([root])
    rel = sorted(str(p.relative_to(root)) for p in pl.delete)
    assert rel == ["pack-0001/images/a.png", "pack-0001/thumbs/a.webp", "up.tar"]
    why = {str(p.relative_to(root)): w for p, w in pl.protected}
    for l in LEDGERS:
        assert why[l] == "ledger" and why[f"pack-0001/{l}"] == "ledger"
    assert why["pending.tar"] == "tar not uploaded"
    assert why["bad.tar"] == "tar upload marker mismatch"
    assert why["staging/pack-0003/c.png"] == "in-progress staging"
    assert why["pack-0004/d.png"] == "in-progress marker"
    assert why["pack-0002/images/b.png"] == "pack not verified remote"
    assert why["creds.vault"].startswith("protected")


def test_dry_run_deletes_nothing(tmp_path):
    root = build(tmp_path / "w")
    before = sorted(p for p in root.rglob("*"))
    pl = cleanup.apply([root])  # default dry-run
    assert pl.delete and sorted(p for p in root.rglob("*")) == before


def test_apply_keeps_everything_protected(tmp_path):
    root = build(tmp_path / "w")
    deleted = []
    cleanup.apply([root], dry_run=False, on_delete=deleted.append)
    assert len(deleted) == 3
    for l in LEDGERS:
        assert (root / l).exists() and (root / "pack-0001" / l).exists()
    for p in ["pending.tar", "bad.tar", "staging/pack-0003/c.png", "pack-0004/d.png", "pack-0002/images/b.png",
              "pack-0001/manifest.json", "pack-0001/.verified_remote", "creds.vault"]:
        assert (root / p).exists(), p
    assert not (root / "pack-0001/images").exists()  # empty dir pruned
    assert (root / "staging").exists()


def test_symlink_and_outside_root_protected(tmp_path):
    root = tmp_path / "w"; v = root / "p"; v.mkdir(parents=True); (v / ".verified_remote").write_text("")
    outside = tmp_path / "precious.txt"; outside.write_text("keep")
    (v / "link.png").symlink_to(outside)
    cleanup.apply([root], dry_run=False)
    assert outside.read_text() == "keep"


def test_extra_protected_and_recheck(tmp_path):
    root = tmp_path / "w"; v = root / "p"; v.mkdir(parents=True); (v / ".verified_remote").write_text("")
    (v / "keep.bin").write_bytes(b"1"); (v / "x.png").write_bytes(b"1")
    assert [p.name for p in cleanup.plan([root], ["*.bin"]).delete] == ["x.png"]
    # an .inprogress marker appearing between plan and delete must stop deletion
    orig = cleanup.plan
    def racing(*a, **k):
        pl = orig(*a, **k); (v / ".inprogress").write_text(""); return pl
    cleanup.plan = racing
    try:
        assert cleanup.apply([root], dry_run=False).delete == []
    finally:
        cleanup.plan = orig
    assert (v / "x.png").exists()
