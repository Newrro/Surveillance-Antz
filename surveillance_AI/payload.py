"""payload.py — timestamp, Brain-response label mapping, and the Part1->Part2
   detection payload builder. Extracted from pipeline.py."""

import time
from datetime import datetime, timezone

from ppl_colors import _COL_EMP, _COL_UNKNOWN, _COL_VIS

def _display_from_brain(j):
    """Build a box label from the Brain's POST /events response (authoritative,
    matches the database). Returns (text, color)."""
    label = (j.get("label") or "Unknown")
    name = j.get("name")
    pid = j.get("person_id")
    if label == "Employee":
        return (f"Employee {name or pid or ''}".strip(), _COL_EMP)
    if label == "Visitor":
        return (f"Visitor {pid or ''}".strip(), _COL_VIS)
    return ("Unknown", _COL_UNKNOWN)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


# Per-run epoch baked into every detection_id. Tracker ids restart from t1 on
# every pipeline launch, so without this a fresh run's "CAM-t3" would collide
# with the previous run's "CAM-t3" still cached by the Brain's track-sticky map
# (TTL minutes) — a different person could inherit the old identity.
_RUN_EPOCH = int(time.time()) % 1_000_000


def build_payload(camera_uid, score, face_embedding, body_embedding, snapshot_path, stamp_ms, idx):
    """Part 1 → Part 2 detection payload (contracts/part1_to_part2.event.schema.json).

    `idx` is the tracker's track id. detection_id is STABLE per track (no
    timestamp) so every emit of the same person shares one id — the Brain can
    dedup repeat Unknown sightings on it, group them into one 'Unknown person'
    card, and keep a track's identity sticky across re-emits."""
    return {
        "detection_id": f"{camera_uid}-r{_RUN_EPOCH}-t{idx}",
        "camera_id": camera_uid,
        "timestamp": utc_now_iso(),
        "detection_conf": round(float(score), 4),
        "face_embedding": [float(x) for x in face_embedding] if face_embedding is not None else None,
        "body_embedding": [float(x) for x in body_embedding] if body_embedding is not None else None,
        "snapshot_path": snapshot_path,
        "clip_path": None,                            # short-clip capture: TODO
    }
