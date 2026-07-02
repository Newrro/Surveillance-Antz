# ─────────────────────────────────────────────
#  CAMERA REGISTRY LOADER  — shared by Part 1 (Perception) and Part 2 (Brain)
# ─────────────────────────────────────────────
# The camera registry is split into two files so credentials never reach git:
#
#   cameras.json          committed   uid / name / zone / is_exit / is_active
#                                      (the shape the Brain's `cameras` table stores)
#   cameras.secrets.json  gitignored  per-uid ip / username / password / rtsp_path
#                                      (copy cameras.secrets.example.json and fill in)
#
# load_cameras() JOINS the two on `camera_uid` and builds the full RTSP
# stream_url at runtime, so:
#   • Part 1 gets {camera_uid, stream_url, label} to connect + tag detections.
#   • Part 2 gets Brain-shaped records (via to_brain_records) to seed its table.
#
# A camera with metadata but no matching secret entry loads with stream_url=None
# (it stays in the registry for the Brain, but Part 1 can't stream it).

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
META_PATH = os.path.join(HERE, "cameras.json")
SECRETS_PATH = os.path.join(HERE, "cameras.secrets.json")

# RTSP control port for the Hikvision-OEM cameras. Overridable per-secret entry.
DEFAULT_RTSP_PORT = 554

# Per-camera role — decides how much of the pipeline runs on it:
#   "detect"   → person DETECTION only (FasterRCNN). Cheap. For path/perimeter cams.
#   "identify" → detect + SAM 2 segment + OSNet ReID + identity (Employee/Visitor/
#                Unknown + confidence). For the high-res gate/back cams.
VALID_ROLES = ("detect", "identify")
DEFAULT_ROLE = "detect"


@dataclass
class Camera:
    """One camera, metadata + (if a secret exists) a ready-to-open RTSP URL."""
    camera_uid: str
    name: str
    zone_id: str
    is_exit_camera: bool
    is_active: bool
    stream_url: Optional[str]           # None when no credentials are configured for it
    role: str = DEFAULT_ROLE            # "detect" or "identify"
    match_threshold: Optional[float] = None  # per-camera override of feature_id MATCH_THRESHOLD

    @property
    def label(self) -> str:
        # `label` is what the livestream viewer/overlay code expects.
        return self.name

    @property
    def streamable(self) -> bool:
        return bool(self.stream_url)

    @property
    def runs_identity(self) -> bool:
        """True → run the full feature-extraction + identity path on this camera."""
        return self.role == "identify"


def build_stream_url(ip, username, password, rtsp_path, stream_type="sub",
                     port=DEFAULT_RTSP_PORT) -> str:
    """Build the RTSP URL, switching to the lighter sub-stream when asked.
    Hikvision-style paths end in '01' (main, full quality) / '02' (sub)."""
    path = rtsp_path
    if stream_type == "sub" and path.endswith("01"):
        path = path[:-2] + "02"
    return f"rtsp://{username}:{password}@{ip}:{port}/{path}"


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_cameras(active_only: bool = True, streamable_only: bool = False) -> list[Camera]:
    """Return the joined camera registry.

    active_only     — drop cameras with is_active=false (default True).
    streamable_only — drop cameras with no credentials configured (stream_url=None).
    """
    meta = _load_json(META_PATH).get("cameras", [])

    secrets_wrapper = {}
    if os.path.exists(SECRETS_PATH):
        secrets_wrapper = _load_json(SECRETS_PATH)
    else:
        print(f"[camera_config] WARNING: {os.path.basename(SECRETS_PATH)} not found — "
              "no cameras will be streamable. Copy cameras.secrets.example.json and fill it in.")

    default_stream_type = secrets_wrapper.get("stream_type", "sub")
    secrets = secrets_wrapper.get("cameras", {})

    cameras: list[Camera] = []
    for entry in meta:
        uid = entry["camera_uid"]
        if active_only and not entry.get("is_active", True):
            continue

        stream_url = None
        sec = secrets.get(uid)
        if sec:
            stream_url = build_stream_url(
                ip=sec["ip"],
                username=sec["username"],
                password=sec["password"],
                rtsp_path=sec.get("rtsp_path", "Streaming/Channels/101"),
                stream_type=sec.get("stream_type", default_stream_type),
                port=sec.get("port", DEFAULT_RTSP_PORT),
            )

        role = entry.get("role", DEFAULT_ROLE)
        if role not in VALID_ROLES:
            print(f"[camera_config] WARNING: {uid} has unknown role '{role}', "
                  f"defaulting to '{DEFAULT_ROLE}'. Valid: {VALID_ROLES}.")
            role = DEFAULT_ROLE

        cam = Camera(
            camera_uid=uid,
            name=entry.get("name", uid),
            zone_id=entry.get("zone_id", ""),
            is_exit_camera=bool(entry.get("is_exit_camera", False)),
            is_active=bool(entry.get("is_active", True)),
            stream_url=stream_url,
            role=role,
            match_threshold=entry.get("match_threshold"),
        )
        if streamable_only and not cam.streamable:
            continue
        cameras.append(cam)

    return cameras


def to_brain_records(cameras: list[Camera]) -> list[dict]:
    """Project Camera objects into the exact shape Part 2's `cameras` table seeds
    (see contracts / surveillance_brain/scripts/seed.py)."""
    return [
        {
            "camera_uid": c.camera_uid,
            "name": c.name,
            "zone_id": c.zone_id,
            "is_exit_camera": c.is_exit_camera,
            "stream_url": c.stream_url,
            "is_active": c.is_active,
        }
        for c in cameras
    ]


# ── quick check: `python -m surveillance_Camera_config.loader` ──
if __name__ == "__main__":
    cams = load_cameras(active_only=False)
    print(f"{len(cams)} cameras in registry:")
    for c in cams:
        state = "stream OK" if c.streamable else "NO CREDS"
        exit_flag = " [EXIT]" if c.is_exit_camera else ""
        thr = f" thr={c.match_threshold}" if c.match_threshold is not None else ""
        print(f"  {c.camera_uid:22} {c.zone_id:16} {c.role:9} {state:9}{exit_flag}{thr}  {c.name}")
