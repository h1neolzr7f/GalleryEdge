"""Deterministic synthetic work generator (site-pack-v1 style JSONL)."""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone

ADJ = ["quiet", "neon", "paper", "amber", "frozen", "silver", "tiny", "lunar"]
NOUN = ["harbor", "garden", "station", "lantern", "forest", "library", "rooftop", "meadow"]
TAGS = ["landscape", "still-life", "abstract", "sketch", "pixel", "watercolor", "night", "day", "city", "sea"]
LICENSES = ["CC0-1.0", "CC-BY-4.0", "synthetic"]


def make_work(i: int, rng: random.Random, base: datetime) -> dict:
    title = f"{rng.choice(ADJ)} {rng.choice(NOUN)} #{i}"
    wid = f"demo-{i:06d}"
    tags = sorted(rng.sample(TAGS, rng.randint(1, 4)))
    return {
        "id": wid,
        "title": title,
        "source_url": f"https://demo-source.invalid/works/{wid}",
        "license": rng.choice(LICENSES),
        "rights_note": "synthetic demo record",
        "rating": "general",
        "artist": f"demo-artist-{rng.randint(1, 20):02d}",
        "tags": tags,
        "prompt": ", ".join([title.split(" #")[0], *tags, "synthetic demo"]),
        "image_count": rng.randint(1, 4),
        "cover_key": f"demo/{wid}/cover.png",
        "created_at": (base - timedelta(days=rng.randint(0, 365))).isoformat(),
        "retrieved_at": base.isoformat(),
        "pack_id": f"demo-pack-{i // 100:04d}",
        "content_flags": [],
        "synthetic": True,
    }


def generate(n: int, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [make_work(i, rng, base) for i in range(n)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate synthetic GalleryEdge works (JSONL)")
    ap.add_argument("-n", type=int, default=100)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("-o", "--out", default="-")
    a = ap.parse_args(argv)
    out = sys.stdout if a.out == "-" else open(a.out, "w", encoding="utf-8")
    try:
        for w in generate(a.n, a.seed):
            out.write(json.dumps(w, ensure_ascii=False) + "\n")
    finally:
        if out is not sys.stdout:
            out.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
