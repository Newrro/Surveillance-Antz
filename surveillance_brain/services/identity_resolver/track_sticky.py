"""identity_resolver/track_sticky.py — Redis track-sticky identity cache."""

from __future__ import annotations

from typing import Optional

import config
from services import presence_cache


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
