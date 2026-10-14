import json
from galleryedge_demo.generate import generate, main


def test_deterministic_and_required_fields():
    a, b = generate(50, seed=7), generate(50, seed=7)
    assert a == b
    for w in a:
        assert w["synthetic"] is True
        assert w["source_url"].startswith("https://demo-source.invalid/")
        assert w["license"] and w["id"].startswith("demo-")
    assert len({w["id"] for w in a}) == 50


def test_cli_writes_jsonl(tmp_path):
    p = tmp_path / "w.jsonl"
    assert main(["-n", "5", "-o", str(p)]) == 0
    lines = p.read_text().splitlines()
    assert len(lines) == 5 and json.loads(lines[0])["id"] == "demo-000000"
