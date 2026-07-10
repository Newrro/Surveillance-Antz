"""
workers/consolidation.py — periodic in-process gallery consolidation (Phase 2).

Folds VISITOR identities that are really the same person (matching face-gallery
centroids) into their oldest id, on a timer. This MUST run in-process: it shares
the Brain's embedded-Qdrant client, which a separate process can't open while the
Brain holds it (unlike prune_events, which is Postgres-only and safe standalone).

Independent of the midnight-flush scheduler (which run.sh disables), so it works
in native mode too. Gated by config.CONSOLIDATE_ENABLE.

SAFETY: defaults to LOG-ONLY (CONSOLIDATE_APPLY=False) — each cycle logs the merge
plan without touching data, because auto-merging a borderline face match can fold
two different people together. When apply IS enabled it uses the stricter
CONSOLIDATE_AUTO_FACE_THRESHOLD (not the looser manual-button threshold). Humans
apply merges via Settings → "Merge duplicate visitors" after reviewing the plan.

Started/stopped by api.main's lifespan.
"""
from __future__ import annotations

import asyncio
import logging

import config
from db.connection import get_session
from services import dedup_service

logger = logging.getLogger("consolidation")

_task: asyncio.Task | None = None


async def run_once() -> int:
    """One consolidation pass. Returns the number of duplicate ids folded (or, in
    log-only mode, the number that WOULD be folded). Never raises to the caller's
    loop — logs and returns 0 on error."""
    apply = bool(config.CONSOLIDATE_APPLY)
    thr = config.CONSOLIDATE_AUTO_FACE_THRESHOLD if apply else config.CONSOLIDATE_FACE_THRESHOLD
    try:
        async with get_session() as session:
            plans = await dedup_service.consolidate_visitors(
                session, face_threshold=thr, apply=apply
            )
            if apply:
                await session.commit()
    except Exception as e:  # noqa: BLE001 — keep the loop alive
        logger.warning("consolidation pass failed: %s", e)
        return 0

    total = sum(len(p.duplicates) for p in plans)
    if not plans:
        logger.info("consolidation: no duplicates (thr=%.2f, apply=%s)", thr, apply)
        return 0
    for p in plans:
        logger.info(
            "consolidation %s: keep %s ← %s (face sim %.3f)",
            "APPLIED" if apply else "PLAN (log-only, set CONSOLIDATE_APPLY=1 to enable)",
            p.primary_label, ", ".join(p.labels), p.similarity,
        )
    return total


async def _loop() -> None:
    period = max(60.0, config.CONSOLIDATE_EVERY_MINUTES * 60.0)
    logger.info(
        "consolidation loop every %.0f min (apply=%s, thr=%.2f)",
        period / 60.0, config.CONSOLIDATE_APPLY,
        config.CONSOLIDATE_AUTO_FACE_THRESHOLD if config.CONSOLIDATE_APPLY
        else config.CONSOLIDATE_FACE_THRESHOLD,
    )
    while True:
        await asyncio.sleep(period)
        await run_once()


def start() -> None:
    """Launch the periodic task on the running event loop. Idempotent."""
    global _task
    if not config.CONSOLIDATE_ENABLE:
        logger.info("consolidation disabled (CONSOLIDATE_ENABLE=0)")
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop())
    logger.info("consolidation worker started")


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _task = None
