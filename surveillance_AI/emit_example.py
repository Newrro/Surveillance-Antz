"""
emit_example.py  (Part 1 — SKELETON / mock producer)
=====================================================
A stand-in for the real Perception pipeline.  It fabricates a detection
payload matching the Part 1 -> Part 2 contract and POSTs it to the Brain,
so the Brain (Part 2) and UI (Part 3) can be exercised end-to-end before
the real AI model exists.

The real pipeline will replace the synthetic embeddings with model output
(ArcFace face embedding + OSNet body ReID embedding) and the fixed camera
id with the actual source camera.

Usage:
    python emit_example.py --url http://localhost:8000 --camera GATE-01

NOTE: This is intentionally minimal — Part 1's real implementation lives
elsewhere.  Requires: pip install requests
"""

from __future__ import annotations

import argparse
import hashlib
import math
from datetime import datetime, timezone

EMBEDDING_DIMENSIONS = 512


def synthetic_embedding(seed: str, dim: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """Deterministic L2-normalized vector (placeholder for a real model)."""
    floats: list[float] = []
    counter = 0
    while len(floats) < dim:
        h = hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        for i in range(0, len(h), 4):
            floats.append(int.from_bytes(h[i:i + 4], "big") / 2**32 - 0.5)
            if len(floats) >= dim:
                break
        counter += 1
    norm = math.sqrt(sum(v * v for v in floats)) or 1.0
    return [v / norm for v in floats]


def build_payload(camera: str, person_seed: str) -> dict:
    return {
        "detection_id": f"det-{person_seed}",
        "camera_id": camera,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "detection_conf": 0.93,
        "face_embedding": synthetic_embedding(person_seed + ":face"),
        "body_embedding": synthetic_embedding(person_seed + ":body"),
        "snapshot_path": f"storage/img/{person_seed}.jpg",
        "clip_path": f"storage/vid/{person_seed}.mp4",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--camera", default="GATE-01")
    ap.add_argument("--person", default="person-001")
    args = ap.parse_args()

    import requests  # local import so the file imports without the dep

    payload = build_payload(args.camera, args.person)
    resp = requests.post(f"{args.url}/events", json=payload, timeout=10)
    print(resp.status_code, resp.json())


if __name__ == "__main__":
    main()
