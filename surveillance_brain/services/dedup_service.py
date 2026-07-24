"""
services/dedup_service.py
=========================
Duplicate-detection guard (module 6 in the RUNG01 notes).

Purpose:
    Part 1 emits one payload PER accepted person crop PER frame — a person
    standing in front of a camera for 10 seconds can generate hundreds of
    near-identical detections.  Without a guard we would spam the event
    ledger and the /live feed.

Strategy:
    A short-window Redis key `dedup:{identity_id}:{camera_id}` with a TTL of
    DUPLICATE_WINDOW_SECONDS.  The FIRST detection of an identity on a camera
    within the window is logged; subsequent ones are suppressed until the
    key expires (i.e. the person has been continuously present).  Presence
    tracking still runs on every detection — only the *ledger write* and the
    *live broadcast* are de-duplicated.

Also exposes `merge_identities()` — a best-effort admin helper to collapse
two identity rows that turned out to be the same person (e.g. a face-only
match and a body-only match that were never linked).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

import config
from db.models import DetectionEvent, IdentityType, PresenceSession
from repositories import embedding_repo, identity_repo
from services import media_paths, presence_cache

logger = logging.getLogger(__name__)


def _key(identity_id: int, camera_id: int) -> str:
    return f"dedup:{identity_id}:{camera_id}"


def _unknown_key(detection_id: str, camera_id: int) -> str:
    return f"dedup:unk:{detection_id}:{camera_id}"


async def _claim_window(key: str) -> bool:
    """SET NX EX the dedup key. Returns True if this call is a DUPLICATE (the key
    already existed). Fails OPEN (False) if Redis is down — better to log a
    possible duplicate than silently drop a real event."""
    try:
        client = await presence_cache.get_client()
        was_set = await client.set(key, "1", nx=True, ex=config.DUPLICATE_WINDOW_SECONDS)
        # was_set is True when WE created the key (i.e. NOT a duplicate).
        return not bool(was_set)
    except Exception as e:  # noqa: BLE001
        logger.warning("Dedup check failed (%s) — treating as non-duplicate", e)
        return False


async def is_duplicate(identity_id: int, camera_id: int) -> bool:
    """True if this (identity, camera) pair was already logged inside the window."""
    return await _claim_window(_key(identity_id, camera_id))


async def is_duplicate_unknown(detection_id: str, camera_id: int) -> bool:
    """Dedup Unknown detections (no identity_id) by their STABLE per-track
    detection_id from Part 1. A person who lingers unrecognised re-emits every
    few seconds; without this each emit would be a fresh Unknown ledger row and
    a new 'Unknown person' card. Collapses them to one per window."""
    return await _claim_window(_unknown_key(detection_id, camera_id))


async def merge_identities(session: AsyncSession, primary_id: int, duplicate_id: int) -> None:
    """
    Merge `duplicate_id` INTO `primary_id`.

    - Re-points detection_events + presence_sessions from duplicate → primary.
    - MOVES the duplicate's Qdrant vectors onto the primary (a merge must ADD
      the duplicate's gallery views to the keeper — deleting them thinned the
      gallery on every merge, so the person failed the next re-match and
      immediately minted a fresh duplicate, which read as "merge not working").
    - Deletes the duplicate identity row (CASCADE drops its extension row).

    Caller owns the transaction.  Intended for the admin path, not the hot
    ingest path.
    """
    if primary_id == duplicate_id:
        raise ValueError("primary_id and duplicate_id are the same")

    primary = await identity_repo.fetch_identity_by_id(session, primary_id)
    if primary is None:
        raise ValueError(f"primary identity_id={primary_id} not found")
    duplicate = await identity_repo.fetch_identity_by_id(session, duplicate_id)
    if duplicate is None:
        raise ValueError(f"duplicate identity_id={duplicate_id} not found")

    await session.execute(
        update(DetectionEvent)
        .where(DetectionEvent.identity_id == duplicate_id)
        .values(identity_id=primary_id)
    )
    await session.execute(
        update(PresenceSession)
        .where(PresenceSession.identity_id == duplicate_id)
        .values(identity_id=primary_id)
    )

    # Move the duplicate's gallery onto the primary, then drop the duplicate's
    # points and row. insert_face/insert_body re-apply the per-identity view cap,
    # so the merged gallery stays bounded.
    for tpl in await embedding_repo.fetch_face_vectors(duplicate_id):
        await embedding_repo.insert_face(primary_id, tpl, source="merge")
    for tpl in await embedding_repo.fetch_body_vectors(duplicate_id):
        await embedding_repo.insert_body(primary_id, tpl, source="merge")
    await embedding_repo.delete_embeddings_for_identity(duplicate_id)
    await session.delete(duplicate)

    # A merge IS same-person evidence: either deferred clustering re-identified
    # this face across tracklets (≥ the conservative merge threshold) or a human
    # asserted it manually — both meet the bar the online path uses to confirm.
    # So the surviving visitor is CONFIRMED (Unknown → Visitor). This also means
    # confirmation can never be LOST by folding a confirmed visitor into an
    # older, still-provisional id (the duplicate's row is CASCADE-deleted).
    if primary.identity_type == IdentityType.VISITOR:
        if await identity_repo.confirm_visitor(session, primary_id):
            logger.info("CONFIRMED Visitor %s (id=%d) on merge — duplicate fold is re-identification",
                        primary.display_label, primary_id)
    await session.flush()

    logger.info("Merged identity %d into %d", duplicate_id, primary_id)


async def purge_identity(session: AsyncSession, identity_id: int) -> None:
    """Delete an identity ENTIRELY — its sightings, sessions, embeddings and the
    identity row (CASCADE drops its visitor/employee extension). Unlike merge,
    nothing is preserved. Caller owns the transaction. Admin path only."""
    identity = await identity_repo.fetch_identity_by_id(session, identity_id)
    if identity is None:
        raise ValueError(f"identity_id={identity_id} not found")
    await session.execute(delete(DetectionEvent).where(DetectionEvent.identity_id == identity_id))
    await session.execute(delete(PresenceSession).where(PresenceSession.identity_id == identity_id))
    await embedding_repo.delete_embeddings_for_identity(identity_id)
    await session.delete(identity)
    await session.flush()
    media_paths.delete_profile(identity_id)   # drop the durable avatar + lock too
    logger.info("Purged identity %d (events + sessions + vectors + profile + row)", identity_id)


# ---------------------------------------------------------------------------
# Offline gallery consolidation (Phase 2)
# ---------------------------------------------------------------------------
@dataclass
class MergePlan:
    """One planned/applied merge: fold `duplicates` into `primary` (the oldest id)."""
    primary: int
    primary_label: str
    duplicates: List[int]
    labels: List[str]
    similarity: float


def _normed_centroid(vectors: List[List[float]]) -> Optional[List[float]]:
    """Mean of an identity's face vectors, L2-normalized — its gallery centroid.
    Averaging over all stored views is a simple quality-robust representation:
    one bad frame can't dominate. Returns None if the identity has no face."""
    import numpy as np
    if not vectors:
        return None
    m = np.asarray(vectors, dtype="float32").mean(axis=0)
    n = float(np.linalg.norm(m))
    if n == 0:
        return None
    return (m / n).tolist()


