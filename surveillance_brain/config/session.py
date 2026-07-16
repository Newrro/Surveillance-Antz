from __future__ import annotations

from pydantic import BaseModel


class _SessionCronSettings(BaseModel):
    # ---- Session + cron -------------------------------------------------
    # TTL on the Redis live-presence hash (seconds).  This is the ONLY
    # place a TTL is applied — Postgres records are permanent.
    SESSION_TIMEOUT_SECONDS: int = 300

    # Short window (seconds) during which repeated detections of the same
    # person on the same camera are treated as duplicates and NOT re-logged.
    DUPLICATE_WINDOW_SECONDS: int = 30

    # APScheduler cron expressions (UTC).
    MIDNIGHT_FLUSH_CRON: str = "0 0 * * *"   # close dangling sessions @ 00:00
    ARCHIVE_CRON: str = "*/30 * * * *"       # export logs+datasheet every 30 min
    RETENTION_CRON: str = "30 0 * * *"       # prune old detection_events @ 00:30

    # Delete detection_events older than this many days (0 = keep forever). They
    # are archived to JSONL first (ARCHIVE_CRON), so this only trims the live
    # ledger — the unbounded table on a 24/7 system. Vectors are per-identity
    # (bounded) and unknowns are cleared daily, so only this table needs ageing.
    RETENTION_DAYS: int = 7
