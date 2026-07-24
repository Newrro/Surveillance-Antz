"""
services/occupancy_service.py — live headcount for the console Grid.
====================================================================
Two metrics, computed from `detection_events` (real timestamps, server "today"):

  visits  — unique IDENTIFIED people seen today on ANY camera. A unique id is
            counted once regardless of how many cameras saw it or how many times.
  inside  — of those, the ones seen on ANY camera within the last
            OCCUPANCY_INSIDE_MINUTES. Anyone not seen for that long drops off
            (they left / went out of coverage).

This is the "any-camera" model: there is no entry/exit-camera distinction. A
person counts as a visit the moment any camera identifies them, and counts as
inside for as long as some camera keeps seeing them within the recency window.

Id-less Unknowns are excluded — they can't be de-duplicated into a unique count.
"""

from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy import text

import config
from db.connection import get_session


# Everyone identified on ANY camera today — one row per unique identity.
_ENTERED_SQL = text(
    """
    SELECT DISTINCT de.identity_id
    FROM detection_events de
    WHERE de.identity_id IS NOT NULL
      AND de.detected_at::date = CURRENT_DATE
    """
)

# Of the entered ids: those seen (on any camera) within the recency window.
_INSIDE_SQL = text(
    """
    SELECT DISTINCT de.identity_id
    FROM detection_events de
    WHERE de.identity_id = ANY(:ids)
      AND de.detected_at >= NOW() - make_interval(mins => :mins)
    """
)


async def occupancy() -> Dict[str, Any]:
    minutes = int(config.OCCUPANCY_INSIDE_MINUTES)

    async with get_session() as session:
        entered: List[int] = [int(i) for i in (
            await session.execute(_ENTERED_SQL)
        ).scalars().all()]
        if not entered:
            return {"visits": 0, "inside": 0, "entered_ids": [], "inside_ids": [],
                    "window_minutes": minutes}
        inside: List[int] = [int(i) for i in (
            await session.execute(_INSIDE_SQL, {"ids": entered, "mins": minutes})
        ).scalars().all()]

    return {
        "visits": len(entered),
        "inside": len(inside),
        "entered_ids": entered,
        "inside_ids": inside,
        "window_minutes": minutes,
    }
