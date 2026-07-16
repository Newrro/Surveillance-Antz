"""identity_resolver/body_relink.py — constrained same-camera body re-link."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

import config
from db.models import MatchedBy
from repositories import embedding_repo, event_repo, identity_repo

from .types import ResolutionResult
from .helpers import _classify

logger = logging.getLogger(__name__)


async def _relink_by_body(
    session: AsyncSession,
    body_embedding: Optional[Sequence[float]],
    camera_id: Optional[int],
    detected_at: Optional[datetime],
    context: str = "no-face",
) -> Optional[ResolutionResult]:
    """Constrained body RE-LINK for a no-face sighting: reuse an existing identity
    iff its body vector matches at/above BODY_MERGE_THRESHOLD AND that identity was
    seen on THIS camera within BODY_MERGE_WINDOW_SECONDS. Same clothes, same place,
    seconds apart ⇒ same person walking with their face turned away. The tight
    cosine + camera + time gate is what keeps clothing from ever merging strangers
    (body similarity alone never creates or joins an identity)."""
    if body_embedding is None or len(body_embedding) == 0 or camera_id is None:
        return None
    when = detected_at or datetime.utcnow()
    try:
        hits = await embedding_repo.search_body(body_embedding, limit=5)
    except Exception as e:  # noqa: BLE001 — a body-index hiccup must not kill ingest
        logger.warning("body re-link search failed: %s", e)
        return None
    if not hits:
        return None
    since = when - timedelta(seconds=config.BODY_MERGE_WINDOW_SECONDS)
    recent = await event_repo.identities_seen_on_camera_since(session, camera_id, since)
    best_id, best_sim = None, 0.0
    for iid, sim in hits:
        if sim >= config.BODY_MERGE_THRESHOLD and iid in recent and sim > best_sim:
            best_id, best_sim = iid, sim
    if best_id is None:
        # Telemetry for tuning BODY_MERGE_THRESHOLD: show the best CO-PRESENT
        # candidate (the one the gate was actually judging), not just the top hit.
        copresent = [(iid, sim) for iid, sim in hits if iid in recent]
        if copresent:
            miss_id, miss_sim = max(copresent, key=lambda x: x[1])
            logger.info("body re-link MISS: best co-present id=%d sim=%.3f < thr=%.2f (cam=%s)",
                        miss_id, miss_sim, config.BODY_MERGE_THRESHOLD, camera_id)
        return None
    identity = await identity_repo.fetch_identity_by_id(session, best_id)
    if identity is None:
        return None
    classification, label = await _classify(session, identity)
    logger.info("resolve: %s → BODY-RELINK id=%d (%s) cls=%s sim=%.3f (same cam ≤%ds)",
                context.upper(), identity.id, identity.display_label, classification.value,
                best_sim, config.BODY_MERGE_WINDOW_SECONDS)
    return ResolutionResult(
        classification=classification, identity_id=identity.id,
        matched_by=MatchedBy.BODY, similarity=best_sim, label=label,
    )
