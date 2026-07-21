"""snapshots.py — person/face/full-scene crop + snapshot writers, and the
   quality-weighted face pooling. Extracted from pipeline.py."""

import os
import shutil

import cv2
import numpy as np

from ppl_colors import _COL_VIS

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORAGE_IMG = os.path.join(REPO_ROOT, "storage", "img")
# Tier-A durable per-identity photos live HERE — deliberately OUTSIDE storage/img
# so the storage pruner (which only walks storage/img) never deletes them.
PROFILES_DIR = os.path.join(REPO_ROOT, "storage", "profiles")
MIN_CROP_SIDE = 24  # skip crops too tiny to embed reliably
FULL_FRAME_MAX_W = int(os.environ.get("FULL_FRAME_MAX_W", "1280"))
FULL_FRAME_QUALITY = int(os.environ.get("FULL_FRAME_QUALITY", "88"))


def _abs(relpath):
    if not relpath:
        return None
    return relpath if os.path.isabs(relpath) else os.path.join(REPO_ROOT, relpath)


def save_profile_photo(identity_id, face_path=None, snap_path=None):
    """Tier-A durable per-identity photo → storage/profiles/<identity_id>.jpg.

    This is the ONE image the UI shows for a person, and it is NEVER pruned (the
    folder is outside storage/img), so a face never disappears from the Report/
    Records when the per-sighting frames age out. Prefer the aligned FACE crop; a
    face always refreshes the profile, while a body-only shot only FILLS an empty
    slot (so we never downgrade a good face profile to a back/body shot).

    Best-effort — copies from the already-saved snapshot files; never raises."""
    if identity_id is None:
        return None
    # A manual/enrolled photo is PINNED with a sibling '<id>.lock' marker (written by
    # the Brain on enrollment or a profile-photo edit). Never overwrite it with a
    # captured face — the chosen photo is the person's fixed avatar. Mirrors
    # surveillance_brain/services/media_paths.py:is_locked.
    if os.path.exists(os.path.join(PROFILES_DIR, f"{identity_id}.lock")):
        return None
    try:
        os.makedirs(PROFILES_DIR, exist_ok=True)
        dst = os.path.join(PROFILES_DIR, f"{identity_id}.jpg")
        face = _abs(face_path)
        if face and os.path.exists(face):
            shutil.copyfile(face, dst)          # a real face → always refresh
            return dst
        body = _abs(snap_path)
        if body and os.path.exists(body) and not os.path.exists(dst):
            shutil.copyfile(body, dst)          # body-only → fill an empty slot only
            return dst
    except OSError:
        pass
    return None


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
