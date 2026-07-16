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

    # ---- Tiered retention (2026-07 rework) ------------------------------
    # The old single RETENTION_DAYS conflated two very different things and
    # caused real record loss. They are now split:
    #
    #   EVENT_RETENTION_DAYS  — detection_events ROWS (who/where/when). These are
    #       tiny (~0.5 KB) and archived to JSONL nightly, so we keep them LONG
    #       (movement history / attendance). 0 = keep forever.
    #   MEDIA_RETENTION_DAYS  — per-sighting JPEGs under storage/img (the bulk).
    #       Forensic media whose value decays with age; pruned on this window
    #       (with STORAGE_MAX_GB as a size backstop). Losing an old frame does
    #       NOT lose the sighting row or the person's durable profile photo
    #       (storage/profiles/<id>.jpg, never pruned).
    #
    # RETENTION_DAYS is kept as a back-compat fallback for either if unset.
    RETENTION_DAYS: int = 7
    EVENT_RETENTION_DAYS: int = 365
    MEDIA_RETENTION_DAYS: int = 30
