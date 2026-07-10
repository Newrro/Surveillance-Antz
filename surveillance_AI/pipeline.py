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
IDENTITY_MAX_PROBES = int(os.environ.get("IDENTITY_MAX_PROBES", "3"))      # frames sampled before first emit
IDENTITY_REEMIT_FRAC = float(os.environ.get("IDENTITY_REEMIT_FRAC", "0.15"))  # re-emit if a face is this much better

# ── Face quality gate (IDENTITY_REDESIGN.md Phase A) ───────────────────────
# Precision-first: only faces above this quality (AdaFace-norm × sharpness proxy)
# feed identity. A low-quality face contributes NOTHING to the template, so a
# track with only bad faces stays faceless → Unknown, rather than enrolling a
# garbage vector that later false-matches. Biggest single precision lever.
FACE_MIN_QUALITY = float(os.environ.get("FACE_MIN_QUALITY", "18.0"))
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
    a slow encode on one tile can't stall the others."""
    tmp = os.path.join(SHM_DIR, f".{s.camera_uid}.tmp")
    final = os.path.join(SHM_DIR, f"{s.camera_uid}.jpg")
    while True:
        t0 = time.time()
        frame = s.get_frame()
        if frame is not None:
            h, w = frame.shape[:2]
            small = cv2.resize(frame, (PREVIEW_W, PREVIEW_H))
            sx, sy = PREVIEW_W / max(1, w), PREVIEW_H / max(1, h)
            for (x1, y1, x2, y2, label, color) in get_annotations(s.camera_uid):
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


def save_snapshot(camera_uid, crop_bgr, stamp_ms, idx):
    """Save the full-body crop under storage/img/<camera_uid>/ and return its path."""
    out_dir = os.path.join(STORAGE_IMG, camera_uid)
    os.makedirs(out_dir, exist_ok=True)
    fname = f"{stamp_ms}_{idx}.jpg"
    cv2.imwrite(os.path.join(out_dir, fname), crop_bgr)
    return f"storage/img/{camera_uid}/{fname}"


def save_face_snapshot(camera_uid, face_bgr, stamp_ms, idx):
    """Save the aligned face crop next to the body crop as <stem>_face.jpg. The UI
    derives this path from the body snapshot, so no contract change is needed."""
    out_dir = os.path.join(STORAGE_IMG, camera_uid)
    os.makedirs(out_dir, exist_ok=True)
    fname = f"{stamp_ms}_{idx}_face.jpg"
    cv2.imwrite(os.path.join(out_dir, fname), face_bgr)
    return f"storage/img/{camera_uid}/{fname}"


def save_frame_snapshot(camera_uid, frame_bgr, box_xyxy, stamp_ms, idx):
    """Save the whole scene (downscaled, person's box drawn) as <stem>_full.jpg,
    sharing the body snapshot's stem so the UI derives it the same way it does the
    face. A verification companion — lets a human confirm the crops are the right
    person, not part of the identity signal."""
    out_dir = os.path.join(STORAGE_IMG, camera_uid)
    os.makedirs(out_dir, exist_ok=True)
    h, w = frame_bgr.shape[:2]
    s = FULL_FRAME_MAX_W / w if w > FULL_FRAME_MAX_W else 1.0
    img = cv2.resize(frame_bgr, (int(w * s), int(h * s))) if s < 1.0 else frame_bgr.copy()
    x1, y1, x2, y2 = (int(v * s) for v in box_xyxy[:4])
    cv2.rectangle(img, (x1, y1), (x2, y2), _COL_VIS, 2)
    fname = f"{stamp_ms}_{idx}_full.jpg"
    cv2.imwrite(os.path.join(out_dir, fname), img, [int(cv2.IMWRITE_JPEG_QUALITY), FULL_FRAME_QUALITY])
    return f"storage/img/{camera_uid}/{fname}"


