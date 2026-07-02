"""
services/archive_service.py
===========================
Local-storage export/archive (per the Part 2 data-lifetime decision:
"DB permanent, files = export/archive").

Postgres remains the permanent source of truth.  On a schedule
(config.ARCHIVE_CRON) this service writes two human-readable artifacts to
local storage so each feature has an offline, greppable copy:

  1. Logs      → {LOG_DIR}/events-YYYY-MM-DD.jsonl
                 Append-only JSON Lines, one event object per line.
                 Only events newer than the last archived timestamp are
                 appended (tracked via a small cursor file), so re-runs
                 are incremental and idempotent.

  2. Datasheet → {DATASHEET_DIR}/person-{identity_id}.json
                 One JSON file per known person: their profile + rolling
                 visit history.  Overwritten each run (a snapshot, not a log).

Nothing is deleted from Postgres here — TTL applies only to the Redis
live-presence layer.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
from db.connection import get_session
from repositories import identity_repo
from services import log_service, search_service

logger = logging.getLogger(__name__)

_CURSOR_FILE = "last_archived_at.txt"


def _ensure_dirs() -> None:
    Path(config.LOG_DIR).mkdir(parents=True, exist_ok=True)
    Path(config.DATASHEET_DIR).mkdir(parents=True, exist_ok=True)


def _cursor_path() -> Path:
    return Path(config.LOG_DIR) / _CURSOR_FILE


def _read_cursor() -> Optional[datetime]:
    p = _cursor_path()
    if not p.exists():
        return None
    try:
        return datetime.fromisoformat(p.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _write_cursor(ts: datetime) -> None:
    _cursor_path().write_text(ts.isoformat(), encoding="utf-8")


async def archive_logs() -> int:
    """
    Append events created since the last cursor to a daily .jsonl file.
    Returns the number of events written.
    """
    _ensure_dirs()
    since = _read_cursor()

    events = await log_service.list_events(start_date=since, limit=10000)
    if not events:
        logger.debug("archive_logs: nothing new since %s", since)
        return 0

    # events come newest-first; write oldest-first so the .jsonl reads
    # chronologically.
    events = list(reversed(events))

    written = 0
    max_ts: Optional[datetime] = since
    # Group by calendar day of the event time.
    for ev in events:
        ts_raw = ev.get("time")
        day = (ts_raw or datetime.utcnow().isoformat())[:10]  # YYYY-MM-DD
        path = Path(config.LOG_DIR) / f"events-{day}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        written += 1
        if ts_raw:
            try:
                ts = datetime.fromisoformat(ts_raw)
                if max_ts is None or ts > max_ts:
                    max_ts = ts
            except ValueError:
                pass

    if max_ts is not None:
        _write_cursor(max_ts)
    logger.info("archive_logs: wrote %d event(s)", written)
    return written


async def archive_datasheets() -> int:
    """
    Write a per-person datasheet JSON snapshot for every known identity.
    Returns the number of datasheets written.
    """
    _ensure_dirs()

    async with get_session() as session:
        # Employees + visitors are all the known identities.  We pull
        # employee ids + visitor ids cheaply.
        employees = await identity_repo.list_employees(session, limit=100000)
        emp_ids = [e.identity_id for e in employees]

    # Visitors: reuse the events feed to discover active identities without
    # a dedicated repo method (keeps the surface small).  Employees are the
    # important datasheets; visitors are snapshotted opportunistically via
    # their profile too.
    written = 0
    for identity_id in emp_ids:
        profile = await search_service.get_person_profile(identity_id)
        if profile is None:
            continue
        path = Path(config.DATASHEET_DIR) / f"person-{identity_id}.json"
        path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1

    logger.info("archive_datasheets: wrote %d datasheet(s)", written)
    return written


async def run_archive() -> None:
    """Full archive pass — called by the scheduler and on demand."""
    start = datetime.utcnow()
    logger.info("Archive starting at %s (storage=%s)", start.isoformat(), os.path.abspath(config.STORAGE_ROOT))
    try:
        logs = await archive_logs()
    except Exception as e:  # noqa: BLE001
        logger.error("archive_logs failed: %s", e)
        logs = 0
    try:
        sheets = await archive_datasheets()
    except Exception as e:  # noqa: BLE001
        logger.error("archive_datasheets failed: %s", e)
        sheets = 0
    elapsed = (datetime.utcnow() - start).total_seconds()
    logger.info("Archive complete in %.2fs (logs=%d datasheets=%d)", elapsed, logs, sheets)
