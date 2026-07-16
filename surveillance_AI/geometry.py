"""geometry.py — box IoU / overlap / occlusion + label picking. Extracted from
   pipeline.py; pure functions (no pipeline state)."""

import os

from ppl_colors import _COL_PERSON

# Skip identity on a track whose box is heavily overlapped by ANOTHER person's box
# (the crop would contain a neighbour → wrong face). Fraction of THIS box covered.
OCCLUSION_OVERLAP = float(os.environ.get("OCCLUSION_OVERLAP", "0.45"))


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
