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

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

import config
from db.models import Classification, IdentityType, MatchedBy
from repositories import embedding_repo, event_repo, identity_repo
from services import calibration_service, presence_cache

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Track-sticky identity cache (detection_id → identity_id)
# ---------------------------------------------------------------------------
# Part 1's detection_id is stable per tracker track and camera-scoped
# ("{camera_uid}-t{n}"). Once a track is resolved to an identity, later payloads
# of the SAME track (a re-emit with a better face, or a no-face fallback) reuse
# that identity instead of minting a second id / an id-less Unknown. Redis-backed
# and best-effort: any cache failure falls through to normal resolution.
def _track_key(detection_id: str) -> str:
    return f"trackid:{detection_id}"


async def get_track_identity(detection_id: Optional[str]) -> Optional[int]:
    if not detection_id:
        return None
    try:
        client = await presence_cache.get_client()
        val = await client.get(_track_key(detection_id))
        return int(val) if val is not None else None
    except Exception:  # noqa: BLE001 — cache is an optimisation, never a blocker
        return None


async def remember_track_identity(detection_id: Optional[str], identity_id: Optional[int]) -> None:
    if not detection_id or identity_id is None:
        return
    try:
        client = await presence_cache.get_client()
        await client.set(_track_key(detection_id), str(identity_id),
                         ex=config.TRACK_STICKY_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        pass


@dataclass(frozen=True)
class ResolutionResult:
    """Returned by resolve()."""
    classification: Classification
    identity_id: Optional[int]
    matched_by: MatchedBy = MatchedBy.NONE
    similarity: Optional[float] = None
    label: Optional[str] = None

    def __repr__(self) -> str:
        return (
            f"<ResolutionResult cls={self.classification.value} id={self.identity_id} "
            f"by={self.matched_by.value} sim={self.similarity} label={self.label}>"
        )


async def resolve(
    session: AsyncSession,
    detection_conf: float,
    face_embedding: Optional[Sequence[float]] = None,
    body_embedding: Optional[Sequence[float]] = None,
    camera_id: Optional[int] = None,
    detected_at: Optional[datetime] = None,
    detection_id: Optional[str] = None,
) -> ResolutionResult:
    """Open-set 1:N FACE identification, recall-first + self-calibrating.

    1. GATE: low detection_conf → UNKNOWN (no id — a non-capture).
       No face → track-sticky reuse, else constrained body RE-LINK, else Unknown.
    2. Best-of-set search; s1/s2 = top-1/top-2 cosine to DISTINCT identities.
       Feed s2 to the calibrator ONLY when it is below the current threshold —
       an above-threshold s2 is almost always our OWN duplicate (same person
       fragmented across two ids), not an impostor, and counting it poisons the
       impostor statistics upward.
    3. ASSIGN to id1 iff s1 ≥ match_threshold() AND ((s1−s2) ≥ margin OR s2 is
       ALSO ≥ threshold). The margin guards against look-alikes — but when both
       top candidates clear the floor they are (near-always) fragments of the
       SAME person, so refusing to assign would mint a THIRD id. Assign to the
       best and let deferred clustering fold the other one.
    4. Otherwise: same-track sticky id (a re-emit must not mint a duplicate),
       else ENROLL a new Visitor NOW from this good capture.
    """
    # ---- 1. GATE ----------------------------------------------------- #
    if detection_conf < config.DETECTION_CONF_THRESHOLD:
        logger.info("resolve: GATED conf=%.2f < %.2f → Unknown (no id)",
                    detection_conf, config.DETECTION_CONF_THRESHOLD)
        return ResolutionResult(classification=Classification.UNKNOWN, identity_id=None)

    has_face_now = face_embedding is not None and len(face_embedding) > 0
    if not has_face_now:
        # No clear face this time — but NOT necessarily a stranger. In order:
        #   a. the SAME track was already identified → keep that identity (a
        #      person we named must never also produce id-less Unknown rows);
        #   b. constrained body RE-LINK — same camera, short window, high cosine;
        #   c. genuinely uncapturable → Unknown, no identity.
        sticky = await _resolve_sticky_track(session, detection_id)
        if sticky is not None:
            return sticky
        relinked = await _relink_by_body(session, body_embedding, camera_id, detected_at)
        if relinked is not None:
            return relinked
        logger.info("resolve: NO-FACE (body_only=%s) → Unknown (no id)",
                    body_embedding is not None)
        return ResolutionResult(classification=Classification.UNKNOWN, identity_id=None)

    # ---- 2. OPEN-SET best-of-set search ------------------------------ #
    face_hits = await embedding_repo.search_face(face_embedding, limit=10)
    best_by_id: dict[int, float] = {}
    for iid, sim in face_hits:
        if iid not in best_by_id or sim > best_by_id[iid]:
            best_by_id[iid] = sim
    ranked = sorted(best_by_id.items(), key=lambda kv: kv[1], reverse=True)
    id1, s1 = (ranked[0] if ranked else (None, 0.0))
    s2 = ranked[1][1] if len(ranked) > 1 else 0.0

    thr = calibration_service.match_threshold()
    # The 2nd-best distinct identity is an impostor sample ONLY when it sits below
    # the decision floor. Above it, s2 is nearly always a duplicate of the SAME
    # person (fragmented gallery) — feeding those high cosines to the calibrator
    # dragged the impostor mean/std up, raising the threshold, causing MORE misses
    # and more duplicates: a self-poisoning loop.
    #
    # Gate against the FIXED cold-start default, NOT the live threshold: gating on
    # the moving thr creates the opposite spiral (thr drops → sampling window drops
    # → mean drops → thr pins at the clamp floor — observed live 2026-07-14, match
    # floor stuck at 0.38). A fixed reference keeps the sample support stable.
    if len(ranked) > 1 and s2 < config.FACE_MATCH_THRESHOLD_DEFAULT:
        calibration_service.observe_impostor(s2)

    # One structured line per decision — the primary debugging surface. Shows the
    # best/second-best identity cosines, the LIVE self-calibrated threshold, and the
    # margin, so "why did this match / why a new id?" is answerable straight from logs.
    logger.info("resolve: s1=%.3f(id=%s) s2=%.3f thr=%.3f margin=%.3f cands=%d",
                s1, id1, s2, thr, config.FACE_MATCH_MARGIN, len(ranked))

    # ---- 3. CONFIDENT re-match → ASSIGN (re-use id) ------------------ #
    # Margin: top-1 must beat top-2 clearly — UNLESS top-2 also clears the floor,
    # in which case the two ids are (near-always) fragments of the same person;
    # assign to the best one and let deferred clustering fold the other.
    if id1 is not None and s1 >= thr and ((s1 - s2) >= config.FACE_MATCH_MARGIN or s2 >= thr):
        identity = await identity_repo.fetch_identity_by_id(session, id1)
        if identity is not None:
            if s2 >= thr and (s1 - s2) < config.FACE_MATCH_MARGIN:
                logger.info("resolve: duplicate-hint — ids %s both ≥ thr; clustering will fold",
                            [i for i, _ in ranked[:2]])
            await _assign_and_learn(session, identity, face_embedding, s1,
                                    body_embedding=body_embedding)
            classification, label = await _classify(session, identity)
            logger.info("resolve: → ASSIGN id=%d (%s) cls=%s by=face sim=%.3f",
                        identity.id, identity.display_label, classification.value, s1)
            return ResolutionResult(
                classification=classification, identity_id=identity.id,
                matched_by=MatchedBy.FACE, similarity=s1, label=label,
            )
        logger.warning("resolve: face match id=%d gone from DB — enrolling as new", id1)

    # ---- 4a. Same track already has an id → REUSE it (learn the view) - #
    # A track re-emitting with a better pooled face must NEVER mint a second id:
    # it is by construction the same physical person the tracker followed.
    cached_id = await get_track_identity(detection_id)
    if cached_id is not None:
        identity = await identity_repo.fetch_identity_by_id(session, cached_id)
        if identity is not None:
            await _assign_and_learn(session, identity, face_embedding, s1,
                                    body_embedding=body_embedding)
            classification, label = await _classify(session, identity)
            logger.info("resolve: → TRACK-STICKY id=%d (%s) cls=%s (track=%s, s1=%.3f)",
                        identity.id, identity.display_label, classification.value,
                        detection_id, s1)
            return ResolutionResult(
                classification=classification, identity_id=identity.id,
                matched_by=MatchedBy.FACE, similarity=s1, label=label,
            )

    # ---- 4b. Face matched nobody — but was this person JUST here? ----- #
    # Mint-averting constrained body re-link (the other half of the no-face
    # re-link, and the original anti-fragmentation design): same camera + short
    # window + high body cosine ⇒ the tracker broke and re-pooled a WORSE face
    # angle of the SAME person (verified live: one person minted twice 10 s
    # apart in identical clothes). Attribute the sighting instead of minting a
    # duplicate. The face view is NOT learned — a body link is not face proof,
    # and learning it could poison the gallery if the link is ever wrong.
    relink = await _relink_by_body(session, body_embedding, camera_id, detected_at,
                                   context="mint-avert")
    if relink is not None:
        return relink

    # ---- 4c. NO confident match → ENROLL a new Visitor NOW ----------- #
    # A well-captured face with no existing match is a NEW person. We do NOT leave
    # them Unknown (the old bug) — being captured clearly IS the bar for a Visitor.
    return await _create_visitor(session, face_embedding, body_embedding, top_sim=s1)


async def _resolve_sticky_track(
    session: AsyncSession, detection_id: Optional[str]
) -> Optional[ResolutionResult]:
    """A no-face payload whose track was ALREADY identified keeps its identity —
    the person the tracker followed did not change because their face turned away."""
    cached_id = await get_track_identity(detection_id)
    if cached_id is None:
        return None
    identity = await identity_repo.fetch_identity_by_id(session, cached_id)
    if identity is None:
        return None
    classification, label = await _classify(session, identity)
    logger.info("resolve: NO-FACE → TRACK-STICKY id=%d (%s) cls=%s (track=%s)",
                identity.id, identity.display_label, classification.value, detection_id)
    return ResolutionResult(
        classification=classification, identity_id=identity.id,
        matched_by=MatchedBy.BODY, similarity=None, label=label,
    )


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


async def _create_visitor(
    session: AsyncSession,
    face_embedding: Sequence[float],
    body_embedding: Optional[Sequence[float]],
    top_sim: float,
) -> ResolutionResult:
    """Enroll a brand-new face as a VISITOR immediately. Stores the first face
    template and confirms the visitor when a body was captured too ("clear face +
    body ⇒ Visitor"). If a later tracklet of the SAME person misses the online
    match and enrolls again, deferred clustering folds the duplicate back."""
    year = datetime.utcnow().year
    seq = await identity_repo.next_visitor_seq(session, year)
    label = f"VIS-{year}-{seq:04d}"
    new_identity = await identity_repo.create_identity(session, IdentityType.VISITOR, label)
    await identity_repo.insert_visitor(
        session, identity_id=new_identity.id, visitor_seq=seq,
        year=year, first_seen_at=datetime.utcnow(),
    )
    # Store the body vector TOO — not for identity (face-only, clothing never
    # merges people) but for the constrained same-camera body RE-LINK, which needs
    # something to match when this person's next tracklet has no usable face.
    await embedding_repo.store_embeddings(
        new_identity.id, face_embedding=face_embedding,
        body_embedding=body_embedding, source="auto_first_seen",
    )
    has_body = body_embedding is not None and len(body_embedding) > 0
    await identity_repo.set_visitor_flags(session, new_identity.id, True, has_body)

    # Confirm-on-capture: a clear face (we are past the gate) plus a body picture is
    # a proper capture → a real Visitor from the first sighting. A face with no body
    # is a weaker capture → stays provisional (Unknown) until re-identified.
    confirmed = has_body
    if confirmed:
        await identity_repo.confirm_visitor(session, new_identity.id)
    classification = Classification.VISITOR if confirmed else Classification.UNKNOWN
    logger.info("New face → %s %s (id=%d, top_sim=%.3f)",
                "VISITOR" if confirmed else "provisional (Unknown)",
                label, new_identity.id, top_sim)
    return ResolutionResult(
        classification=classification, identity_id=new_identity.id,
        matched_by=MatchedBy.NONE, similarity=None, label=label,
    )


async def _classify(session: AsyncSession, identity):
    """Event label for a matched identity: Employee, Visitor (confirmed), or Unknown
    (a provisional visitor — captured a face but no body yet — not yet confirmed)."""
    if identity.identity_type == IdentityType.EMPLOYEE:
        return Classification.EMPLOYEE, identity.display_label
    confirmed = await identity_repo.is_confirmed_visitor(session, identity.id)
    return (Classification.VISITOR if confirmed else Classification.UNKNOWN), identity.display_label


async def _assign_and_learn(
    session: AsyncSession, identity, face_embedding: Sequence[float], sim: float,
    body_embedding: Optional[Sequence[float]] = None,
) -> None:
    """On a confident re-match: (1) learn — add this face as a fresh gallery view
    when it isn't a near-duplicate, so future angles keep matching (this is what
    keeps a returning person on ONE id); (2) refresh the body vector (today's
    clothes) so the same-camera body RE-LINK can follow this person's faceless
    tracklets; (3) confirm a provisional visitor, since being re-identified is
    proof of a re-identifiable person. Employees are already confirmed and carry
    no visitor row."""
    # Learn only inside the confidence band: above the FLOOR (a borderline assign
    # must not graft a possibly-wrong face into the gallery) and below the CEILING
    # (a near-duplicate view adds nothing).
    if config.LEARN_SIMILARITY_FLOOR <= sim < config.LEARN_SIMILARITY_CEILING:
        await embedding_repo.store_embeddings(identity.id, face_embedding=face_embedding,
                                              body_embedding=body_embedding, source="relearn")

    if identity.identity_type == IdentityType.EMPLOYEE:
        return
    await identity_repo.set_visitor_flags(session, identity.id, True, False)
    if not await identity_repo.is_confirmed_visitor(session, identity.id):
        if await identity_repo.confirm_visitor(session, identity.id):
            logger.info("CONFIRMED Visitor %s (id=%d): re-identified by face (sim=%.3f)",
                        identity.display_label, identity.id, sim)
