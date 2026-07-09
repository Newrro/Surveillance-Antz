# ─────────────────────────────────────────────
#  THE DECISION  — face-primary, body-fallback identity
# ─────────────────────────────────────────────
# Give it a person crop (or pre-extracted embeddings) and it returns:
#   {person_id, label, name, confidence, confidence_pct, matched_by, is_new,
#    learned, error}
#
# Flow (mirrors the Brain's design — face first, body fallback):
#   extract face (AdaFace) + body (OSNet)
#   -> FACE match >= FACE_MATCH_THRESHOLD ? recognise by face
#   -> else BODY match >= BODY_MATCH_THRESHOLD ? recognise by body
#   -> else auto-enroll a new Visitor (with whatever embeddings we have)
# On a recognise that isn't near-perfect, LEARN the new view (progressive confidence).

from . import config
from .extractor import Extractor
from .face_extractor import FaceExtractor
from .gallery import Gallery


class Identifier:
    def __init__(self):
        self.body = Extractor()        # OSNet body ReID (fallback signal)
        self.face = FaceExtractor()    # AdaFace face embedding (primary signal)
        self.gallery = Gallery()
        self.extractor = self.body     # backward-compat alias
        self.last_face_crop = None     # aligned face image from the last extract()
        self.last_face_quality = 0.0   # AdaFace-norm quality of that face (best-shot)

    def _too_small(self, crop):
        if crop is None or crop.size == 0:
            return True
        h, w = crop.shape[:2]
        return h < config.MIN_CROP_SIDE or w < config.MIN_CROP_SIDE

    def extract(self, person_bgr, body_bgr=None):
        """Return (face_emb_or_None, body_emb_or_None) for a person crop.

        `body_bgr` (optional) is a background-blanked crop of the SAME person for
        the body ReID vector, so OSNet describes the person and not the shared
        camera scene. Face always uses the raw crop — alignment needs true pixels.
        """
        face, self.last_face_crop = self.face.embed_with_face(person_bgr)
        self.last_face_quality = self.face.last_norm if face is not None else 0.0
        bb = person_bgr if body_bgr is None else body_bgr
        body = None if self._too_small(bb) else self.body.embed(bb)
        return face, body

    def identify(self, person_bgr, detection_conf=None, face_threshold=None):
        face_emb, body_emb = self.extract(person_bgr)
        return self.identify_features(face_emb, body_emb, detection_conf, face_threshold)

    def identify_features(self, face_emb, body_emb, detection_conf=None, face_threshold=None):
        """Decide identity from already-extracted embeddings (so the pipeline can
        compute them once and reuse them to emit to the Brain).

        Flow (mirrors the Brain):
          detection weak / no features  → Unknown (we're not sure — don't guess)
          otherwise search the gallery:  face first, then body
            match       → that person's label (Employee / Visitor) + confidence
            no match    → a NEW Visitor (confidently a person, just not seen before)

        `detection_conf` is the detector's score; `face_threshold` overrides
        FACE_MATCH_THRESHOLD for this call (per-camera stricter/looser)."""
        # ── QUALITY GATE → Unknown when we're not sure enough to identify ──
        if detection_conf is not None and detection_conf < config.DETECTION_CONF_THRESHOLD:
            return self._result(None, config.LABEL_UNKNOWN, "", 0.0,
                                matched_by="none", error="low_confidence")
        if face_emb is None and body_emb is None:
            return self._result(None, config.LABEL_UNKNOWN, "", 0.0,
                                matched_by="none", error="no_features")

        face_thr = config.FACE_MATCH_THRESHOLD if face_threshold is None else face_threshold

        # ── FACE (primary) ──
        if face_emb is not None:
            p, sim = self.gallery.best_match_face(face_emb)
            if p is not None and sim >= face_thr:
                learned = self._maybe_learn(self.gallery.add_face_view, p, face_emb, sim)
                if body_emb is not None and not p.body_views:
                    self.gallery.add_body_view(p, body_emb)
                return self._result(p.id, p.label, p.name, sim,
                                    matched_by="face", learned=learned)

        # ── BODY (fallback) ──
        if body_emb is not None:
            p, sim = self.gallery.best_match_body(body_emb)
            if p is not None and sim >= config.BODY_MATCH_THRESHOLD:
                learned = self._maybe_learn(self.gallery.add_body_view, p, body_emb, sim)
                if face_emb is not None and not p.face_views:
                    self.gallery.add_face_view(p, face_emb)
                return self._result(p.id, p.label, p.name, sim,
                                    matched_by="body", learned=learned)

        # ── good detection + features, but no match → a NEW Visitor ──
        if config.AUTO_ENROLL_UNKNOWN:
            p = self.gallery.add(face_emb, body_emb, label=config.LABEL_VISITOR)
            return self._result(p.id, config.LABEL_VISITOR, "", 0.0,
                                matched_by="none", is_new=True)
        return self._result(None, config.LABEL_UNKNOWN, "", 0.0, matched_by="none")

        return self._result(None, config.LABEL_UNKNOWN, "", best_sim, matched_by="none")

    def _maybe_learn(self, add_view_fn, person, embedding, sim):
        if sim < config.LEARN_CEILING:
            add_view_fn(person, embedding)
            return True
        return False

    def _result(self, pid, label, name, sim, matched_by, is_new=False,
                learned=False, error=None):
        return {
            "person_id": pid, "label": label, "name": name,
            "confidence": round(sim, 4), "confidence_pct": round(sim * 100, 1),
            "matched_by": matched_by, "is_new": is_new, "learned": learned,
            "error": error,
        }
