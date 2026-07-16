"""
workers/midnight_flush.py
=========================
Background schedulers (APScheduler, AsyncIOScheduler on the FastAPI loop):

  1. Midnight flush (config.MIDNIGHT_FLUSH_CRON, default 00:00 UTC):
        UPDATE presence_sessions SET status='exited', exit_at=NOW()
        WHERE status='inside';
        + evict all Redis presence:current:* keys.
     Closes sessions left dangling by people who were never seen exiting.

  2. Archive (config.ARCHIVE_CRON, default every 30 min):
        Export new events → JSONL and per-person datasheets → JSON in
        local storage (services.archive_service).  Postgres stays intact.

Started/stopped by api.main's lifespan.
"""

from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

import config
from db.connection import get_session
from services import archive_service, presence_cache

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
async def run_midnight_flush() -> None:
    """Close all open presence_sessions and clear the Redis presence cache."""
    start = datetime.utcnow()
    logger.info("Midnight flush starting at %s", start.isoformat())

    closed_count = 0
    try:
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE presence_sessions "
                    "SET status = 'exited', exit_at = NOW() "
                    "WHERE status = 'inside'"
                )
            )
            closed_count = result.rowcount or 0
        logger.info("Midnight flush: closed %d dangling session(s)", closed_count)
    except Exception as e:  # noqa: BLE001
        logger.error("Midnight flush: Postgres update failed: %s", e)

    evicted_count = 0
    try:
        evicted_count = await presence_cache.evict_all()
        logger.info("Midnight flush: evicted %d Redis presence key(s)", evicted_count)
    except Exception as e:  # noqa: BLE001
        logger.error("Midnight flush: Redis evict failed: %s", e)

    # Daily fresh start: drop all Unknowns (unconfirmed people). Confirmed
    # Visitors + Employees are kept so returning people are still recognised.
    cleared = 0
    try:
        from api.routers.admin import clear_unknowns
        cleared = await clear_unknowns()
        logger.info("Midnight flush: cleared %d unknown(s)", cleared)
    except Exception as e:  # noqa: BLE001
        logger.error("Midnight flush: clear-unknowns failed: %s", e)

    elapsed = (datetime.utcnow() - start).total_seconds()
    logger.info(
        "Midnight flush complete in %.2fs (sessions_closed=%d, redis_keys_evicted=%d, unknowns_cleared=%d)",
        elapsed, closed_count, evicted_count, cleared,
    )


async def run_archive() -> None:
    """Delegate to the archive service (JSONL logs + datasheet snapshots)."""
    await archive_service.run_archive()


async def run_retention() -> None:
    """Delete detection_events older than config.EVENT_RETENTION_DAYS. They are archived
    to JSONL first (run_archive), so this only trims the live ledger — the one
    table that grows without bound on a 24/7 system. No-op when RETENTION_DAYS<=0."""
    if config.EVENT_RETENTION_DAYS <= 0:
        return
    start = datetime.utcnow()
    deleted = 0
    try:
        async with get_session() as session:
            result = await session.execute(
                text(
                    "DELETE FROM detection_events "
                    "WHERE detected_at < NOW() - make_interval(days => :days)"
                ),
                {"days": int(config.EVENT_RETENTION_DAYS)},
            )
            deleted = result.rowcount or 0
        logger.info(
            "Retention: deleted %d detection_event(s) older than %d day(s) in %.2fs",
            deleted, config.EVENT_RETENTION_DAYS, (datetime.utcnow() - start).total_seconds(),
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Retention: detection_events prune failed: %s", e)


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------
def _cron(expr: str) -> CronTrigger:
    return CronTrigger.from_crontab(expr)


def start_scheduler() -> AsyncIOScheduler:
    """Register both jobs and start the scheduler.  Idempotent."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    _scheduler = AsyncIOScheduler(timezone="UTC")

    _scheduler.add_job(
        run_midnight_flush,
        trigger=_cron(config.MIDNIGHT_FLUSH_CRON),
        id="midnight_flush",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    _scheduler.add_job(
        run_archive,
        trigger=_cron(config.ARCHIVE_CRON),
        id="archive",
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
    )
    _scheduler.add_job(
        run_retention,
        trigger=_cron(config.RETENTION_CRON),
        id="retention",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )

    _scheduler.start()
    logger.info(
        "Schedulers registered (flush=%r archive=%r retention=%r keep=%dd)",
        config.MIDNIGHT_FLUSH_CRON, config.ARCHIVE_CRON,
        config.RETENTION_CRON, config.EVENT_RETENTION_DAYS,
    )
    return _scheduler


async def stop_scheduler() -> None:
    """Graceful shutdown — called from api.main's lifespan."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Schedulers stopped")
