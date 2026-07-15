# ─────────────────────────────────────────────
#  SCRFD FACE DETECTOR  — face bbox + 5 landmarks (onnxruntime)
# ─────────────────────────────────────────────
# Replaces MTCNN as the FACE DETECTOR stage. Like MTCNN it only *locates* faces
# and their 5 landmarks (eyes / nose / mouth corners) — recognition is still
# AdaFace. SCRFD is faster, more robust on small/angled CCTV faces, and needs no
# torch (pure onnxruntime), so it runs on the low-priority GPU stream or CPU.
#
# Model: scrfd_500m_bnkps.onnx (SCRFD 500 MFLOPs, "bnkps" = bbox + 5 keypoints).
# Output structure: 3 FPN strides [8, 16, 32], 2 anchors/location, three heads
# per stride — score [N,1], bbox [N,4], kps [N,10]. Decoding below is the
# standard SCRFD anchor decode (same maths as insightface, vendored so we carry
# no insightface dependency).

import cv2
import numpy as np
import onnxruntime as ort


def _distance2bbox(points, distance):
    """Decode (l, t, r, b) distances from an anchor centre into x1,y1,x2,y2."""
    x1 = points[:, 0] - distance[:, 0]
    y1 = points[:, 1] - distance[:, 1]
    x2 = points[:, 0] + distance[:, 2]
    y2 = points[:, 1] + distance[:, 3]
    return np.stack([x1, y1, x2, y2], axis=-1)


def _distance2kps(points, distance):
    """Decode 5 keypoint (dx, dy) offsets from an anchor centre → [N, 10]."""
    preds = []
    for i in range(0, distance.shape[1], 2):
        px = points[:, 0] + distance[:, i]
        py = points[:, 1] + distance[:, i + 1]
        preds.append(px)
        preds.append(py)
    return np.stack(preds, axis=-1)


def _nms(dets, thresh):
    """Plain greedy NMS over [x1,y1,x2,y2,score] rows; returns kept indices."""
    x1, y1, x2, y2, scores = dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        iou = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[np.where(iou <= thresh)[0] + 1]
    return keep


class SCRFD:
    """SCRFD face detector. detect(img_bgr) -> (boxes[N,4] xyxy, scores[N],
    kpss[N,5,2]) in the input image's own pixel coordinates (empty arrays when
    no face clears the score threshold)."""

    _FEAT_STRIDE_FPN = (8, 16, 32)
    _FMC = 3            # number of feature-map strides / heads-per-branch
    _NUM_ANCHORS = 2

    def __init__(self, model_path, input_size=640, conf_thresh=0.5,
                 nms_thresh=0.4, device="cuda"):
        providers = (["CUDAExecutionProvider", "CPUExecutionProvider"]
                     if str(device).startswith("cuda") else ["CPUExecutionProvider"])
        try:
            self.session = ort.InferenceSession(model_path, providers=providers)
        except Exception:                       # cuDNN/provider hiccup → CPU
            self.session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        self.output_names = [o.name for o in self.session.get_outputs()]
        self.input_size = (int(input_size), int(input_size))   # (w, h), square
        self.conf_thresh = float(conf_thresh)
        self.nms_thresh = float(nms_thresh)
        self._center_cache = {}

    def detect(self, img_bgr):
        H, W = img_bgr.shape[:2]
        input_w, input_h = self.input_size

        # Aspect-preserving letterbox into a top-left-anchored square (insightface
        # convention). det_scale maps model coords back to the original image.
        im_ratio, model_ratio = H / float(W), input_h / float(input_w)
        if im_ratio > model_ratio:
            new_h = input_h
            new_w = int(new_h / im_ratio)
        else:
            new_w = input_w
            new_h = int(new_w * im_ratio)
        det_scale = new_h / float(H)
        resized = cv2.resize(img_bgr, (new_w, new_h))
        det_img = np.zeros((input_h, input_w, 3), dtype=np.uint8)
        det_img[:new_h, :new_w, :] = resized

        # (x - 127.5) / 128, BGR->RGB, NCHW.
        blob = cv2.dnn.blobFromImage(
            det_img, 1.0 / 128.0, (input_w, input_h), (127.5, 127.5, 127.5), swapRB=True)
        outs = self.session.run(self.output_names, {self.input_name: blob})

        scores_list, bboxes_list, kpss_list = [], [], []
        for idx, stride in enumerate(self._FEAT_STRIDE_FPN):
            scores = outs[idx]
            bbox_preds = outs[idx + self._FMC] * stride
            kps_preds = outs[idx + self._FMC * 2] * stride
            h, w = input_h // stride, input_w // stride
            key = (h, w, stride)
            centers = self._center_cache.get(key)
            if centers is None:
                ax, ay = np.meshgrid(np.arange(w), np.arange(h))
                centers = np.stack([ax, ay], axis=-1).astype(np.float32).reshape(-1, 2) * stride
                if self._NUM_ANCHORS > 1:
                    centers = np.stack([centers] * self._NUM_ANCHORS, axis=1).reshape(-1, 2)
                if len(self._center_cache) < 100:
                    self._center_cache[key] = centers
            pos = np.where(scores.ravel() >= self.conf_thresh)[0]
            if pos.size == 0:
                continue
            bboxes = _distance2bbox(centers, bbox_preds)[pos]
            kpss = _distance2kps(centers, kps_preds)[pos].reshape(-1, 5, 2)
            scores_list.append(scores.ravel()[pos])
            bboxes_list.append(bboxes)
            kpss_list.append(kpss)

        if not scores_list:
            return (np.zeros((0, 4), np.float32),
                    np.zeros((0,), np.float32),
                    np.zeros((0, 5, 2), np.float32))

        scores = np.concatenate(scores_list)
        bboxes = np.concatenate(bboxes_list) / det_scale
        kpss = np.concatenate(kpss_list) / det_scale
        order = scores.argsort()[::-1]
        pre_det = np.hstack([bboxes, scores[:, None]]).astype(np.float32)[order]
        kpss = kpss[order]
        keep = _nms(pre_det, self.nms_thresh)
        return pre_det[keep, :4], pre_det[keep, 4], kpss[keep]
