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
from datetime import datetime, timezone

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import cv2
import numpy as np

# flat sibling modules
from detector import PersonDetector, draw_boxes
from feature_id.identify import Identifier

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
from surveillance_Camera_config import load_cameras  # noqa: E402

import nvr_stream as nvr  # noqa: E402

STORAGE_IMG = os.path.join(REPO_ROOT, "storage", "img")
MIN_CROP_SIDE = 24  # skip crops too tiny to embed reliably


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
    """Save the crop under storage/img/<camera_uid>/ and return the contract path."""
    out_dir = os.path.join(STORAGE_IMG, camera_uid)
    os.makedirs(out_dir, exist_ok=True)
    fname = f"{stamp_ms}_{idx}.jpg"
    cv2.imwrite(os.path.join(out_dir, fname), crop_bgr)
    return f"storage/img/{camera_uid}/{fname}"


def build_payload(camera_uid, score, face_embedding, body_embedding, snapshot_path, stamp_ms, idx):
    """Part 1 → Part 2 detection payload (contracts/part1_to_part2.event.schema.json)."""
    return {
        "detection_id": f"{camera_uid}-{stamp_ms}-{idx}",
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
    last_pass = {c.camera_uid: 0.0 for c in cameras}
    roles = {c.camera_uid: c for c in cameras}

    win = "Part 1 — pipeline"
    if args.show:
        cv2.namedWindow(win, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)

    print("Running. Ctrl-C to stop.")
    try:
        while True:
            for stream in streams:
                uid = stream.camera_uid
                cam = roles[uid]
                now = time.time()
                if now - last_pass[uid] < args.cooldown:
                    continue
                frame = stream.get_frame()
                if frame is None:
                    continue

                boxes = detector.detect(frame, conf=args.conf, normalized=False)
                if not boxes:
                    continue
                last_pass[uid] = now
                vis = frame.copy() if args.show else None

                # ── DETECT-ONLY cameras: no feature extraction ──
                if not cam.runs_identity:
                    print(f"[{uid}] detect-only: {len(boxes)} person(s)")
                    if args.show:
                        draw_boxes(vis, boxes, normalized=False)
                        cv2.putText(vis, f"{uid} [detect]  {len(boxes)} ppl",
                                    (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2)
                        cv2.imshow(win, vis)
                    continue

                # ── IDENTIFY cameras: detect → (segment) → embed → ID ──
                if segmenter is not None:
                    segmenter.set_frame(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

                stamp_ms = int(now * 1000)
                for i, box in enumerate(boxes):
                    mask = segmenter.mask_for_box(box) if segmenter is not None else None
                    crop = crop_person(frame, box, mask=mask)
                    if crop is None:
                        continue

                    face_emb, body_emb = identifier.extract(crop)
                    result = identifier.identify_features(face_emb, body_emb,
                                                          face_threshold=cam.match_threshold)
                    snap = save_snapshot(uid, crop, stamp_ms, i)

                    tag = "NEW " if result["is_new"] else ("learn " if result["learned"] else "")
                    print(f"[{uid}] {result['label']:8} {result['person_id']}  "
                          f"conf={result['confidence_pct']}% via {result['matched_by']:4}  {tag}"
                          f"(det {box[4]:.2f})")

                    if args.emit and result["error"] != "no_features":
                        payload = build_payload(uid, box[4], face_emb, body_emb, snap, stamp_ms, i)
                        try:
                            r = session.post(f"{args.brain_url}/events", json=payload, timeout=10)
                            print(f"    → POST /events {r.status_code}")
                        except Exception as e:  # noqa: BLE001 — keep the loop alive
                            print(f"    → POST failed: {e}")

                    if args.show:
                        x1, y1, x2, y2 = box[:4]
                        cv2.rectangle(vis, (int(x1), int(y1)), (int(x2), int(y2)), (0, 200, 0), 2)
                        cv2.putText(vis, f"{result['label']} {result['confidence_pct']}%",
                                    (int(x1), max(14, int(y1) - 6)),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

                if args.show:
                    cv2.putText(vis, f"{uid} [identify]",
                                (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    cv2.imshow(win, vis)

            if args.show and (cv2.waitKey(1) & 0xFF) == ord('q'):
                break
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        for s in streams:
            s.stop()
        if args.show:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
