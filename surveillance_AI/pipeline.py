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

# flat sibling modules
from detector import PersonDetector, draw_boxes
from feature_id.identify import Identifier
from tracker import SimpleTracker

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
PREVIEW_FPS = float(os.environ.get("PREVIEW_FPS", "8"))
PREVIEW_W = int(os.environ.get("PREVIEW_W", "640"))
PREVIEW_H = int(os.environ.get("PREVIEW_H", "360"))

_annot = {}                 # camera_uid -> list of (x1, y1, x2, y2, label, color)
_annot_lock = threading.Lock()


def set_annotations(uid, annos):
    with _annot_lock:
        _annot[uid] = annos


def get_annotations(uid):
    with _annot_lock:
        return list(_annot.get(uid, ()))


def start_preview_writer(streams):
    """Background thread: for each camera, overlay the latest boxes on the latest
    frame and write a JPEG to SHM at PREVIEW_FPS — smooth video, boxes update at
    the detection rate. Decouples display FPS from the (slower) detection loop."""
    os.makedirs(SHM_DIR, exist_ok=True)
    period = 1.0 / max(1.0, PREVIEW_FPS)
    enc = [int(cv2.IMWRITE_JPEG_QUALITY), 70]

    def loop():
        while True:
            t0 = time.time()
            for s in streams:
                frame = s.get_frame()
                if frame is None:
                    continue
                vis = frame.copy()
                for (x1, y1, x2, y2, label, color) in get_annotations(s.camera_uid):
                    cv2.rectangle(vis, (int(x1), int(y1)), (int(x2), int(y2)), color, 3)
                    if label:
                        cv2.putText(vis, label, (int(x1), max(18, int(y1) - 8)),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
                small = cv2.resize(vis, (PREVIEW_W, PREVIEW_H))
                ok, buf = cv2.imencode(".jpg", small, enc)
                if not ok:
                    continue
                tmp = os.path.join(SHM_DIR, f".{s.camera_uid}.tmp")
                final = os.path.join(SHM_DIR, f"{s.camera_uid}.jpg")
                try:
                    with open(tmp, "wb") as f:
                        f.write(buf.tobytes())
                    os.replace(tmp, final)   # atomic swap so readers never see a half-written file
                except OSError:
                    pass
            dt = time.time() - t0
            if dt < period:
                time.sleep(period - dt)

    th = threading.Thread(target=loop, daemon=True)
    th.start()
    return th


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
    state_lock = threading.Lock()
    trackers = {c.camera_uid: SimpleTracker() for c in cameras}
    frames = {}          # uid -> latest frame (for the identity worker to crop from)
    stop_flag = threading.Event()

    def identity_worker():
        """Resolve identity ONCE per track, then lock it. Labels come from the
        Brain's POST /events response (authoritative database id)."""
        while not stop_flag.is_set():
            ran = False
            for cam in id_cams:
                uid = cam.camera_uid
                now = time.time()
                with state_lock:
                    frame = frames.get(uid)
                    # First attempt after resolve_interval; if still Unknown, back off
                    # to 5s so unknown/distant people don't spam the log.
                    pend = [(t, tuple(t.box)) for t in trackers[uid].tracks
                            if not t.resolved and t.misses == 0
                            and now - t.last_resolve >= (5.0 if t.emitted else resolve_interval)]
                if frame is None or not pend:
                    continue
                # SAM 2 once per (camera, frame): give it the frame, then pull a
                # per-box mask below. Blanking the background is what makes the
                # body vector describe the PERSON instead of the shared scene.
                seg_ready = False
                for t, box in pend:
                    t.last_resolve = now
                    crop = crop_person(frame, box)          # raw crop → face
                    if crop is None:
                        continue
                    body_crop = crop
                    if segmenter is not None:
                        try:
                            if not seg_ready:
                                segmenter.set_frame(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                                seg_ready = True
                            mask = segmenter.mask_for_box(box)
                            masked = crop_person(frame, box, mask=mask)
                            if masked is not None:
                                body_crop = masked        # background-blanked → body ReID
                        except Exception as e:  # noqa: BLE001 — fall back to raw crop
                            print(f"    → segment failed: {e}")
                    face_emb, body_emb = identifier.extract(crop, body_bgr=body_crop)
                    if face_emb is None and body_emb is None:
                        continue                 # no usable features yet — stays 'Unknown', retry
                    ran = True
                    t.emitted = True
                    stamp_ms = int(now * 1000)
                    snap = save_snapshot(uid, crop, stamp_ms, t.id)
                    # Also save a face thumbnail when a face was found, so the UI
                    # can show BOTH a full-body crop and a face crop per person.
                    if identifier.last_face_crop is not None:
                        save_face_snapshot(uid, identifier.last_face_crop, stamp_ms, t.id)
                    disp = color = None
                    locked = False
                    if args.emit:
                        payload = build_payload(uid, box[4], face_emb, body_emb, snap, stamp_ms, t.id)
                        try:
                            j = session.post(f"{args.brain_url}/events", json=payload, timeout=10).json()
                            disp, color = _display_from_brain(j)
                            locked = j.get("label") in ("Visitor", "Employee")
                        except Exception as e:  # noqa: BLE001 — keep the loop alive
                            print(f"    → POST failed: {e}")
                    if disp is None:             # offline / no --emit → local gallery
                        res = identifier.identify_features(
                            face_emb, body_emb, detection_conf=box[4],
                            face_threshold=cam.match_threshold)
                        if res["label"] == "Employee":
                            disp, color, locked = f"Employee {res['name'] or res['person_id'] or ''}".strip(), _COL_EMP, True
                        elif res["label"] == "Visitor":
                            disp, color, locked = f"Visitor {res['person_id'] or ''}".strip(), _COL_VIS, True
                        else:
                            disp, color = "Unknown", _COL_UNKNOWN
                    with state_lock:
                        t.label, t.color = disp, color
                        if locked:
                            t.resolved = True    # lock: never re-classified → no churn, one id
                    feat = ("F" if face_emb is not None else "-") + ("B" if body_emb is not None else "-")
                    print(f"[{uid}] track#{t.id} -> {disp}  (det {box[4]:.2f} feat {feat})")
            if not ran:
                time.sleep(0.1)

    if id_cams:
        threading.Thread(target=identity_worker, daemon=True).start()

    last_detect = {c.camera_uid: 0.0 for c in cameras}
    print(f"Running (boxes ~{1/detect_interval:.0f} fps/cam, tracker-based identity, "
          f"preview -> {SHM_DIR}). Ctrl-C to stop.")
    try:
        while True:
            did_work = False
            for stream in streams:
                uid = stream.camera_uid
                cam = roles[uid]
                now = time.time()
                if now - last_detect[uid] < detect_interval:
                    continue
                frame = stream.get_frame()
                if frame is None:
                    continue
                last_detect[uid] = now
                did_work = True

                boxes = detector.detect(frame, conf=args.conf, normalized=False)
                with state_lock:
                    frames[uid] = frame
                    tks = trackers[uid].update(boxes)
                if cam.runs_identity:
                    # unresolved tracks show 'Unknown' (red) until identity locks them
                    annos = [(*t.box[:4], t.label or "Unknown", t.color or _COL_UNKNOWN) for t in tks]
                else:
                    annos = [(*t.box[:4], "person", _COL_PERSON) for t in tks]
                set_annotations(uid, annos)

            if not did_work:
                time.sleep(0.02)                  # avoid busy-spin when nothing is due
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        stop_flag.set()
        for s in streams:
            s.stop()


if __name__ == "__main__":
    main()
