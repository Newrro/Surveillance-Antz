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
                 "label", "color", "resolved", "last_resolve", "emitted",
                 "snap_path", "face_path",
                 "best_face_emb", "best_body_emb", "best_face_q", "emit_face_q", "probes",
                 "face_emb_sum", "face_w_sum", "emit_face_w", "best_shot_q")

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
        self.snap_path = None      # body-crop snapshot saved once per track (dedupe)
        self.face_path = None      # face-crop snapshot saved once per track
        # ── quality-weighted temporal face pooling (Part 2, Phase 3b) ──
        # A single "best shot" is still one noisy frame, so the SAME person's face
        # can enroll as several gallery entries that don't re-match (measured: one
        # person fragmenting into 4+ Visitor ids). Instead we POOL every probe's
        # face embedding into a quality-weighted centroid and identify/enroll from
        # that — a far more stable vector that re-matches one gallery entry.
        self.face_emb_sum = None    # Σ (quality · face_vec) over probes (np.float32[512])
        self.face_w_sum = 0.0       # Σ quality — pooled vec = normalize(sum), weight = this
        self.emit_face_w = 0.0      # face_w_sum at last emit (re-emit when pool grows enough)
        self.best_face_emb = None   # highest-quality single shot (kept only for reference)
        self.best_body_emb = None   # body embedding paired with the best face shot
        self.best_face_q = 0.0      # quality of best_face_emb (drives the saved face thumbnail)
        self.emit_face_q = 0.0      # (legacy) quality of the face we last emitted
        self.probes = 0             # number of heavy identity attempts on this track
        self.best_shot_q = 0.0      # sharpness×size of the best body-only snapshot (faceless tracks)


class MotionTracker:
    """Kalman-motion tracker (Oc-SORT via BoxMOT) behind the SimpleTracker
    interface. It keeps person IDs stable through occlusion / brief exits far
    better than IoU-only association, so the same person stops being re-minted as
    a new track (the root of the 'one person → many Unknowns' fragmentation).

    No re-ID model and no GPU — pure motion (Kalman + observation-centric
    recovery) — so it costs ~nothing on the 6 GB card. Cross-track / re-entry
    re-linking is handled downstream by the Brain's constrained body merge, which
    reuses the OSNet embeddings we already extract (no second re-ID model).

    Exposes the same surface the pipeline uses: `.tracks` (list of Track, each
    carrying identity state that persists with its stable id) and
    `update(boxes, frame) -> visible tracks`.
    """

    def __init__(self, det_thresh=None, max_age=None, min_hits=None, iou_threshold=None):
        import os
        import numpy as np  # noqa: F401 — used in update
        from boxmot.trackers import OcSort
        self._oc = OcSort(
            det_thresh=float(os.environ.get("TRACK_DET_THRESH", det_thresh or 0.3)),
            max_age=int(os.environ.get("TRACK_MAX_AGE", max_age or 60)),      # frames a track survives an occlusion
            min_hits=int(os.environ.get("TRACK_MIN_HITS", min_hits or 3)),    # frames before a track is confirmed
            iou_threshold=float(os.environ.get("TRACK_IOU", iou_threshold or 0.3)),
        )
        self._by_id = {}        # stable boxmot id -> Track (identity state lives here)
        self._drop_after = int(os.environ.get("TRACK_MAX_AGE", max_age or 60)) * 3

    def update(self, boxes, frame):
        import numpy as np
        if boxes:
            dets = np.array([[b[0], b[1], b[2], b[3], b[4], 0] for b in boxes], dtype=float)
        else:
            dets = np.empty((0, 6), dtype=float)
        rows = np.asarray(self._oc.update(dets, frame))  # [x1,y1,x2,y2,id,conf,cls,detidx]
        present = set()
        visible = []
        for r in rows:
            x1, y1, x2, y2, tid = float(r[0]), float(r[1]), float(r[2]), float(r[3]), int(r[4])
            conf = float(r[5]) if len(r) > 5 else 0.0
            present.add(tid)
            t = self._by_id.get(tid)
            if t is None:
                t = Track(tid, (x1, y1, x2, y2, conf))     # new stable id → fresh identity state
                self._by_id[tid] = t
            else:
                t.box = (x1, y1, x2, y2, conf)
                t.hits += 1
                t.misses = 0
            visible.append(t)
        # age out absent ids; keep them a while so identity state survives a blink
        for tid, t in list(self._by_id.items()):
            if tid not in present:
                t.misses += 1
                if t.misses > self._drop_after:
                    del self._by_id[tid]
        return visible

    @property
    def tracks(self):
        return list(self._by_id.values())


class SimpleTracker:
    def __init__(self, iou_thresh=0.25, max_misses=40):
        self.tracks = []
        self.next_id = 1
        self.iou_thresh = iou_thresh
        self.max_misses = max_misses   # frames a track survives without a detection

    def update(self, boxes, frame=None):
        """Associate `boxes` (list of (x1,y1,x2,y2,conf)) to existing tracks by
        greedy IoU. Returns the list of currently-visible tracks (misses == 0).
        `frame` is accepted (ignored) so this matches MotionTracker's signature."""
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