def _cosine(a: List[float], b: List[float]) -> float:
    import numpy as np
    va, vb = np.asarray(a, "float32"), np.asarray(b, "float32")
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    if na == 0 or nb == 0:
        return 0.0
    return float(va.dot(vb) / (na * nb))


async def consolidate_visitors(
    session: AsyncSession,
    face_threshold: Optional[float] = None,
    apply: bool = False,
) -> List[MergePlan]:
    """Deferred face clustering — the SOURCE OF TRUTH for identity. Finds VISITOR
    ids that are really the same person and folds each cluster into its oldest id.

    Uses BEST-OF-SET matching (not centroid-to-centroid): for each identity's stored
    face templates we query the face index and connect it to any OTHER visitor whose
    template it matches at/above `face_threshold`. Best-of-set tolerates the wide
    intra-person variation of low-res faces far better than a single centroid — it's
    the change that lets fragments of one person actually re-join. Face only (never
    body/clothing). `face_threshold` defaults to the self-calibrated, conservative
    merge threshold (well above the impostor ceiling), so auto-merge stays precise.

    apply=False → DRY RUN (plans only). apply=True → calls merge_identities per pair;
    the CALLER owns the transaction (commit after)."""
    from services import calibration_service
    thr = calibration_service.merge_threshold() if face_threshold is None else face_threshold

    visitors = await identity_repo.list_visitor_identities(session)
    labels: Dict[int, str] = {v.id: v.display_label for v in visitors}
    visitor_ids = set(labels)
    if len(visitor_ids) < 2:
        return []

    # Union-Find over the threshold graph. An edge (a,b) exists when a face template
    # of `a` retrieves a template of `b` at cosine ≥ thr (best-of-set, via the index).
    parent = {i: i for i in visitor_ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)    # keep the oldest (lowest id) as root

    best_sim: Dict[Tuple[int, int], float] = {}
    for vid in visitor_ids:
        templates = await embedding_repo.fetch_face_vectors(vid)
        for tpl in templates:
            for hid, sim in await embedding_repo.search_face(tpl, limit=6):
                if hid == vid or hid not in visitor_ids or sim < thr:
                    continue
                union(vid, hid)
                key = (min(vid, hid), max(vid, hid))
                if sim > best_sim.get(key, 0.0):
                    best_sim[key] = sim

    # Group by root; emit a plan for every cluster of size > 1.
    groups: Dict[int, List[int]] = {}
    for i in visitor_ids:
        groups.setdefault(find(i), []).append(i)

    plans: List[MergePlan] = []
    for root, members in groups.items():
        dups = sorted(m for m in members if m != root)
        if not dups:
            continue
        sims = [best_sim.get((min(root, d), max(root, d)), 0.0) for d in dups]
        plans.append(MergePlan(
            primary=root, primary_label=labels[root],
            duplicates=dups, labels=[labels[d] for d in dups],
            similarity=round(max(sims) if sims else 0.0, 4),
        ))

    if apply:
        for plan in plans:
            for dup in plan.duplicates:
                await merge_identities(session, primary_id=plan.primary, duplicate_id=dup)
        if plans:
            logger.info("Consolidated %d duplicate visitor(s) into %d identities (thr=%.3f)",
                        sum(len(p.duplicates) for p in plans), len(plans), thr)
    return plans
