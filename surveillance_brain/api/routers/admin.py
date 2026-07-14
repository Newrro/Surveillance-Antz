"""
api/routers/admin.py — administrative maintenance endpoints.

POST /admin/reset — wipe the whole database (people, events, sessions, embeddings,
live presence) for a clean slate. Cameras are KEPT (they're infrastructure, not
data). Protected by HTTP Basic Auth (api.auth.require_admin).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import text

from api.auth import require_admin
from db import vector_store
from db.connection import get_session
from services import calibration_service, dedup_service, presence_cache

logger = logging.getLogger("admin")

router = APIRouter(prefix="/admin", tags=["admin"])

# Everything except the camera registry. RESTART IDENTITY resets the serial ids
# so the next visitor is VIS-2026-0001 again; CASCADE clears the 1:1 extensions.
_WIPE_SQL = (
    "TRUNCATE detection_events, presence_sessions, employees, visitors, identities "
    "RESTART IDENTITY CASCADE"
)


@router.post("/reset")
async def reset_database(_: str = Depends(require_admin)) -> dict:
    """Delete all people/events/sessions/embeddings/presence. Keeps cameras."""
    async with get_session() as session:
        await session.execute(text(_WIPE_SQL))
        await session.commit()
    await vector_store.clear_all()               # drop face + body vectors
    try:
        redis = await presence_cache.get_client()
        await redis.flushdb()                    # clear live presence + dedup guards
    except Exception as exc:  # noqa: BLE001 — Redis is best-effort here
        logger.warning("reset: redis flush failed: %s", exc)
    logger.warning("DATABASE RESET via /admin/reset — all people/events cleared")
    return {"status": "ok", "message": "Database wiped. Cameras kept."}


async def clear_unknowns() -> int:
    """Delete every UNCONFIRMED person (Unknowns: visitor rows with no
    confirmed_at) — their identity, embeddings, sessions, and all UNKNOWN-labelled
    sightings. CONFIRMED Visitors and Employees are kept, so a real visitor is
    still recognised tomorrow. Returns how many Unknowns were removed."""
    async with get_session() as session:
        rows = await session.execute(
            text("SELECT identity_id FROM visitors WHERE confirmed_at IS NULL")
        )
        ids = [r[0] for r in rows]
        # Unknown CASES (identity_type='unknown') are ephemeral placeholders for
        # unidentified tracks — swept together with the unconfirmed visitors.
        case_rows = await session.execute(
            text("SELECT id FROM identities WHERE identity_type = 'unknown'")
        )
        ids += [r[0] for r in case_rows]
        # Drop all Unknown sightings (covers the unconfirmed ids + any orphans).
        await session.execute(text("DELETE FROM detection_events WHERE classification = 'unknown'"))
        if ids:
            await session.execute(
                text("DELETE FROM presence_sessions WHERE identity_id = ANY(:ids)"), {"ids": ids}
            )
            # CASCADE removes the visitors extension rows.
            await session.execute(text("DELETE FROM identities WHERE id = ANY(:ids)"), {"ids": ids})
        await session.commit()

    for iid in ids:                              # drop their face/body vectors
        try:
            await vector_store.delete_for_identity(iid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("clear_unknowns: vector delete failed for %d: %s", iid, exc)
    logger.info("Cleared %d unknown(s)", len(ids))
    return len(ids)


@router.post("/clear-unknowns")
async def clear_unknowns_endpoint(_: str = Depends(require_admin)) -> dict:
    """Manual 'clear today's unknowns' — same as the automatic midnight sweep."""
    removed = await clear_unknowns()
    return {"status": "ok", "removed": removed, "message": f"Cleared {removed} unknown(s). Visitors kept."}


@router.post("/consolidate")
async def consolidate_endpoint(apply: bool = False, _: str = Depends(require_admin)) -> dict:
    """Gallery consolidation (Phase 2): fold VISITOR identities that are really the
    SAME person (matching face-gallery centroids) into their oldest id. Runs
    IN-PROCESS so it shares the Brain's embedded-Qdrant client (a standalone script
    can't open the same on-disk Qdrant while the Brain holds it).

    ?apply=false (default) → DRY RUN: returns the merge plan, changes nothing.
    ?apply=true            → executes the merges and commits."""
    async with get_session() as session:
        plans = await dedup_service.consolidate_visitors(session, apply=apply)
        if apply:
            await session.commit()
    merges = [
        {"keep": p.primary, "keep_label": p.primary_label,
         "merged": p.duplicates, "merged_labels": p.labels, "similarity": p.similarity}
        for p in plans
    ]
    total = sum(len(p.duplicates) for p in plans)
    return {
        "status": "ok",
        "applied": apply,
        "duplicates_found": total,
        "clusters": len(merges),
        "merges": merges,
        "message": (f"Merged {total} duplicate visitor(s) into {len(merges)} identities."
                    if apply else
                    f"DRY RUN — found {total} duplicate(s) in {len(merges)} cluster(s). "
                    f"POST again with ?apply=true to merge."),
    }


