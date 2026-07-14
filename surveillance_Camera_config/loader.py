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
from urllib.parse import quote

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
    """One camera, metadata + (if a secret exists) a ready-to-open stream URL.

    source_type decides how frames are obtained:
      "rtsp"      → stream_url built from ip/user/pwd (Hikvision-style cams).
      "url"       → stream_url is a direct HTTP/RTSP URL (e.g. an MJPEG feed).
      "pano_view" → a flat view carved from a shared 360° source; stream_url is
                    the source URL and yaw/pitch/fov/equi_* describe the view.
    """
    camera_uid: str
    name: str
    zone_id: str
    is_exit_camera: bool
    is_active: bool
    stream_url: Optional[str]           # None when no connection is configured for it
    role: str = DEFAULT_ROLE            # "detect" or "identify"
    match_threshold: Optional[float] = None  # per-camera override of feature_id MATCH_THRESHOLD
    # Detection geometry config (all normalized 0..1 coordinates):
    #   roi      — [x1,y1,x2,y2] include-rect; detections whose box CENTER falls
    #              outside are ignored (None → whole frame).
    #   exclude  — list of [x1,y1,x2,y2] rects; a detection centered inside any
    #              is ignored (foliage, roads, neighbour property…).
    # Shape-gate overrides (None → detector env defaults apply):
    #   min_height_frac — person box must be ≥ this fraction of frame height.
    #   min_aspect      — height/width must be ≥ this (upright-ness).
    roi: Optional[list] = None
    exclude: Optional[list] = None
    min_height_frac: Optional[float] = None
    min_aspect: Optional[float] = None
    source_type: str = "rtsp"           # "rtsp" | "url" | "pano_view"
    # pano_view geometry (only used when source_type == "pano_view")
    pano_group: Optional[str] = None    # parent 360 camera_uid (shared PanoStream)
    yaw: float = 0.0
    pitch: float = 0.0
    fov: float = 90.0
    equi_w: int = 2880
    equi_h: int = 1440

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
    # URL-encode credentials — passwords often contain '@', ':' or '/', which
    # would otherwise corrupt the userinfo/host split and break RTSP auth.
    user_enc = quote(str(username), safe="")
    pass_enc = quote(str(password), safe="")
    return f"rtsp://{user_enc}:{pass_enc}@{ip}:{port}/{path}"


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

    def _role(value):
        if value not in VALID_ROLES:
            print(f"[camera_config] WARNING: unknown role '{value}', "
                  f"defaulting to '{DEFAULT_ROLE}'. Valid: {VALID_ROLES}.")
            return DEFAULT_ROLE
        return value

    cameras: list[Camera] = []
    for entry in meta:
        uid = entry["camera_uid"]
        if active_only and not entry.get("is_active", True):
            continue

        source = entry.get("source", {}) or {}
        sec = secrets.get(uid, {}) or {}

        # ── 360° panoramic source: expand into one camera per carved view ──
        if source.get("type") == "pano":
            url = sec.get("url")
            equi_w = int(source.get("equi_w", 2880))
            equi_h = int(source.get("equi_h", 1440))
            default_fov = float(source.get("fov", 90.0))
            for view in entry.get("views", []):
                suffix = view["suffix"]
                cam = Camera(
                    camera_uid=f"{uid}-{suffix}",
                    name=f"{entry.get('name', uid)} {suffix.title()}",
                    zone_id=view.get("zone_id", entry.get("zone_id", "")),
                    is_exit_camera=bool(view.get("is_exit_camera",
                                                 entry.get("is_exit_camera", False))),
                    is_active=True,
                    stream_url=url,
                    role=_role(view.get("role", entry.get("role", DEFAULT_ROLE))),
                    match_threshold=view.get("match_threshold", entry.get("match_threshold")),
                    source_type="pano_view",
                    pano_group=uid,
                    yaw=float(view.get("yaw", 0.0)),
                    pitch=float(view.get("pitch", 0.0)),
                    fov=float(view.get("fov", default_fov)),
                    equi_w=equi_w,
                    equi_h=equi_h,
                )
                if streamable_only and not cam.streamable:
                    continue
                cameras.append(cam)
            continue

        # ── direct URL source (HTTP/MJPEG feed, or any ready-made URL) ──
        if source.get("type") == "url" or "url" in sec:
            stream_url = sec.get("url")
            source_type = "url"
        # ── default: RTSP built from ip/user/pwd ──
        else:
            stream_url = None
            if sec:
                stream_url = build_stream_url(
                    ip=sec["ip"],
                    username=sec["username"],
                    password=sec["password"],
                    rtsp_path=sec.get("rtsp_path", "Streaming/Channels/101"),
                    stream_type=sec.get("stream_type", default_stream_type),
                    port=sec.get("port", DEFAULT_RTSP_PORT),
                )
            source_type = "rtsp"

        cam = Camera(
            camera_uid=uid,
            name=entry.get("name", uid),
            zone_id=entry.get("zone_id", ""),
            is_exit_camera=bool(entry.get("is_exit_camera", False)),
            is_active=bool(entry.get("is_active", True)),
            stream_url=stream_url,
            role=_role(entry.get("role", DEFAULT_ROLE)),
            match_threshold=entry.get("match_threshold"),
            source_type=source_type,
            roi=entry.get("roi"),
            exclude=entry.get("exclude"),
            min_height_frac=entry.get("min_height_frac"),
            min_aspect=entry.get("min_aspect"),
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
