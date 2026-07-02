# ─────────────────────────────────────────────
#  FACE EXTRACTOR  — the PRIMARY identity signal (AdaFace, NOT InsightFace)
# ─────────────────────────────────────────────
# Turns a person crop into a 512-number FACE embedding:
#   1. MTCNN (facenet-pytorch) finds the largest face + its 5 landmarks.
#   2. Align to the standard ArcFace 112x112 template (similarity transform).
#   3. AdaFace IR-101 (WebFace12M) produces the embedding.
#
# Returns None when there is no usable face (too small / not frontal / occluded) —
# the caller then falls back to the body ReID embedding. Only the recognition model
# is AdaFace; MTCNN is used purely as a face detector.

import cv2
import numpy as np
import torch
from facenet_pytorch import MTCNN

from . import config
from .adaface_net import build_model

# ArcFace 5-point reference landmarks (left eye, right eye, nose, mouth L, mouth R)
# for a 112x112 aligned face — the alignment AdaFace was trained on.
_ARCFACE_112 = np.float32([
    [38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
    [41.5493, 92.3655], [70.7299, 92.2041],
])


def _align_112(img_bgr, landmarks5):
    """Warp the face to a 112x112 ArcFace-aligned crop using its 5 landmarks."""
    M, _ = cv2.estimateAffinePartial2D(
        np.asarray(landmarks5, np.float32), _ARCFACE_112, method=cv2.LMEDS)
    if M is None:
        return None
    return cv2.warpAffine(img_bgr, M, (112, 112), borderValue=0.0)


class FaceExtractor:
    """AdaFace face embedding. embed(person_bgr) -> L2-normalized 512-vec, or None
    when no usable face is found (caller falls back to body ReID)."""

    def __init__(self):
        self.device = config.DEVICE
        print(f"[face] loading MTCNN detector + AdaFace ({config.FACE_BACKBONE}) on {self.device} ...")
        self.mtcnn = MTCNN(keep_all=True, min_face_size=config.FACE_MIN_SIZE, device=self.device)
        self.model = build_model(config.FACE_BACKBONE).eval().to(self.device)
        self.model.load_state_dict(torch.load(config.FACE_WEIGHTS, map_location=self.device))
        print("[face] ready.")

    def _largest_face_landmarks(self, rgb):
        boxes, probs, lms = self.mtcnn.detect(rgb, landmarks=True)
        if boxes is None:
            return None
        best, best_area = None, 0.0
        for b, p, lm in zip(boxes, probs, lms):
            if p is None or p < config.FACE_DET_CONF:
                continue
            area = (b[2] - b[0]) * (b[3] - b[1])
            if area > best_area:
                best, best_area = lm, area
        return best

    def embed(self, person_bgr):
        if person_bgr is None or person_bgr.size == 0:
            return None
        rgb = cv2.cvtColor(person_bgr, cv2.COLOR_BGR2RGB)   # MTCNN wants RGB
        lm = self._largest_face_landmarks(rgb)
        if lm is None:
            return None
        aligned = _align_112(person_bgr, lm)                 # AdaFace wants BGR
        if aligned is None:
            return None
        x = ((aligned.astype(np.float32) / 255.0) - 0.5) / 0.5
        t = torch.from_numpy(x.transpose(2, 0, 1)[None]).float().to(self.device)
        with torch.no_grad():
            feat, _ = self.model(t)
        v = feat.cpu().numpy()[0].astype("float32")
        n = np.linalg.norm(v)
        return v / n if n > 0 else None
