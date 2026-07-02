"""
services/feature_matcher.py
===========================
Vector-match interface over Qdrant — face-primary, body-fallback.

Strategy (per the Part 2 matching decision):
    1. If a face embedding is present, search the `faces` collection.
       A hit at or above FACE_SIMILARITY_THRESHOLD wins → matched_by=FACE.
    2. Otherwise (no face, or face too weak), fall back to the body ReID
       embedding and search the `bodies` collection.  A hit at or above
       BODY_SIMILARITY_THRESHOLD wins → matched_by=BODY.
    3. No qualifying hit in either modality → None (caller creates a new
       visitor).

Face is trusted first because it's far more discriminative than body
ReID; body is only a cross-camera continuity fallback for when the face
isn't visible (person turned away, low res, occlusion).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence

import config
from db.models import MatchedBy
from repositories import embedding_repo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MatchResult:
    """Returned by find_match() when a vector match is found."""
    identity_id: int
    similarity: float
    matched_by: MatchedBy

    def __repr__(self) -> str:
        return (
            f"<MatchResult id={self.identity_id} "
            f"sim={self.similarity:.4f} by={self.matched_by.value}>"
        )


async def find_match(
    face_embedding: Optional[Sequence[float]] = None,
    body_embedding: Optional[Sequence[float]] = None,
) -> Optional[MatchResult]:
    """
    Find the best existing identity for the given embeddings.

    Returns a MatchResult (with matched_by=FACE or BODY) or None.
    """
    # ---- 1. Face (primary) ------------------------------------------- #
    if face_embedding is not None and len(face_embedding) > 0:
        candidates = await embedding_repo.search_face(face_embedding, limit=5)
        if candidates:
            best_id, best_sim = candidates[0]
            if best_sim >= config.FACE_SIMILARITY_THRESHOLD:
                logger.info("Face match: identity=%d sim=%.4f", best_id, best_sim)
                return MatchResult(best_id, best_sim, MatchedBy.FACE)
            logger.debug("Best face sim %.4f < %.2f", best_sim, config.FACE_SIMILARITY_THRESHOLD)

    # ---- 2. Body (fallback) ------------------------------------------ #
    if body_embedding is not None and len(body_embedding) > 0:
        candidates = await embedding_repo.search_body(body_embedding, limit=5)
        if candidates:
            best_id, best_sim = candidates[0]
            if best_sim >= config.BODY_SIMILARITY_THRESHOLD:
                logger.info("Body (ReID) match: identity=%d sim=%.4f", best_id, best_sim)
                return MatchResult(best_id, best_sim, MatchedBy.BODY)
            logger.debug("Best body sim %.4f < %.2f", best_sim, config.BODY_SIMILARITY_THRESHOLD)

    return None
