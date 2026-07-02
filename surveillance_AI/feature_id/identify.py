# ─────────────────────────────────────────────
#  STEPS 3-5 — THE DECISION  (ties everything together)
# ─────────────────────────────────────────────
# Give it a segmented person crop, it returns:
#   {person_id, label, name, confidence, confidence_pct, is_new, learned, error}
#
# Flow:  guard bad crops -> extract -> match -> threshold -> decide
#        -> if matched but not near-perfect, LEARN the new view (raise confidence)
#        -> else auto-enroll unknowns as new visitors.

from . import config
from .extractor import Extractor
from .gallery import Gallery


class Identifier:
    def __init__(self):
        self.extractor = Extractor()
        self.gallery = Gallery()

    def _too_small(self, crop):
        """A segmented crop too tiny/empty to trust for features."""
        if crop is None or crop.size == 0:
            return True
        h, w = crop.shape[:2]
        return h < config.MIN_CROP_SIDE or w < config.MIN_CROP_SIDE

    def identify(self, person_bgr, match_threshold=None):
        # ── STEP 0 — "couldn't extract features" guard ──
        # If SAM 2 gave us nothing usable, don't invent an identity — say so.
        if self._too_small(person_bgr):
            return {
                "person_id": None, "label": config.LABEL_UNKNOWN, "name": "",
                "confidence": 0.0, "confidence_pct": 0.0,
                "is_new": False, "learned": False,
                "error": "no_features",   # segmentation failed / crop too small
            }

        # ── STEP 1 — extract the 512-number embedding ──
        emb = self.extractor.embed(person_bgr)
        return self.identify_embedding(emb, match_threshold=match_threshold)

    def identify_embedding(self, emb, match_threshold=None):
        """Same decision as identify() but on an ALREADY-extracted embedding, so
        the pipeline can compute the vector once (and reuse it to emit to the
        Brain) and pass a per-camera threshold override.

        match_threshold=None → use the global config.MATCH_THRESHOLD. Passing a
        higher value (e.g. 0.85) makes THIS camera stricter without touching the
        others — that's how you 'raise the threshold' on a specific camera."""
        threshold = config.MATCH_THRESHOLD if match_threshold is None else match_threshold

        # ── STEP 2 — find the closest known person ──
        match, sim = self.gallery.best_match(emb)

        # ── STEP 3 — recognized? (above the threshold) ──
        if match is not None and sim >= threshold:
            # PROGRESSIVE CONFIDENCE: matched, but not a near-duplicate → this is
            # a fresh angle worth remembering, so next time the score is higher.
            learned = False
            if sim < config.LEARN_CEILING:
                self.gallery.add_view(match, emb)
                learned = True
            return {
                "person_id": match.id, "label": match.label, "name": match.name,
                "confidence": round(sim, 4), "confidence_pct": round(sim * 100, 1),
                "is_new": False, "learned": learned, "error": None,
            }

        # ── STEP 4 — nobody matched well enough → Unknown ──
        if config.AUTO_ENROLL_UNKNOWN:
            new_person = self.gallery.add(emb, label=config.LABEL_VISITOR)
            return {
                "person_id": new_person.id, "label": config.LABEL_VISITOR, "name": "",
                "confidence": round(sim, 4), "confidence_pct": round(sim * 100, 1),
                "is_new": True, "learned": False, "error": None,
            }

        return {
            "person_id": None, "label": config.LABEL_UNKNOWN, "name": "",
            "confidence": round(sim, 4), "confidence_pct": round(sim * 100, 1),
            "is_new": False, "learned": False, "error": None,
        }