def build_payload(camera_uid, score, face_embedding, body_embedding, snapshot_path, stamp_ms, idx):
    """Part 1 → Part 2 detection payload (contracts/part1_to_part2.event.schema.json).

    `idx` is the tracker's track id. detection_id is STABLE per track (no
    timestamp) so every emit of the same person shares one id — the Brain can
    dedup repeat Unknown sightings on it, and the UI groups them into a single
    'Unknown person' card instead of minting a new card every emit."""
    return {
        "detection_id": f"{camera_uid}-t{idx}",
        "camera_id": camera_uid,
        "timestamp": utc_now_iso(),
        "detection_conf": round(float(score), 4),
        "face_embedding": [float(x) for x in face_embedding] if face_embedding is not None else None,
        "body_embedding": [float(x) for x in body_embedding] if body_embedding is not None else None,
        "snapshot_path": snapshot_path,
        "clip_path": None,                            # short-clip capture: TODO
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
    roles = {c.camera_uid: c for c in cameras}
    detect_interval = float(os.environ.get("DETECT_INTERVAL", "0.25"))
    resolve_interval = float(os.environ.get("RESOLVE_INTERVAL", "1.0"))

    # Per-camera trackers give each person a stable track, so identity is resolved
    # ONCE per track (then locked) instead of per frame — no Unknown/Visitor churn,
    # one database id per person. Detection (fast) and identity (background) share
    # the tracks under this lock.
    def _make_tracker():
        """Kalman-motion tracker (Oc-SORT) by default — stable IDs through
        occlusion, far fewer spurious new tracks. TRACKER=simple forces the old
        IoU tracker; falls back to it automatically if BoxMOT can't load."""
        name = os.environ.get("TRACKER", "ocsort").lower()
        if name in ("ocsort", "bytetrack", "motion", "boxmot"):
            try:
                return MotionTracker()
            except Exception as e:  # noqa: BLE001
                print(f"[tracker] BoxMOT unavailable ({e}); using SimpleTracker")
        return SimpleTracker()

    state_lock = threading.Lock()
    trackers = {c.camera_uid: _make_tracker() for c in cameras}
    print(f"[tracker] using {type(next(iter(trackers.values()))).__name__}")
    frames = {}          # uid -> latest frame (for the identity worker to crop from)
    stop_flag = threading.Event()

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
        stamp_ms = int(time.time() * 1000)
        with state_lock:
            t.probes += 1
            # Pool this probe's face into a QUALITY-WEIGHTED running centroid, so we
            # identify/enroll from many frames averaged (weighted by quality) rather
            # than one best shot — a stable vector that re-matches one gallery entry.
            # QUALITY GATE: only faces above the floor feed identity. A low-quality
            # face is ignored entirely (not pooled) — precision over coverage.
            if face_emb is not None and face_q >= FACE_MIN_QUALITY:
                w = max(1e-3, float(face_q))
                fv = np.asarray(face_emb, dtype=np.float32) * w
                t.face_emb_sum = fv if t.face_emb_sum is None else t.face_emb_sum + fv
                t.face_w_sum += w
                if face_q > t.best_face_q:               # best single shot → snapshot + paired body
                    t.best_face_emb, t.best_body_emb, t.best_face_q = face_emb, body_emb, face_q
            elif t.best_body_emb is None and body_emb is not None:
                t.best_body_emb = body_emb              # keep a body even before any face
            # Snapshots: one body crop per track; refresh the face thumb on a better shot.
            if t.snap_path is None:
                t.snap_path = save_snapshot(uid, crop, stamp_ms, t.id)
                save_frame_snapshot(uid, frame, box, stamp_ms, t.id)   # full scene for verification
            if face_emb is not None and face_q >= t.best_face_q and identifier.last_face_crop is not None:
                t.face_path = save_face_snapshot(uid, identifier.last_face_crop, stamp_ms, t.id)
            # Decide whether to (re-)emit. Wait for a few probes so the FIRST emit
            # pools several faces (a poor single face enrolls a bad gallery entry the
            # person then fails to re-match). After that, re-emit only once the pool
            # has grown meaningfully (more/better face evidence → sharper centroid).
            budget_done = t.probes >= IDENTITY_MAX_PROBES
            have_face = t.face_w_sum > 0.0
            ready = have_face and (budget_done or t.emitted)
            ready = ready or (budget_done and not have_face)   # body-only fallback → Unknown
            if not ready:
                return True                          # keep probing; worker retries this track
            if t.emitted and not (t.face_w_sum > t.emit_face_w * (1.0 + IDENTITY_REEMIT_FRAC)):
                return True                          # already sent our pooled best; nothing better yet
            emit_face = _pooled_face(t.face_emb_sum, t.face_w_sum)
            emit_body = t.best_body_emb
            snap, emit_q = t.snap_path, t.best_face_q
            t.emitted = True
            t.emit_face_w = t.face_w_sum
            t.emit_face_q = t.best_face_q

        disp = color = None
        locked = False
        if args.emit:
            payload = build_payload(uid, box[4], emit_face, emit_body, snap, stamp_ms, t.id)
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
            perf["detect_s"] += time.time() - t0
            perf["calls"] += 1
            perf["cams"] += len(due)

            for (stream, frame), boxes in zip(due, box_lists):
                uid = stream.camera_uid
                cam = roles[uid]
                with state_lock:
                    frames[uid] = frame
                    tks = trackers[uid].update(boxes, frame)
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
