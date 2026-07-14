"""
pipeline.py — the real Part 1 (Perception) producer, role-aware.

Each camera has a role (set in surveillance_Camera_config/cameras.json):

  role="detect"    → FasterRCNN person DETECTION only. Cheap. Path/perimeter cams.
                     Logs person counts; emits nothing (no embedding to emit).
  role="identify"  → DETECT → (optional SAM 2 SEGMENT) → crop → OSNet body ReID →
                     IDENTITY (Employee / Visitor / Unknown + confidence %), using
                     the local feature_id gallery with progressive-confidence
                     learning. High-res gate/back cams for feature extraction.

WHY the split: you don't want to run the heavy feature-extraction model on every
path camera — only where you actually recognise people. Detection is cheap and
runs everywhere; identity runs only on identify-cameras.

MODELS (no YOLO):
  • detection   → FasterRCNN-MobileNetV3   (detector.py)
  • segmentation→ SAM 2                     (segmenter.py, optional --segment)
  • body ReID   → OSNet (torchreid)         (feature_id/extractor.py)

THRESHOLDS:
  • per-camera match_threshold in cameras.json overrides the global
    feature_id MATCH_THRESHOLD — raise it on a camera to make it stricter.
  • progressive confidence: when a known person matches above threshold but below
    LEARN_CEILING, the new view is stored, so future sightings score higher.

Usage:
    python pipeline.py                       # run all cameras, local ID, print (no POST)
    python pipeline.py --segment             # + SAM 2 background removal before ReID
    python pipeline.py --show                # + annotated window
    python pipeline.py --cameras GATE-RIGHT  # just one camera
    python pipeline.py --emit --brain-url http://localhost:8000   # also POST to the Brain

Enroll employees first so they're labelled 'Employee' (else everyone is an
auto-enrolled Visitor):
    python -m feature_id.enroll  EMP-001  "Asha R."  path/to/photo.jpg
"""
import collections
import json
import os
import sys
import time
import argparse
import threading
from datetime import datetime, timezone

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import cv2
import numpy as np
import torch
from contextlib import nullcontext

# flat sibling modules
from detector import PersonDetector, draw_boxes
from feature_id.identify import Identifier
from tracker import SimpleTracker, MotionTracker

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
from surveillance_Camera_config import load_cameras  # noqa: E402

import nvr_stream as nvr  # noqa: E402

STORAGE_IMG = os.path.join(REPO_ROOT, "storage", "img")
MIN_CROP_SIDE = 24  # skip crops too tiny to embed reliably

# ── Live annotated preview (REAL boxes on the dashboard) ───────────────────
# The pipeline is the single camera consumer: it decodes, detects and identifies,
# then draws the REAL boxes on the frame and writes a small JPEG per camera to
# shared memory. The UI bridge (surveillance_UI/server.py) serves those JPEGs as
# MJPEG — so the dashboard shows the actual detector output (not fabricated
# boxes), and the UI no longer decodes RTSP itself (frees the GPU).
SHM_DIR = os.environ.get("SENTINEL_SHM", "/dev/shm/sentinel")
PREVIEW_FPS = float(os.environ.get("PREVIEW_FPS", "12"))
PREVIEW_W = int(os.environ.get("PREVIEW_W", "1280"))
PREVIEW_H = int(os.environ.get("PREVIEW_H", "720"))
PREVIEW_QUALITY = int(os.environ.get("PREVIEW_QUALITY", "85"))

# ── Decoupled detection resolution ─────────────────────────────────────────
# Frames now arrive at NATIVE resolution (nvr_stream) so face/body crops keep
# their pixels. Detection doesn't need that: RT-DETR resizes to a fixed input
# internally, so we run it on a cheap downscaled COPY (longest side capped here)
# and scale the boxes back to full-res coords. Crops are then taken from the
# full-res frame. Bigger = catches smaller/distant people; smaller = less lag.
DETECT_MAX_SIDE = int(os.environ.get("DETECT_MAX_SIDE", "960"))

# ── Full-scene verification snapshot ───────────────────────────────────────
# Beside the face + body crops we save the whole frame (box drawn) so the UI can
# show the crops IN CONTEXT — you can eyeball whether they came from the right
# person. Downscaled + moderate quality: this is for human review, not features.
FULL_FRAME_MAX_W = int(os.environ.get("FULL_FRAME_MAX_W", "1280"))
FULL_FRAME_QUALITY = int(os.environ.get("FULL_FRAME_QUALITY", "88"))
# The ORIGINAL full frame in each evidence set is saved UNTOUCHED at native
# resolution (only JPEG-compressed at this quality) — raw evidence survives
# even though the preview/annotated copies are downscaled.
FULL_FRAME_ORIG_QUALITY = int(os.environ.get("FULL_FRAME_ORIG_QUALITY", "92"))
# Skip detection on a camera whose newest decoded frame is older than this —
# a frozen decode must not burn GPU batches on the same stale image.
STALE_FRAME_MAX_S = float(os.environ.get("STALE_FRAME_MAX_S", "2.0"))

# ── Identity as a lagged background service ────────────────────────────────
# The grid (live detection boxes) is the real-time FOREGROUND; identity is a
# throttled BACKGROUND worker that is allowed to lag a few seconds. These knobs
# keep the heavy face/body/SAM2 work from ever starving the detector that feeds
# the grid — see the identity_worker below.
IDENTITY_MIN_HITS = int(os.environ.get("IDENTITY_MIN_HITS", "3"))                 # only resolve stable tracks
IDENTITY_MAX_RATE = float(os.environ.get("IDENTITY_MAX_RATE", "4"))               # max resolves/sec (yields GPU)
IDENTITY_LATENCY_BUDGET = float(os.environ.get("IDENTITY_LATENCY_BUDGET", "4.0")) # acceptable label lag (s)