@router.get("/calibration")
async def calibration_endpoint() -> dict:
    """Live self-calibration state (read-only, no auth — aggregate stats only, no
    PII). Use this to debug matching: `impostor_samples` must reach CALIB_WARMUP
    before `warmed_up` flips true; until then the cold-start defaults are used.
    `match_threshold` is the live online assign floor; `merge_threshold` is the
    (stricter) deferred-clustering floor. If people fragment, watch these vs the
    `resolve: s1=… thr=…` log lines."""
    return {"status": "ok", **calibration_service.stats()}


@router.get("/suggested-links")
async def suggested_links(_: str = Depends(require_admin)) -> dict:
    """Manual-review list: pairs of Unknown cases (or case ↔ person) whose BODY
    similarity is plausible (≥ BODY_REVIEW_THRESHOLD) but below the automatic
    re-link bar, seen on the SAME camera within BODY_REVIEW_WINDOW_MINUTES.
    Each suggestion carries the score, time gap, camera, and both parties'
    evidence thumbnails — a human approves (merge) or dismisses. Nothing here
    is applied automatically."""
    import config as _cfg
    from datetime import datetime, timedelta
    from repositories import embedding_repo
    from sqlalchemy import text as _text

    out: list[dict] = []
    async with get_session() as session:
        since = datetime.utcnow() - timedelta(minutes=_cfg.BODY_REVIEW_WINDOW_MINUTES)
        # Recent unknown cases with their latest sighting (camera + evidence).
        rows = (await session.execute(_text(
            """
            SELECT DISTINCT ON (e.identity_id)
                   e.identity_id, i.display_label, e.camera_id, c.camera_uid,
                   e.detected_at, e.body_path, e.face_path
              FROM detection_events e
              JOIN identities i ON i.id = e.identity_id
              LEFT JOIN cameras c ON c.id = e.camera_id
             WHERE i.identity_type = 'unknown'
               AND e.detected_at >= :since AND e.hidden_at IS NULL
             ORDER BY e.identity_id, e.detected_at DESC
            """), {"since": since})).all()
        latest = {r[0]: r for r in rows}

        for case_id, r in latest.items():
            vectors = await embedding_repo.fetch_body_vectors(case_id)
            best: dict[int, float] = {}
            for v in vectors:
                for hid, sim in await embedding_repo.search_body(v, limit=8):
                    if hid == case_id or sim < _cfg.BODY_REVIEW_THRESHOLD:
                        continue
                    if sim > best.get(hid, 0.0):
                        best[hid] = sim
            for hid, sim in sorted(best.items(), key=lambda kv: -kv[1])[:3]:
                other = latest.get(hid)
                # The counterpart may be a visitor/employee — fetch its context.
                if other is None:
                    orow = (await session.execute(_text(
                        """
                        SELECT e.identity_id, i.display_label, e.camera_id, c.camera_uid,
                               e.detected_at, e.body_path, e.face_path
                          FROM detection_events e
                          JOIN identities i ON i.id = e.identity_id
                          LEFT JOIN cameras c ON c.id = e.camera_id
                         WHERE e.identity_id = :iid AND e.hidden_at IS NULL
                         ORDER BY e.detected_at DESC LIMIT 1
                        """), {"iid": hid})).first()
                    if orow is None:
                        continue
                    other = orow
                if other[2] != r[2]:
                    continue                      # same-camera constraint (uniform guard)
                if case_id > hid and hid in latest:
                    continue                      # avoid listing (a,b) and (b,a)
                gap = abs((r[4] - other[4]).total_seconds())
                out.append({
                    "a_id": case_id, "a_label": r[1], "a_body": r[5], "a_face": r[6],
                    "b_id": hid, "b_label": other[1], "b_body": other[5], "b_face": other[6],
                    "body_similarity": round(sim, 3),
                    "camera": r[3], "time_gap_seconds": int(gap),
                    "auto_link_bar": _cfg.BODY_MERGE_THRESHOLD,
                })
    out.sort(key=lambda s: -s["body_similarity"])
    return {"status": "ok", "count": len(out), "suggestions": out[:50]}
