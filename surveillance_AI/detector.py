"""
detector.py — person detection (Part 1, Perception).

FasterRCNN-MobileNetV3 person boxes with the false-positive filters that made
the live gate feed reliable (min height + upright aspect ratio, to reject the
"leaves / distant clutter" detections). Extracted from the working
sam2_people_live.py / sam2_multicam.py so both the viewer and the headless
pipeline share one implementation.

Detection runs on a downscaled copy (DET_MAX_SIDE) for speed; boxes are scaled
back to full-resolution pixel coordinates.
"""
import cv2
import numpy as np
import torch
from torchvision.models.detection import (
    fasterrcnn_mobilenet_v3_large_fpn,
    FasterRCNN_MobileNet_V3_Large_FPN_Weights,
)

PERSON_CLASS = 1        # COCO 'person' label in torchvision detection models
DET_MAX_SIDE = 800      # detection input is downscaled to this longest side (speed)

# false-positive filters (the "leaves" problem on the outdoor gate cameras)
MIN_HEIGHT_FRAC = 0.05  # a person box must be >= 5% of frame height
MIN_ASPECT = 1.1        # height/width >= this (people are upright, not wide)


def pick_device():
    if torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def _personlike(x1, y1, x2, y2, frame_h):
    w, h = x2 - x1, y2 - y1
    if w <= 0 or h <= 0:
        return False
    if h < MIN_HEIGHT_FRAC * frame_h:   # too small → distant noise / leaf cluster
        return False
    if h / w < MIN_ASPECT:              # wider than tall → not an upright person
        return False
    return True


class PersonDetector:
    """Loads the detector once; detect() returns person boxes for a BGR frame."""

    def __init__(self, device=None, det_max_side=DET_MAX_SIDE):
        self.device = device or pick_device()
        self.det_max_side = det_max_side
        print(f"[detector] loading FasterRCNN-MobileNetV3 on {self.device} ...")
        weights = FasterRCNN_MobileNet_V3_Large_FPN_Weights.DEFAULT
        self.model = fasterrcnn_mobilenet_v3_large_fpn(weights=weights).eval().to(self.device)
        print("[detector] ready.")

    def detect(self, frame_bgr, conf=0.50, normalized=False):
        """Return a list of person boxes.

        normalized=False → (x1, y1, x2, y2, score) in absolute pixels.
        normalized=True  → coordinates divided by frame width/height (0..1).
        """
        h, w = frame_bgr.shape[:2]
        scale = self.det_max_side / max(h, w) if max(h, w) > self.det_max_side else 1.0
        small = cv2.resize(frame_bgr, (int(w * scale), int(h * scale))) if scale < 1 else frame_bgr
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        t = torch.from_numpy(rgb).permute(2, 0, 1).float().div(255).to(self.device)
        with torch.inference_mode():
            out = self.model([t])[0]

        boxes = []
        for box, label, score in zip(out["boxes"], out["labels"], out["scores"]):
            if int(label) != PERSON_CLASS or float(score) < conf:
                continue
            x1, y1, x2, y2 = (box.detach().cpu().numpy() / scale)
            if not _personlike(x1, y1, x2, y2, h):
                continue
            if normalized:
                boxes.append((x1 / w, y1 / h, x2 / w, y2 / h, float(score)))
            else:
                boxes.append((float(x1), float(y1), float(x2), float(y2), float(score)))
        return boxes


def draw_boxes(img, boxes, normalized=False, thickness=2, with_score=True):
    """Draw person boxes on `img` in place. Set normalized=True if boxes are 0..1."""
    H, W = img.shape[:2]
    for (x1, y1, x2, y2, score) in boxes:
        if normalized:
            x1, y1, x2, y2 = x1 * W, y1 * H, x2 * W, y2 * H
        p1, p2 = (int(x1), int(y1)), (int(x2), int(y2))
        cv2.rectangle(img, p1, p2, (0, 200, 0), thickness)
        if with_score:
            cv2.putText(img, f"{score:.2f}", (p1[0], max(12, p1[1] - 5)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