# ── Best-shot tracklet identity (Part 2) ──────────────────────────────────
# Instead of resolving a track off its first (often blurry / side-on) frame, we
# probe it a few times, keep the HIGHEST-quality face (AdaFace feature-norm), and
# resolve from that best shot. A person's best face re-matches ONE gallery entry
# far more reliably → the same person stops splitting into many Visitors.
IDENTITY_MAX_PROBES = int(os.environ.get("IDENTITY_MAX_PROBES", "8"))      # frames pooled (upper bound / body-only fallback)
IDENTITY_MIN_EMIT_PROBES = int(os.environ.get("IDENTITY_MIN_EMIT_PROBES", "3"))  # emit a first label after this many face probes
IDENTITY_REEMIT_FRAC = float(os.environ.get("IDENTITY_REEMIT_FRAC", "0.15"))  # re-emit if a face is this much better

# ── Face quality gate (IDENTITY_REDESIGN.md Phase A) ───────────────────────
# Precision-first: only faces above this quality (AdaFace-norm × sharpness proxy)
# feed identity. A low-quality face contributes NOTHING to the template, so a
# track with only bad faces stays faceless → Unknown, rather than enrolling a
# garbage vector that later false-matches. Biggest single precision lever.
# 24 (was 18): live-verified 2026-07-14 — a non-face crop (white fabric blob)
# scored q23, passed the old gate, matched a gallery at 0.47 and was learned,
# contaminating the identity. Photo-verified real faces this deployment score
# q24-42, so 24 is the empirical floor separating faces from garbage.
FACE_MIN_QUALITY = float(os.environ.get("FACE_MIN_QUALITY", "24.0"))
# Skip identity on a track whose box is heavily overlapped by ANOTHER person's box
# (the crop would contain a neighbour → wrong face). Fraction of THIS box covered.
OCCLUSION_OVERLAP = float(os.environ.get("OCCLUSION_OVERLAP", "0.45"))


def _stream_ctx(s):
    """Run the enclosed GPU ops on CUDA stream `s` (or a no-op on CPU)."""
    return torch.cuda.stream(s) if s is not None else nullcontext()

_annot = {}                 # camera_uid -> list of (x1, y1, x2, y2, label, color)
_annot_lock = threading.Lock()


def set_annotations(uid, annos):
    with _annot_lock:
        _annot[uid] = annos


def get_annotations(uid):
    with _annot_lock:
        return list(_annot.get(uid, ()))


