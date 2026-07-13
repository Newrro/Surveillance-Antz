"""
workers/consolidation.py — periodic in-process deferred face clustering.

This is the SOURCE OF TRUTH for identity in the 2026 design. Online matching is
recall-first and may briefly create a duplicate Visitor when a returning face
misses; this worker folds those duplicates back by best-of-set face clustering
(dedup_service.consolidate_visitors) into the oldest id, on a short timer — so the
gallery converges to one-identity-per-person within a cycle.

MUST run in-process: it shares the Brain's embedded-Qdrant client, which a separate
process can't open while the Brain holds it (unlike prune_events, Postgres-only).
Independent of the midnight-flush scheduler (which run.sh disables). Gated by
config.CONSOLIDATE_ENABLE.

THRESHOLD: self-calibrated (calibration_service.merge_threshold) — mean impostor
similarity + K·std, clamped — so merges stay well above the impostor ceiling and
adapt to the deployed cameras with no hand-tuning. Applies by default
(CONSOLIDATE_APPLY=True); set it to 0 to fall back to log-only review. Humans can
still merge/split manually via Settings → "Merge duplicate visitors".

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
    from services import calibration_service
    apply = bool(config.CONSOLIDATE_APPLY)
    # face_threshold=None → consolidate_visitors uses the self-calibrated merge
    # threshold (mean impostor + K·std), which adapts to the deployed cameras.
    logger.info("calibration: %s", calibration_service.stats())
    try:
        async with get_session() as session:
            plans = await dedup_service.consolidate_visitors(
                session, face_threshold=None, apply=apply
            )
            if apply:
                await session.commit()
    except Exception as e:  # noqa: BLE001 — keep the loop alive
        logger.warning("consolidation pass failed: %s", e)
        return 0

    total = sum(len(p.duplicates) for p in plans)
    if not plans:
        logger.info("consolidation: no duplicates (apply=%s)", apply)
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
        "consolidation loop every %.0f min (apply=%s, self-calibrated merge threshold)",
        period / 60.0, config.CONSOLIDATE_APPLY,
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
