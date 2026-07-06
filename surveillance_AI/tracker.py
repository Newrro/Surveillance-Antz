"""
tracker.py — a minimal per-camera IoU tracker.

Why it exists: without tracking, identity is decided independently on every frame,
so the SAME person flickers Unknown → Visitor → Unknown as the detection
confidence wobbles around the gate, and every qualifying frame that doesn't
re-match spawns a NEW database identity. A tracker gives each person a stable
track for as long as they're in view, so identity is resolved ONCE per track and
then locked — one person, one label, one database id.

Greedy IoU association, no external deps. Good enough for a handful of people per
camera at a few fps.
"""


def _iou(a, b):
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


class Track:
    __slots__ = ("id", "box", "hits", "misses",
                 "label", "color", "resolved", "last_resolve", "emitted")

    def __init__(self, tid, box):
        self.id = tid
        self.box = box              # (x1, y1, x2, y2, conf)
        self.hits = 1
        self.misses = 0
        self.label = None          # display text once identity resolves
        self.color = None
        self.resolved = False      # True once locked to a real identity (Visitor/Employee)
        self.last_resolve = 0.0    # last identity attempt (time.time())
        self.emitted = False       # at least one /events POST sent for this track


class SimpleTracker:
    def __init__(self, iou_thresh=0.25, max_misses=40):
        self.tracks = []
        self.next_id = 1
        self.iou_thresh = iou_thresh
        self.max_misses = max_misses   # frames a track survives without a detection

    def update(self, boxes):
        """Associate `boxes` (list of (x1,y1,x2,y2,conf)) to existing tracks by
        greedy IoU. Returns the list of currently-visible tracks (misses == 0)."""
        unmatched = set(range(len(boxes)))
        # Match existing tracks to the best available detection.
        for t in sorted(self.tracks, key=lambda tr: tr.hits, reverse=True):
            best, best_iou = -1, self.iou_thresh
            for j in unmatched:
                v = _iou(t.box, boxes[j])
                if v > best_iou:
                    best, best_iou = j, v
            if best >= 0:
                t.box = boxes[best]
                t.hits += 1
                t.misses = 0
                unmatched.discard(best)
            else:
                t.misses += 1
        # New detections become new tracks.
        for j in unmatched:
            self.tracks.append(Track(self.next_id, boxes[j]))
            self.next_id += 1
        # Drop stale tracks.
        self.tracks = [t for t in self.tracks if t.misses <= self.max_misses]
        return [t for t in self.tracks if t.misses == 0]
