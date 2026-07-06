"""
detector.py — person detection (Part 1, Perception).

Pluggable detector backend, selected by the DETECTOR_MODEL env var (or the
`model=` arg). Default is RT-DETR — a transformer detector that's far more
accurate/reliable than the old lightweight model (it rejects the backlit-clutter
false positives the MobileNet detector produced), and real-time on a GPU.

  DETECTOR_MODEL=rtdetr               RT-DETR R50 (HuggingFace, default) — accurate
  DETECTOR_MODEL=fasterrcnn_resnet50  torchvision FasterRCNN-ResNet50-FPN v2
  DETECTOR_MODEL=fasterrcnn_mobilenet torchvision FasterRCNN-MobileNetV3 (fast/light)

Every backend returns the same thing from detect(): a list of person boxes with
the same false-positive shape filters (min height + upright aspect) that made the
outdoor gate feed reliable. On the RTX 4060 install a CUDA torch build; the code
auto-detects and uses the GPU.
"""
import os

import cv2
import numpy as np
import torch

PERSON_CLASS = 1        # COCO 'person' label id in torchvision detection models
# Detector input is downscaled to this longest side. Lower = faster (less lag)
# at some cost to small/distant detections. Tunable via DET_MAX_SIDE.
DET_MAX_SIDE = int(os.environ.get("DET_MAX_SIDE", "800"))

# false-positive filters (the "leaves / clutter" problem on outdoor cameras)
MIN_HEIGHT_FRAC = 0.05  # a person box must be >= 5% of frame height
MIN_ASPECT = 1.1        # height/width >= this (people are upright, not wide)

DETECTOR_MODEL = os.environ.get("DETECTOR_MODEL", "rtdetr")
RTDETR_CHECKPOINT = os.environ.get("RTDETR_CHECKPOINT", "PekingU/rtdetr_r50vd")


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
    if h < MIN_HEIGHT_FRAC * frame_h:   # too small → distant noise / clutter
        return False
    if h / w < MIN_ASPECT:              # wider than tall → not an upright person
        return False
    return True


class PersonDetector:
    """Loads the chosen detector once; detect() returns person boxes for a BGR frame."""

    def __init__(self, model=None, device=None, det_max_side=DET_MAX_SIDE):
        self.model_name = (model or DETECTOR_MODEL).lower()
        self.device = device or pick_device()
        self.det_max_side = det_max_side
        print(f"[detector] loading '{self.model_name}' on {self.device} ...")
        if self.model_name == "rtdetr":
            self._init_rtdetr()
        elif self.model_name in ("fasterrcnn_resnet50", "fasterrcnn_mobilenet"):
            self._init_frcnn()
        else:
            raise ValueError(f"unknown DETECTOR_MODEL '{self.model_name}'")
        print("[detector] ready.")

    # ── backends ──────────────────────────────────────────────
    def _init_rtdetr(self):
        from transformers import RTDetrForObjectDetection, RTDetrImageProcessor
        self._proc = RTDetrImageProcessor.from_pretrained(RTDETR_CHECKPOINT)
        self._model = RTDetrForObjectDetection.from_pretrained(RTDETR_CHECKPOINT).eval().to(self.device)
        id2label = self._model.config.id2label
        self._person_ids = {i for i, l in id2label.items() if str(l).lower() == "person"}

    def _init_frcnn(self):
        from torchvision.models import detection as D
        if self.model_name == "fasterrcnn_resnet50":
            w = D.FasterRCNN_ResNet50_FPN_V2_Weights.DEFAULT
            self._model = D.fasterrcnn_resnet50_fpn_v2(weights=w).eval().to(self.device)
        else:
            w = D.FasterRCNN_MobileNet_V3_Large_FPN_Weights.DEFAULT
            self._model = D.fasterrcnn_mobilenet_v3_large_fpn(weights=w).eval().to(self.device)

    def _infer(self, frame_bgr, conf):
        """Backend-specific → list of (x1,y1,x2,y2,score) person boxes in absolute px."""
        h, w = frame_bgr.shape[:2]
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        if self.model_name == "rtdetr":
            from PIL import Image
            inputs = self._proc(images=Image.fromarray(rgb), return_tensors="pt").to(self.device)
            with torch.inference_mode():
                out = self._model(**inputs)
            res = self._proc.post_process_object_detection(
                out, target_sizes=torch.tensor([[h, w]]).to(self.device), threshold=min(conf, 0.25))[0]
            boxes = []
            for score, label, box in zip(res["scores"], res["labels"], res["boxes"]):
                if int(label) not in self._person_ids or float(score) < conf:
                    continue
                x1, y1, x2, y2 = box.detach().cpu().numpy()
                boxes.append((float(x1), float(y1), float(x2), float(y2), float(score)))
            return boxes
        # FasterRCNN family: downscale for speed, scale boxes back
        scale = self.det_max_side / max(h, w) if max(h, w) > self.det_max_side else 1.0
        small = cv2.resize(rgb, (int(w * scale), int(h * scale))) if scale < 1 else rgb
        t = torch.from_numpy(small).permute(2, 0, 1).float().div(255).to(self.device)
        with torch.inference_mode():
            out = self._model([t])[0]
        boxes = []
        for box, label, score in zip(out["boxes"], out["labels"], out["scores"]):
            if int(label) != PERSON_CLASS or float(score) < conf:
                continue
            x1, y1, x2, y2 = (box.detach().cpu().numpy() / scale)
            boxes.append((float(x1), float(y1), float(x2), float(y2), float(score)))
        return boxes

    # ── public API (same for every backend) ───────────────────
    def detect(self, frame_bgr, conf=0.50, normalized=False):
        """Return person boxes.
        normalized=False → (x1,y1,x2,y2,score) absolute pixels.
        normalized=True  → coordinates divided by frame width/height (0..1).
        """
        h, w = frame_bgr.shape[:2]
        out = []
        for (x1, y1, x2, y2, score) in self._infer(frame_bgr, conf):
            if not _personlike(x1, y1, x2, y2, h):
                continue
            if normalized:
                out.append((x1 / w, y1 / h, x2 / w, y2 / h, score))
            else:
                out.append((x1, y1, x2, y2, score))
        return out


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
