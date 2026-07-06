#!/usr/bin/env python3
"""
tools/integration_sim.py — end-to-end integration driver (stands in for Part 1)
================================================================================
Part 1 (Perception) needs real RTSP cameras + a GPU to produce embeddings, which
isn't available on a dev laptop. This tool fabricates a *realistic* stream of
detection payloads (the Part 1 -> Part 2 contract) and POSTs them to the Brain,
so the whole system can be exercised end-to-end:

        integration_sim.py  ──POST /events──▶  Brain (Part 2)  ──WS /live──▶  UI (Part 3)

It models a FIXED CAST of distinct people (default 10). Each person has a stable,
deterministic embedding, so the Brain recognizes them consistently:
  • first sighting  -> auto-enrolled as a Visitor,
  • later sightings -> matched to that same Visitor (NOT a new identity).
Nobody is an Employee (the facility starts with zero enrolled staff — enroll via
the UI / POST /employees). Detections are high-confidence so nothing is spuriously
Unknown; a small rate of genuine low-confidence "Unknown" blips is opt-in.

Cameras come from the shared registry (surveillance_Camera_config), so the UIDs
match the Brain's cameras table and the UI grid.

Usage:
    python tools/integration_sim.py                 # 10 people, loop ~1 event / 2.5s
    python tools/integration_sim.py --people 10 --once
    python tools/integration_sim.py --unknown-rate 0.05   # 5% genuine Unknowns
    python tools/integration_sim.py --url http://localhost:8000 --interval 2

Requires: pip install requests
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

EMBEDDING_DIMENSIONS = 512

# A small, fixed cast of visitors. Names are cosmetic (the Brain doesn't name
# auto-enrolled visitors) — the stable `seed` is what drives recognition.
CAST = [
    "Aarav", "Diya", "Kabir", "Meera", "Rohan",
    "Sara", "Vikram", "Ananya", "Farid", "Leela",
    "Nikhil", "Priya", "Arjun", "Zoya", "Ishaan",
]


def _load_camera_uids() -> list[str]:
    """Canonical camera UIDs from the shared registry, so the Brain accepts them
    and the UI grid tiles match. Falls back to the known site UIDs."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from surveillance_Camera_config.loader import load_cameras
        uids = [c.camera_uid for c in load_cameras(active_only=True, streamable_only=True)]
        if uids:
            return uids
    except Exception:  # noqa: BLE001
        pass
    return ["GATE-RIGHT", "GATE-INSIDE-LEFT", "GATE-OUTSIDE-LEFT",
            "PATHWAY-FRONT", "CAVILAND-FRONT", "SANJEEVAN-INSIDE-FRONT", "SANJEEVAN-INSIDE"]


def synthetic_embedding(seed: str, dim: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """Deterministic L2-normalized vector — the same `seed` always reproduces the
    same vector, so a person is recognized as themselves across sightings."""
    floats: list[float] = []
    counter = 0
    while len(floats) < dim:
        h = hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        for i in range(0, len(h), 4):
            floats.append(int.from_bytes(h[i:i + 4], "big", signed=False) / 2**32 - 0.5)
            if len(floats) >= dim:
                break
        counter += 1
    vec = floats[:dim]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def build_payload(camera: str, person_seed: str, conf: float) -> dict:
    return {
        "detection_id": f"det-{person_seed}-{datetime.now(timezone.utc).strftime('%H%M%S%f')}",
        "camera_id": camera,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "detection_conf": round(conf, 3),
        "face_embedding": synthetic_embedding(person_seed + ":face"),
        "body_embedding": synthetic_embedding(person_seed + ":body"),
        "snapshot_path": f"storage/img/{person_seed}.jpg",
        "clip_path": f"storage/vid/{person_seed}.mp4",
    }


def emit(session, url: str, payload: dict) -> None:
    try:
        resp = session.post(f"{url}/events", json=payload, timeout=10)
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        who = body.get("name") or body.get("person_id") or "—"
        dup = " (dup)" if body.get("duplicate") else ""
        print(f"  {resp.status_code}  {payload['camera_id']:<22} conf={payload['detection_conf']:.2f}"
              f"  -> {body.get('label','?'):<8} {who}{dup}")
    except Exception as e:  # noqa: BLE001
        print(f"  ERR  {payload['camera_id']}: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Stream realistic mock detections into the Brain (Part 1 stand-in).")
    ap.add_argument("--url", default="http://localhost:8000", help="Brain base URL")
    ap.add_argument("--interval", type=float, default=2.5, help="seconds between detections")
    ap.add_argument("--people", type=int, default=10, help="size of the fixed visitor cast")
    ap.add_argument("--unknown-rate", type=float, default=0.0,
                    help="fraction of detections that are genuine low-confidence Unknowns (0.0-1.0)")
    ap.add_argument("--once", action="store_true", help="send one detection per cast member, then exit")
    ap.add_argument("--seed", type=int, default=7, help="RNG seed for reproducible runs")
    args = ap.parse_args()

    try:
        import requests
    except ImportError:
        print("This tool needs `requests`:  pip install requests", file=sys.stderr)
        sys.exit(1)

    n = max(1, min(args.people, len(CAST)))
    cast = [f"visitor-{CAST[i].lower()}" for i in range(n)]
    cameras = _load_camera_uids()
    rng = random.Random(args.seed)
    session = requests.Session()

    try:
        h = session.get(f"{args.url}/health", timeout=5).json()
        print(f"Brain @ {args.url}  db={h.get('database')} redis={h.get('redis')} qdrant={h.get('qdrant')}")
    except Exception as e:  # noqa: BLE001
        print(f"Cannot reach the Brain at {args.url}/health — is it running?\n  {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Cast: {n} distinct people across {len(cameras)} cameras "
          f"(unknown-rate={args.unknown_rate}). Ctrl-C to stop.\n")

    import time

    def send_one(person_seed: str) -> None:
        # Occasionally emit a genuine Unknown: a brand-new face at low confidence
        # (below the Brain's 0.80 gate) so it stays Unknown without enrolling.
        if rng.random() < args.unknown_rate:
            emit(session, args.url, build_payload(rng.choice(cameras),
                 f"stranger-{rng.randint(1, 9999):04d}", rng.uniform(0.45, 0.70)))
            return
        emit(session, args.url, build_payload(rng.choice(cameras), person_seed, rng.uniform(0.88, 0.97)))

    try:
        if args.once:
            for seed in cast:
                send_one(seed)
            return
        while True:
            send_one(rng.choice(cast))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