def _preview_loop(s, period, enc):
    """Per-camera preview loop. Resize the frame to tile size FIRST, then scale
    the (full-res) detection boxes down and draw them on the small frame — so we
    never copy/annotate an 11 MB 1440p frame. One of these runs per camera, so
    a slow encode on one tile can't stall the others.

    Side outputs per tick (all atomic writes):
      {cam}.jpg          annotated preview tile (the grid video)
      {cam}.tracks.json  REAL live-track metadata (normalized boxes + labels) —
                         the dashboard's only source of boxes; nothing is faked
      clip ring          CLEAN (un-annotated) frames feed the clip pre-roll
    """
    tmp = os.path.join(SHM_DIR, f".{s.camera_uid}.tmp")
    final = os.path.join(SHM_DIR, f"{s.camera_uid}.jpg")
    tracks_tmp = os.path.join(SHM_DIR, f".{s.camera_uid}.tracks.tmp")
    tracks_final = os.path.join(SHM_DIR, f"{s.camera_uid}.tracks.json")
    while True:
        t0 = time.time()
        frame = s.get_frame()
        if frame is not None:
            h, w = frame.shape[:2]
            small = cv2.resize(frame, (PREVIEW_W, PREVIEW_H))
            _clips.feed(s.camera_uid, small)          # clean frame → clip pre-roll
            sx, sy = PREVIEW_W / max(1, w), PREVIEW_H / max(1, h)
            annos = get_annotations(s.camera_uid)
            for (x1, y1, x2, y2, label, color) in annos:
                p1 = (int(x1 * sx), int(y1 * sy))
                p2 = (int(x2 * sx), int(y2 * sy))
                cv2.rectangle(small, p1, p2, color, 2)
                if label:
                    cv2.putText(small, label, (p1[0], max(16, p1[1] - 6)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
            ok, buf = cv2.imencode(".jpg", small, enc)
            if ok:
                try:
                    with open(tmp, "wb") as f:
                        f.write(buf.tobytes())
                    os.replace(tmp, final)   # atomic swap so readers never see a half-written file
                except OSError:
                    pass
            # Real track metadata for the dashboard (normalized coordinates).
            try:
                payload = {"ts": t0, "w": w, "h": h, "tracks": [
                    {"box": [round(x1 / w, 4), round(y1 / h, 4),
                             round(x2 / w, 4), round(y2 / h, 4)],
                     "label": label or "person"}
                    for (x1, y1, x2, y2, label, _c) in annos
                ]}
                with open(tracks_tmp, "w") as f:
                    json.dump(payload, f)
                os.replace(tracks_tmp, tracks_final)
            except OSError:
                pass
        dt = time.time() - t0
        if dt < period:
            time.sleep(period - dt)


def start_preview_writer(streams):
    """One preview thread PER camera: overlay the latest boxes on the latest frame
    and write a JPEG to SHM at PREVIEW_FPS. Display FPS is decoupled from the
    (slower) detection loop, and cameras no longer serialize behind each other."""
    os.makedirs(SHM_DIR, exist_ok=True)
    period = 1.0 / max(1.0, PREVIEW_FPS)
    enc = [int(cv2.IMWRITE_JPEG_QUALITY), PREVIEW_QUALITY]
    threads = []
    for s in streams:
        th = threading.Thread(target=_preview_loop, args=(s, period, enc), daemon=True)
        th.start()
        threads.append(th)
    return threads


# Box colours (BGR) by identity outcome.
_COL_EMP = (0, 200, 0)        # Employee → green
_COL_VIS = (0, 170, 255)      # Visitor → orange
_COL_UNKNOWN = (60, 60, 220)  # Unknown / below gate → red
_COL_PERSON = (200, 200, 200) # detected, not yet identified → grey


def _iou(a, b):
    """IoU of two (x1,y1,x2,y2) boxes."""
    ax1, ay1, ax2, ay2 = a[:4]
    bx1, by1, bx2, by2 = b[:4]
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


def _overlap_frac(a, b):
    """Fraction of box `a` that is covered by box `b` (intersection / area(a))."""
    ax1, ay1, ax2, ay2 = a[:4]
    bx1, by1, bx2, by2 = b[:4]
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    area = max(1.0, (ax2 - ax1) * (ay2 - ay1))
    return inter / area


def _occluded(box, others):
    """True if another person's box covers too much of this one — the crop would
    include a neighbour, so the face we extract might be the wrong person."""
    return any(_overlap_frac(box, o) > OCCLUSION_OVERLAP for o in others)


def _label_for_box(box, id_labels):
    """Pick the identity label whose (older) box best overlaps this fresh box.
    Falls back to a plain grey 'person' marker until identity catches up."""
    best, best_iou = None, 0.30
    for (lbox, disp, color) in id_labels:
        v = _iou(box, lbox)
        if v > best_iou:
            best, best_iou = (disp, color), v
    return best if best else ("person", _COL_PERSON)


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


def crop_person(frame_bgr, box_xyxy, mask=None):
    """Crop the person's bounding box. If a mask is given, blank the background
    (pixels outside the mask → black) so the ReID vector describes the person."""
    h, w = frame_bgr.shape[:2]
    x1, y1, x2, y2 = box_xyxy[:4]
    x1, y1 = max(0, int(x1)), max(0, int(y1))
    x2, y2 = min(w, int(x2)), min(h, int(y2))
    if x2 - x1 < MIN_CROP_SIDE or y2 - y1 < MIN_CROP_SIDE:
        return None
    if mask is not None:
        person = np.zeros_like(frame_bgr)
        person[mask] = frame_bgr[mask]
        return person[y1:y2, x1:x2].copy()
    return frame_bgr[y1:y2, x1:x2].copy()


def _crop_sharpness(crop_bgr):
    """Cheap 'good shot' score for a body crop: variance-of-Laplacian (in-focus
    detail) scaled by the crop's short side. Higher = sharper AND bigger. Lets us
    keep the SHARPEST frame as the snapshot instead of whatever frame identity
    happened to probe first — so a person who walks past doesn't get a motion-
    blurred picture just because the first probe caught them mid-stride."""
    if crop_bgr is None or crop_bgr.size == 0:
        return 0.0
    h, w = crop_bgr.shape[:2]
    g = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    m = max(h, w)
    if m > 256:                              # keep the Laplacian cheap on big crops
        sc = 256.0 / m
        g = cv2.resize(g, (max(1, int(w * sc)), max(1, int(h * sc))), interpolation=cv2.INTER_AREA)
    lap = cv2.Laplacian(g, cv2.CV_64F).var()
    return float(lap) * float(min(h, w))


def _pooled_face(emb_sum, w_sum):
    """Quality-weighted temporal pool → one L2-normalized face vector. Averaging
    several frames' embeddings (weighted by quality) cancels per-frame noise, so
    the same person enrolls/matches ONE stable gallery vector (less fragmentation).
    Returns a plain list (JSON-serializable for the Brain payload), or None."""
    if emb_sum is None or w_sum <= 0:
        return None
    v = np.asarray(emb_sum, dtype=np.float32)
    n = float(np.linalg.norm(v))
    if n <= 0:
        return None
    return (v / n).tolist()


def _atomic_imwrite(path, img_bgr, quality=None):
    """Write a JPEG atomically: encode → temp file → os.replace. A reader can
    never see a half-written file, and an existing file is never mutated
    in-place (evidence sets are immutable once written)."""
    params = [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)] if quality else []
    ok, buf = cv2.imencode(".jpg", img_bgr, params)
    if not ok:
        return False
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(buf.tobytes())
    os.replace(tmp, path)
    return True


def save_evidence_set(camera_uid, stamp_ms, idx, seq,
                      frame_bgr=None, box_xyxy=None,
                      body_bgr=None, face_bgr=None):
    """Write ONE immutable sighting evidence set — every file from the SAME
    captured moment, each written once (atomically), none ever replaced:

        {stem}_body.jpg   body crop exactly as captured
        {stem}_face.jpg   face crop exactly as captured (only when present)
        {stem}_orig.jpg   ORIGINAL full frame, untouched, original resolution
        {stem}_annot.jpg  separate annotated copy (box drawn; downscaled)

    `seq` disambiguates multiple evidence sets of the same track (a later,
    better frame is a NEW sighting — never an overwrite of an earlier one).
    Returns {stem, body_path, face_path, full_frame_path,
    full_frame_annotated_path} with repo-relative paths (None where absent)."""
    out_dir = os.path.join(STORAGE_IMG, camera_uid)
    os.makedirs(out_dir, exist_ok=True)
    stem = f"{stamp_ms}_{idx}_s{seq}"
    rel = f"storage/img/{camera_uid}/{stem}"
    paths = {"stem": stem, "body_path": None, "face_path": None,
             "full_frame_path": None, "full_frame_annotated_path": None}

    if body_bgr is not None and body_bgr.size:
        if _atomic_imwrite(os.path.join(out_dir, f"{stem}_body.jpg"), body_bgr):
            paths["body_path"] = f"{rel}_body.jpg"
    if face_bgr is not None and getattr(face_bgr, "size", 0):
        if _atomic_imwrite(os.path.join(out_dir, f"{stem}_face.jpg"), face_bgr):
            paths["face_path"] = f"{rel}_face.jpg"
    if frame_bgr is not None and frame_bgr.size:
        # ORIGINAL frame: untouched, original resolution — the ground truth.
        if _atomic_imwrite(os.path.join(out_dir, f"{stem}_orig.jpg"),
                           frame_bgr, quality=FULL_FRAME_ORIG_QUALITY):
            paths["full_frame_path"] = f"{rel}_orig.jpg"
        # Annotated copy: SEPARATE file, may be downscaled — never the original.
        if box_xyxy is not None:
            h, w = frame_bgr.shape[:2]
            s = FULL_FRAME_MAX_W / w if w > FULL_FRAME_MAX_W else 1.0
            img = cv2.resize(frame_bgr, (int(w * s), int(h * s))) if s < 1.0 else frame_bgr.copy()
            x1, y1, x2, y2 = (int(v * s) for v in box_xyxy[:4])
            cv2.rectangle(img, (x1, y1), (x2, y2), _COL_VIS, 2)
            if _atomic_imwrite(os.path.join(out_dir, f"{stem}_annot.jpg"),
                               img, quality=FULL_FRAME_QUALITY):
                paths["full_frame_annotated_path"] = f"{rel}_annot.jpg"
    return paths


# ── Short clips: per-camera in-memory ring buffer + background writer ───────
# The preview loop feeds each camera's ring with CLEAN (un-annotated) frames at
# CLIP_FPS. When a sighting triggers a clip, the writer thread snapshots the
# pre-roll from the ring, keeps collecting post-roll, then encodes an MP4
# atomically (tmp → rename) at the path that was already reported to the Brain.
CLIP_ENABLE = os.environ.get("CLIP_ENABLE", "1") not in ("0", "false", "no")
CLIP_FPS = float(os.environ.get("CLIP_FPS", "6"))
CLIP_PRE_S = float(os.environ.get("CLIP_PRE_S", "4"))
CLIP_POST_S = float(os.environ.get("CLIP_POST_S", "4"))
CLIP_MAX_CONCURRENT = int(os.environ.get("CLIP_MAX_CONCURRENT", "3"))
STORAGE_CLIPS = os.path.join(REPO_ROOT, "storage", "clips")


class ClipRecorder:
    """One ring buffer per camera (JPEG-encoded frames — bounded RAM) plus a
    single background writer pool. Non-blocking: trigger() returns the future
    clip path immediately; the file appears atomically when post-roll ends."""

    def __init__(self):
        self._rings = {}          # uid -> deque[(ts, jpg_bytes)]
        self._lock = threading.Lock()
        self._deque = collections.deque
        self._active = threading.Semaphore(CLIP_MAX_CONCURRENT)
        self._keep = max(2.0, CLIP_PRE_S + 1.0)

    def feed(self, uid, frame_bgr):
        """Called from the preview loop at preview rate; keeps ≈CLIP_FPS."""
        now = time.time()
        with self._lock:
            ring = self._rings.get(uid)
            if ring is None:
                ring = self._rings[uid] = self._deque()
            if ring and now - ring[-1][0] < 1.0 / CLIP_FPS:
                return
        ok, buf = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            return
        with self._lock:
            ring.append((now, buf.tobytes()))
            while ring and now - ring[0][0] > self._keep:
                ring.popleft()

    def trigger(self, uid, stem):
        """Start a clip around NOW for camera `uid`. Returns the repo-relative
        path the finished file WILL occupy (atomic rename on completion), or
        None when clips are disabled / the writer pool is saturated."""
        if not CLIP_ENABLE:
            return None
        if not self._active.acquire(blocking=False):
            return None                      # saturated — skip, never block ingest
        os.makedirs(os.path.join(STORAGE_CLIPS, uid), exist_ok=True)
        rel = f"storage/clips/{uid}/{stem}.mp4"
        final = os.path.join(REPO_ROOT, rel)
        with self._lock:
            pre = list(self._rings.get(uid) or ())
        threading.Thread(target=self._write, args=(uid, pre, final),
                         daemon=True, name=f"clip-{uid}").start()
        return rel

    def _write(self, uid, pre, final):
        try:
            t_end = time.time() + CLIP_POST_S
            frames = list(pre)
            seen = {ts for ts, _ in frames}
            while time.time() < t_end:
                time.sleep(1.0 / CLIP_FPS)
                with self._lock:
                    ring = list(self._rings.get(uid) or ())
                for ts, jpg in ring:
                    if ts not in seen:
                        frames.append((ts, jpg)); seen.add(ts)
            if not frames:
                return
            frames.sort(key=lambda p: p[0])
            first = cv2.imdecode(np.frombuffer(frames[0][1], np.uint8), cv2.IMREAD_COLOR)
            if first is None:
                return
            h, w = first.shape[:2]
            tmp = f"{final}.tmp{os.getpid()}.mp4"
            vw = cv2.VideoWriter(tmp, cv2.VideoWriter_fourcc(*"mp4v"), CLIP_FPS, (w, h))
            for _, jpg in frames:
                img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    if img.shape[:2] != (h, w):
                        img = cv2.resize(img, (w, h))
                    vw.write(img)
            vw.release()
            os.replace(tmp, final)           # atomic: readers never see a partial clip
        except Exception as e:  # noqa: BLE001 — clips are best-effort
            print(f"    → clip write failed ({uid}): {e}")
        finally:
            self._active.release()


_clips = ClipRecorder()


# ── Runtime metrics (served by the UI bridge at /diag via SHM) ──────────────
# Per-camera counters + global diagnostics, dumped atomically to
# {SHM_DIR}/metrics.json every METRICS_INTERVAL seconds by a tiny thread.
METRICS_INTERVAL = float(os.environ.get("METRICS_INTERVAL", "2.0"))
_metrics_lock = threading.Lock()
_metrics = {"started_at": time.time(), "cameras": {}, "tracker_backend": None,
            "tracker_degraded": False, "detect_ms_ema": None, "identity_queue_depth": 0}


def _metrics_cam(uid):
    with _metrics_lock:
        return _metrics["cameras"].setdefault(uid, {
            "detections": 0, "tracks_created": 0, "observations": 0,
            "identity_emits": 0, "stale_frames_dropped": 0, "frame_age_s": None,
        })


def _metrics_bump(uid, key, n=1):
    cam = _metrics_cam(uid)
    with _metrics_lock:
        cam[key] = cam.get(key, 0) + n


def _metrics_set(key, value, uid=None):
    if uid is None:
        with _metrics_lock:
            _metrics[key] = value
    else:
        cam = _metrics_cam(uid)
        with _metrics_lock:
            cam[key] = value


def start_metrics_writer(streams):
    """Dump metrics.json (atomic) every METRICS_INTERVAL s: per-camera counters,
    decode frame age, detect latency EMA, identity queue depth, tracker backend
    (+degraded flag), and GPU memory — the /diag diagnostics surface."""
    path = os.path.join(SHM_DIR, "metrics.json")
    tmp = os.path.join(SHM_DIR, ".metrics.tmp")

    def _loop():
        while True:
            try:
                for s in streams:
                    age = s.frame_age() if hasattr(s, "frame_age") else None
                    _metrics_set("frame_age_s",
                                 round(age, 2) if age not in (None, float("inf")) else None,
                                 uid=s.camera_uid)
                with _metrics_lock:
                    snap = json.loads(json.dumps(_metrics))   # deep copy via json
                snap["ts"] = time.time()
                if torch.cuda.is_available():
                    snap["gpu"] = {
                        "mem_allocated_mb": round(torch.cuda.memory_allocated() / 1e6, 1),
                        "mem_reserved_mb": round(torch.cuda.memory_reserved() / 1e6, 1),
                    }
                with open(tmp, "w") as f:
                    json.dump(snap, f)
                os.replace(tmp, path)
            except Exception:  # noqa: BLE001 — diagnostics must never kill anything
                pass
            time.sleep(METRICS_INTERVAL)

    threading.Thread(target=_loop, daemon=True, name="metrics").start()


# Per-run epoch baked into every detection_id. Tracker ids restart from t1 on
# every pipeline launch, so without this a fresh run's "CAM-t3" would collide
# with the previous run's "CAM-t3" still cached by the Brain's track-sticky map
# (TTL minutes) — a different person could inherit the old identity.
_RUN_EPOCH = int(time.time()) % 1_000_000


def track_uuid_of(camera_uid, idx):
    """Run-unique, camera-scoped track id — shared by every emit of one track."""
    return f"{camera_uid}-r{_RUN_EPOCH}-t{idx}"


def build_payload(camera_uid, score, face_embedding, body_embedding, stamp_ms, idx,
                  evidence=None, bbox=None, frame_wh=None, clip_path=None):
    """Part 1 → Part 2 detection payload (contracts/part1_to_part2.event.schema.json).

    `idx` is the tracker's track id. detection_id/track_uuid are STABLE per
    track so every emit of the same person shares one id — the Brain groups
    sightings, keeps identity sticky across re-emits, and folds a track's
    Unknown case once a face resolves.

    `evidence` is the dict from save_evidence_set(): ONE immutable set of
    files captured from the same moment. Paths are passed EXPLICITLY — the
    Brain and UI never derive one path from another."""
    ev = evidence or {}
    return {
        "detection_id": track_uuid_of(camera_uid, idx),
        "track_uuid": track_uuid_of(camera_uid, idx),
        "camera_id": camera_uid,
        "timestamp": utc_now_iso(),
        "detection_conf": round(float(score), 4),
        "face_embedding": [float(x) for x in face_embedding] if face_embedding is not None else None,
        "body_embedding": [float(x) for x in body_embedding] if body_embedding is not None else None,
        "bbox": [round(float(v), 1) for v in bbox[:4]] if bbox is not None else None,
        "frame_w": frame_wh[0] if frame_wh else None,
        "frame_h": frame_wh[1] if frame_wh else None,
        "face_path": ev.get("face_path"),
        "body_path": ev.get("body_path"),
        "full_frame_path": ev.get("full_frame_path"),
        "full_frame_annotated_path": ev.get("full_frame_annotated_path"),
        "snapshot_path": ev.get("body_path"),         # legacy alias
        "clip_path": clip_path,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cameras", default="all", help="comma-separated camera_uids, or 'all'")
    ap.add_argument("--conf", type=float, default=0.50, help="min person-detection confidence")
    ap.add_argument("--cooldown", type=float, default=2.0, help="min seconds between passes per camera")
    ap.add_argument("--segment", action="store_true", help="run SAM 2 to blank background before ReID")
    ap.add_argument("--show", action="store_true", help="show an annotated window")
    ap.add_argument("--emit", action="store_true", help="POST identify-camera detections to the Brain")
    ap.add_argument("--brain-url", default="http://localhost:8000")
    args = ap.parse_args()

    # ── select cameras ──
    cameras = load_cameras(streamable_only=True)
    if args.cameras != "all":
        wanted = {c.strip() for c in args.cameras.split(",")}
        cameras = [c for c in cameras if c.camera_uid in wanted]
    if not cameras:
        print("No streamable cameras selected. Check cameras.json / cameras.secrets.json.")
        return

    id_cams = [c for c in cameras if c.runs_identity]
    det_cams = [c for c in cameras if not c.runs_identity]
    print(f"Cameras: {len(cameras)} total | identify: "
          f"{', '.join(c.camera_uid for c in id_cams) or '—'} | "
          f"detect-only: {', '.join(c.camera_uid for c in det_cams) or '—'}")

    # ── models ──
    detector = PersonDetector()                 # detection runs on every camera
    identifier = Identifier() if id_cams else None   # ReID + gallery only if needed
    segmenter = None
    if args.segment and id_cams:
        from segmenter import SAM2Segmenter
        segmenter = SAM2Segmenter()

    # Detection (the grid) gets a HIGH-priority CUDA stream; the heavy identity
    # work gets a LOW-priority one. On the single GPU this lets the detector's
    # kernels be scheduled ahead of face/body/SAM2 kernels, so the live grid stays
    # smooth even while identity churns behind it. No-ops on CPU.
    _cuda = torch.cuda.is_available()
    hi_stream = torch.cuda.Stream(priority=-1) if _cuda else None   # detection / grid
    lo_stream = torch.cuda.Stream(priority=0) if _cuda else None    # background identity

    session = None
    if args.emit:
        import requests
        session = requests.Session()

    streams = nvr.start_streams(cameras)
    start_preview_writer(streams)          # serve REAL annotated frames to the dashboard
    start_metrics_writer(streams)          # /diag diagnostics via SHM metrics.json
    roles = {c.camera_uid: c for c in cameras}
    detect_interval = float(os.environ.get("DETECT_INTERVAL", "0.25"))
    resolve_interval = float(os.environ.get("RESOLVE_INTERVAL", "1.0"))

    # Per-camera trackers give each person a stable track, so identity is resolved
    # ONCE per track (then locked) instead of per frame — no Unknown/Visitor churn,
    # one database id per person. Detection (fast) and identity (background) share
    # the tracks under this lock.
    def _make_tracker():
        """Kalman-motion tracker (Oc-SORT via the pinned boxmot dependency).
        There is NO silent fallback: if BoxMOT cannot load, startup FAILS with
        the reason, unless the operator explicitly opts into degraded tracking
        (TRACKER=simple, or TRACKER_ALLOW_FALLBACK=1). A degraded tracker is
        flagged in /diag metrics — fragmentation counts mean little without
        knowing which backend produced them."""
        name = os.environ.get("TRACKER", "ocsort").lower()
        if name == "simple":
            _metrics_set("tracker_backend", "simple (explicit)")
            _metrics_set("tracker_degraded", True)
            return SimpleTracker()
        try:
            tr = MotionTracker()
            _metrics_set("tracker_backend", "ocsort (boxmot)")
            return tr
        except Exception as e:  # noqa: BLE001
            if os.environ.get("TRACKER_ALLOW_FALLBACK", "0") in ("1", "true", "yes"):
                print(f"[tracker] DEGRADED: BoxMOT unavailable ({e}); "
                      f"greedy-IoU fallback EXPLICITLY allowed")
                _metrics_set("tracker_backend", "simple (degraded fallback)")
                _metrics_set("tracker_degraded", True)
                return SimpleTracker()
            raise SystemExit(
                f"[tracker] BoxMOT/OC-SORT failed to load: {e}\n"
                f"  Fix the dependency (pip install boxmot==19.0.0) or explicitly "
                f"accept degraded tracking with TRACKER_ALLOW_FALLBACK=1.")

    state_lock = threading.Lock()
    trackers = {c.camera_uid: _make_tracker() for c in cameras}
    print(f"[tracker] using {type(next(iter(trackers.values()))).__name__}")
    frames = {}          # uid -> latest frame (for the identity worker to crop from)
    stop_flag = threading.Event()

    # ── Per-camera detection gates (configurable — hard filters reject real
    # humans on some scenes, so every gate can be tuned per camera in
    # cameras.json: roi / exclude / min_height_frac / min_aspect) ────────────
    MIN_HEIGHT_FRAC = float(os.environ.get("MIN_HEIGHT_FRAC", "0.0"))
    MIN_ASPECT = float(os.environ.get("MIN_ASPECT", "0.0"))

    def _passes_gates(cam, box, fw, fh):
        x1, y1, x2, y2 = box[:4]
        w, h = max(1.0, x2 - x1), max(1.0, y2 - y1)
        min_h = cam.min_height_frac if cam.min_height_frac is not None else MIN_HEIGHT_FRAC
        if min_h and h / fh < min_h:
            return False
        min_a = cam.min_aspect if cam.min_aspect is not None else MIN_ASPECT
        if min_a and h / w < min_a:
            return False
        cx, cy = (x1 + x2) / 2 / fw, (y1 + y2) / 2 / fh
        roi = cam.roi
        if roi and not (roi[0] <= cx <= roi[2] and roi[1] <= cy <= roi[3]):
            return False
        for ex in (cam.exclude or ()):
            if ex[0] <= cx <= ex[2] and ex[1] <= cy <= ex[3]:
                return False
        return True

    # ── Immediate observation: EVERY stable human track logs a sighting NOW —
    # detect-only cameras and faceless people included. The evidence set +
    # POST run on a background thread (never blocks the detect loop); identity
    # (if this camera runs it) arrives later on the SAME track_uuid, and the
    # Brain folds the observation's Unknown case onto the resolved person. ──
    obs_queue = collections.deque(maxlen=64)     # (uid, track_id, frame, box)
    obs_session = None
    if args.emit:
        import requests as _rq
        obs_session = _rq.Session()

    def observation_worker():
        while not stop_flag.is_set():
            try:
                uid, t, frame, box = obs_queue.popleft()
            except IndexError:
                time.sleep(0.05)
                continue
            try:
                stamp_ms = int(time.time() * 1000)
                crop = crop_person(frame, box)
                evidence = save_evidence_set(uid, stamp_ms, t.id, 0,
                                             frame_bgr=frame, box_xyxy=box,
                                             body_bgr=crop, face_bgr=None)
                clip_path = _clips.trigger(uid, evidence["stem"])
                with state_lock:
                    t.sight_stem = evidence["stem"]
                    t.clip_path = clip_path
                    if not t.snap_path:
                        t.snap_path = evidence.get("body_path")
                _metrics_bump(uid, "observations")
                if obs_session is not None:
                    fh, fw = frame.shape[:2]
                    payload = build_payload(uid, box[4], None, None, stamp_ms, t.id,
                                            evidence=evidence, bbox=box,
                                            frame_wh=(fw, fh), clip_path=clip_path)
                    obs_session.post(f"{args.brain_url}/events", json=payload, timeout=10)
            except Exception as e:  # noqa: BLE001 — observations are best-effort
                print(f"    → observation failed ({uid}): {e}")

    threading.Thread(target=observation_worker, daemon=True, name="observer").start()

    def _resolve_track(cam, uid, frame, t, box):
        """Heavy identity for ONE track on the LOW-priority GPU stream, using
        quality-gated temporal pooling. Returns True if features were found."""
        # Occlusion gate: if another person's box heavily covers this one, the crop
        # would include a neighbour (wrong face) — skip and retry once they separate.
        with state_lock:
            others = [tuple(o.box) for o in trackers[uid].tracks
                      if o.id != t.id and o.misses == 0]
        if _occluded(box, others):
            return False
        with _stream_ctx(lo_stream):
            crop = crop_person(frame, box)                  # raw crop → face
            if crop is None:
                return False
            body_crop = crop
            if segmenter is not None:
                try:
                    segmenter.set_frame(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                    mask = segmenter.mask_for_box(box)
                    masked = crop_person(frame, box, mask=mask)
                    if masked is not None:
                        body_crop = masked                  # background-blanked → body ReID
                except Exception as e:  # noqa: BLE001 — fall back to raw crop
                    print(f"    → segment failed: {e}")
            face_emb, body_emb = identifier.extract(crop, body_bgr=body_crop)
        if lo_stream is not None:
            lo_stream.synchronize()
        if face_emb is None and body_emb is None:
            return False                     # no usable features yet — retry later

        face_q = identifier.last_face_quality  # AdaFace-norm quality of THIS probe's face
        face_ok = face_emb is not None and face_q >= FACE_MIN_QUALITY
        face_crop = identifier.last_face_crop
        stamp_ms = int(time.time() * 1000)
        with state_lock:
            t.probes += 1
            # Pool this probe's face into a QUALITY-WEIGHTED running centroid, so we
            # identify/enroll from many frames averaged (weighted by quality) rather
            # than one best shot — a stable vector that re-matches one gallery entry.
            # QUALITY GATE: only faces above the floor feed identity. A low-quality
            # face is ignored entirely (not pooled) — precision over coverage.
            if face_ok:
                w = max(1e-3, float(face_q))
                fv = np.asarray(face_emb, dtype=np.float32) * w
                t.face_emb_sum = fv if t.face_emb_sum is None else t.face_emb_sum + fv
                t.face_w_sum += w
                if face_q > t.best_face_q:
                    t.best_face_emb, t.best_body_emb, t.best_face_q = face_emb, body_emb, face_q
            elif t.best_body_emb is None and body_emb is not None:
                t.best_body_emb = body_emb              # keep a body even before any face
            # Decide whether to (re-)emit. Emit a FIRST label as soon as we've pooled
            # a few good faces (snappy — a poor single face enrolls a bad gallery entry
            # the person then fails to re-match, so we want a few, not one), then keep
            # pooling and re-emit a sharper centroid once the pool grows meaningfully.
            budget_done = t.probes >= IDENTITY_MAX_PROBES
            have_face = t.face_w_sum > 0.0
            first_ready = have_face and t.probes >= IDENTITY_MIN_EMIT_PROBES
            ready = first_ready or t.emitted
            ready = ready or (budget_done and not have_face)   # body-only fallback → Unknown case
            if not ready:
                return True                          # keep probing; worker retries this track
            if t.emitted and not (t.face_w_sum > t.emit_face_w * (1.0 + IDENTITY_REEMIT_FRAC)):
                return True                          # already sent our pooled best; nothing better yet
            emit_face = _pooled_face(t.face_emb_sum, t.face_w_sum)
            emit_body = t.best_body_emb
            emit_q = t.best_face_q
            seq = t.probes                           # per-track sequence → unique stems
            clip_path = t.clip_path                  # recorded at first sighting (if any)
            t.emitted = True
            t.emit_face_w = t.face_w_sum
            t.emit_face_q = t.best_face_q

        # ── ONE immutable evidence set from THIS probe's moment ─────────────
        # Body, face (when this probe had one), untouched original frame and a
        # separate annotated copy — all from the same frame, written atomically
        # under a NEW stem. A later, better frame becomes a NEW sighting; it
        # never replaces these files. No face this probe → face_path stays None
        # (the UI says "No face captured for this sighting" — it never borrows).
        evidence = save_evidence_set(
            uid, stamp_ms, t.id, seq,
            frame_bgr=frame, box_xyxy=box,
            body_bgr=crop, face_bgr=face_crop if face_ok else None,
        )
        with state_lock:
            t.snap_path = evidence.get("body_path") or t.snap_path
            t.face_path = evidence.get("face_path") or t.face_path
        _metrics_bump(uid, "identity_emits")

        disp = color = None
        locked = False
        if args.emit:
            fh, fw = frame.shape[:2]
            payload = build_payload(uid, box[4], emit_face, emit_body, stamp_ms, t.id,
                                    evidence=evidence, bbox=box, frame_wh=(fw, fh),
                                    clip_path=clip_path)
            try:
                j = session.post(f"{args.brain_url}/events", json=payload, timeout=10).json()
                disp, color = _display_from_brain(j)
                locked = j.get("label") in ("Visitor", "Employee")
            except Exception as e:  # noqa: BLE001 — keep the worker alive
                print(f"    → POST failed: {e}")
        if disp is None:                     # offline / no --emit → local gallery
            res = identifier.identify_features(
                emit_face, emit_body, detection_conf=box[4], face_threshold=cam.match_threshold)
            if res["label"] == "Employee":
                disp, color, locked = f"Employee {res['name'] or res['person_id'] or ''}".strip(), _COL_EMP, True
            elif res["label"] == "Visitor":
                disp, color, locked = f"Visitor {res['person_id'] or ''}".strip(), _COL_VIS, True
            else:
                disp, color = "Unknown", _COL_UNKNOWN
        with state_lock:
            t.label, t.color = disp, color
            if locked:
                t.resolved = True            # lock: never re-classified → no churn, one id
        feat = ("F" if emit_face is not None else "-") + ("B" if emit_body is not None else "-")
        print(f"[{uid}] track#{t.id} -> {disp}  (det {box[4]:.2f} feat {feat} q{emit_q:.0f} p{t.probes})")
        return True

    def identity_worker():
        """Background identity SERVICE. Each pass it picks the single most-overdue
        STABLE track across all identify cameras, resolves it on the low-priority
        GPU stream, then sleeps so the detector (grid) keeps the GPU. Labels and DB
        updates may lag a few seconds by design — the grid never waits on this."""
        gap = 1.0 / max(0.5, IDENTITY_MAX_RATE)      # min seconds between heavy resolves
        while not stop_flag.is_set():
            now = time.time()
            pick = None      # (cam, uid, frame, track, box, overdue)
            with state_lock:
                for cam in id_cams:
                    uid = cam.camera_uid
                    frame = frames.get(uid)
                    if frame is None:
                        continue
                    for t in trackers[uid].tracks:
                        # Only STABLE, currently-visible, not-yet-locked tracks. A
                        # still-unknown track re-tries after the latency budget.
                        if t.resolved or t.misses != 0 or t.hits < IDENTITY_MIN_HITS:
                            continue
                        due_after = IDENTITY_LATENCY_BUDGET if t.emitted else resolve_interval
                        overdue = now - t.last_resolve - due_after
                        if overdue < 0:
                            continue
                        if pick is None or overdue > pick[5]:
                            pick = (cam, uid, frame, t, tuple(t.box), overdue)
                if pick is not None:
                    pick[3].last_resolve = now   # claim now so we don't re-pick it next pass
            if pick is None:
                time.sleep(0.05)
                continue
            cam, uid, frame, t, box, _ = pick
            try:
                _resolve_track(cam, uid, frame, t, box)
            except Exception as e:  # noqa: BLE001 — never kill the worker
                print(f"    → identity error [{uid}#{t.id}]: {e}")
            time.sleep(gap)                  # YIELD the GPU to the detector between resolves

    if id_cams:
        threading.Thread(target=identity_worker, daemon=True).start()

    last_detect = {c.camera_uid: 0.0 for c in cameras}
    print(f"Running (boxes ~{1/detect_interval:.0f} fps/cam, batched fp16 detect, "
          f"tracker-based identity, preview -> {SHM_DIR}). Ctrl-C to stop.")
    perf = {"detect_s": 0.0, "calls": 0, "cams": 0, "last_report": time.time()}
    try:
        while True:
            now = time.time()
            # Gather every camera whose detect_interval has elapsed, then run them
            # all through ONE batched GPU forward pass (one launch, not N).
            due = []
            for stream in streams:
                uid = stream.camera_uid
                if now - last_detect[uid] < detect_interval:
                    continue
                # Stale-frame dropping: a frozen/lagging decode must not burn a
                # GPU slot detecting the same old frame (fair micro-batching —
                # live cameras aren't starved by dead ones).
                if hasattr(stream, "frame_age") and stream.frame_age() > STALE_FRAME_MAX_S:
                    _metrics_bump(uid, "stale_frames_dropped")
                    continue
                frame = stream.get_frame()
                if frame is None:
                    continue
                last_detect[uid] = now
                due.append((stream, frame))

            if not due:
                time.sleep(0.01)                  # nothing due — avoid busy-spin
                continue

            t0 = time.time()
            # Detect on a downscaled COPY (cheap CPU pre-proc; RT-DETR resizes
            # internally regardless), then scale boxes back to full-res so the
            # tracker and crops operate on native pixels.
            det_frames, det_scales = [], []
            for _, frame in due:
                fh, fw = frame.shape[:2]
                s = DETECT_MAX_SIDE / max(fh, fw) if max(fh, fw) > DETECT_MAX_SIDE else 1.0
                small = cv2.resize(frame, (int(fw * s), int(fh * s)), interpolation=cv2.INTER_AREA) if s < 1.0 else frame
                det_frames.append(small)
                det_scales.append(s)
            with _stream_ctx(hi_stream):                 # detection on the HIGH-priority stream
                box_lists = detector.detect_batch(det_frames, conf=args.conf)
            if hi_stream is not None:
                hi_stream.synchronize()
            box_lists = [
                [(x1 / s, y1 / s, x2 / s, y2 / s, sc) for (x1, y1, x2, y2, sc) in boxes]
                for boxes, s in zip(box_lists, det_scales)
            ]
            batch_ms = (time.time() - t0) * 1000.0
            perf["detect_s"] += time.time() - t0
            perf["calls"] += 1
            perf["cams"] += len(due)
            with _metrics_lock:
                ema = _metrics.get("detect_ms_ema")
                _metrics["detect_ms_ema"] = round(
                    batch_ms if ema is None else 0.9 * ema + 0.1 * batch_ms, 1)

            for (stream, frame), boxes in zip(due, box_lists):
                uid = stream.camera_uid
                cam = roles[uid]
                fh, fw = frame.shape[:2]
                # Per-camera geometry gates (roi / exclude / min size / aspect).
                boxes = [b for b in boxes if _passes_gates(cam, b, fw, fh)]
                _metrics_bump(uid, "detections", len(boxes))
                with state_lock:
                    frames[uid] = frame
                    known = {id(t) for t in trackers[uid].tracks} \
                        if hasattr(trackers[uid], "tracks") else set()
                    tks = trackers[uid].update(boxes, frame)
                    new_tracks = sum(1 for t in tks if id(t) not in known)
                    # Immediate sighting: EVERY stable track (identify AND
                    # detect-only cameras) logs an observation the moment it
                    # stabilises — faceless people become persistent Unknown
                    # cases instead of never being logged at all.
                    for t in tks:
                        if not t.sighted and t.hits >= IDENTITY_MIN_HITS:
                            t.sighted = True
                            obs_queue.append((uid, t, frame.copy(), tuple(t.box)))
                if new_tracks:
                    _metrics_bump(uid, "tracks_created", new_tracks)
                if cam.runs_identity:
                    # Neutral 'person' (grey) until the background worker resolves the
                    # track; it then recolors to Employee/Visitor/Unknown. So a box
                    # shows up LIVE the instant the detector sees someone, and the name
                    # fills in a few seconds later.
                    annos = [(*t.box[:4], t.label or "person", t.color or _COL_PERSON) for t in tks]
                else:
                    annos = [(*t.box[:4], "person", _COL_PERSON) for t in tks]
                set_annotations(uid, annos)

            if now - perf["last_report"] >= 5.0 and perf["calls"]:
                with state_lock:
                    backlog = sum(1 for c in id_cams for t in trackers[c.camera_uid].tracks
                                  if not t.resolved and t.misses == 0 and t.hits >= IDENTITY_MIN_HITS)
                _metrics_set("identity_queue_depth", backlog)
                print(f"[perf] grid: detect {perf['detect_s']/perf['calls']*1000:.0f} ms/batch, "
                      f"{perf['calls']/(now-perf['last_report']):.1f} batches/s, "
                      f"{perf['cams']/perf['calls']:.1f} cams/batch | identity backlog {backlog}")
                perf.update(detect_s=0.0, calls=0, cams=0, last_report=now)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        stop_flag.set()
        for s in streams:
            s.stop()


if __name__ == "__main__":
    main()
