"""
services/identity_resolver.py
=============================
The brain of the classification logic — 2026 self-calibrating, recall-first design.

Given (detection_conf, face_embedding), decide one of:
    - UNKNOWN  (no id)  — detection gated out, OR no face captured. "Unknown" is
                          reserved for people the system could NOT capture properly
                          (no clear face). These are ephemeral (deduped on the
                          per-track detection_id upstream); they never enroll.
    - EMPLOYEE          — the face matched an enrolled employee.
    - VISITOR           — the face matched an existing visitor (re-used id), OR it's
                          a new, well-captured face → enrolled as a Visitor NOW.

DESIGN (why this replaces the old "confirm only on a 0.62 re-match" model)
    Distant-CCTV faces of the SAME person score only ~0.35-0.55 cosine, below any
    safe fixed threshold — so the old model left well-captured people stuck as
    "Unknown" forever. Fixes:

    1. RECALL-FIRST online matching at a SELF-CALIBRATED threshold that floats just
       above the observed nearest-impostor similarity (calibration_service), so a
       returning face actually re-matches its own gallery without hand-tuning.
    2. Multi-template gallery + best-of-set matching (search_face already returns
       per-vector hits; we take the max per identity), which tolerates the wide
       intra-person variation of low-res faces far better than one centroid.
    3. CONFIRM-ON-CAPTURE: a good face (+ body) is a Visitor immediately — being
       captured well IS the bar, not being lucky enough to re-match at 0.62.
    4. Duplicates from an online miss are folded back by DEFERRED CLUSTERING
       (dedup_service.consolidate_visitors), the source of truth for identity, so
       the gallery converges to one-identity-per-person in steady state.

`detection_conf` is a 0.0-1.0 probability from Part 1 (NOT a percentage).
`body_embedding` is accepted for signature compatibility but NEVER used to create
or join an identity (clothing/uniforms cause false merges) — identity is face-only.
"""

from .types import ResolutionResult
from .track_sticky import get_track_identity, remember_track_identity
from .resolve import resolve

__all__ = ["resolve", "remember_track_identity", "get_track_identity", "ResolutionResult"]
