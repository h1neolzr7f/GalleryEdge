import pytest
from galleryedge_ops import packs


def test_state_machine(conn):
    packs.upsert(conn, "p1")
    assert packs.advance(conn, "p1", progress=0.5)["status"] == "running"
    p = packs.advance(conn, "p1")
    assert p["stage"] == "verify" and p["progress"] == 0
    p = packs.advance(conn, "p1", error="boom")
    assert p["status"] == "error" and p["stage"] == "verify"
    with pytest.raises(packs.PackError):
        packs.pause(conn, "p1")
    p = packs.retry(conn, "p1")
    assert (p["status"], p["stage"], p["attempts"], p["error"]) == ("pending", "verify", 1, None)
    with pytest.raises(packs.PackError):
        packs.retry(conn, "p1")
    for _ in range(len(packs.STAGES) - 2):
        p = packs.advance(conn, "p1")
    assert p["status"] == "done" and p["stage"] == "done"
    with pytest.raises(packs.PackError):
        packs.advance(conn, "p1")
    assert conn.execute("SELECT action FROM audit").fetchall() == [("pack.retry",)]


def test_pause_resume_skip(conn):
    packs.upsert(conn, "p2", "upload", "running")
    assert packs.pause(conn, "p2")["status"] == "paused"
    with pytest.raises(packs.PackError):
        packs.advance(conn, "p2")
    assert packs.resume(conn, "p2")["status"] == "pending"
    with pytest.raises(packs.PackError):
        packs.skip(conn, "p2", confirm=False)
    assert packs.skip(conn, "p2", confirm=True)["status"] == "skipped"
    assert packs.summary(conn) == {"skipped": 1}
    with pytest.raises(packs.PackError):
        packs.upsert(conn, "p3", "nope")
    with pytest.raises(packs.PackError):
        packs.retry(conn, "missing")
