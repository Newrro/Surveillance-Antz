"""
services/presence_cache.py
==========================
Strictly Redis-facing — the live "where is X right now?" layer.

The Postgres `presence_sessions` table is the source of truth for
historical entry/exit times.  This Redis cache is the source of truth
for "as of this exact millisecond, which camera is currently looking at
this person?".

Key layout:
    presence:current:{identity_id}  → Redis Hash
        camera_id   : <int>
        zone_id     : <str>
        last_seen   : <iso8601 utc>
    TTL = SESSION_TIMEOUT_SECONDS (300s by default — refresh on every
    detection so a continuously-tracked person never expires).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as redis

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection — created lazily on first use so import-time never blocks.
# ---------------------------------------------------------------------------
_client: Optional[redis.Redis] = None


async def get_client() -> redis.Redis:
    """Return the shared async Redis client (singleton)."""
    global _client
    if _client is None:
        _client = redis.from_url(
            config.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_keepalive=True,
        )
    return _client


async def close_client() -> None:
    """Called on app shutdown — cleanly closes the Redis pool."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _key(identity_id: int) -> str:
    return f"presence:current:{identity_id}"


async def touch(
    identity_id: int,
    camera_id: int,
    zone_id: str,
    ttl: Optional[int] = None,
) -> None:
    """
    Create / refresh the live-presence hash for an identity.

    Args:
        identity_id — the surrogate identity PK
        camera_id   — PK of the camera currently looking at them
        zone_id     — zone label of that camera
        ttl         — override SESSION_TIMEOUT_SECONDS for testing
    """
    client = await get_client()
    payload = {
        "camera_id": str(camera_id),
        "zone_id": zone_id,
        "last_seen": datetime.now(timezone.utc).isoformat(),
    }
    key = _key(identity_id)
    pipe = client.pipeline()
    pipe.hset(key, mapping=payload)
    pipe.expire(key, ttl or config.SESSION_TIMEOUT_SECONDS)
    await pipe.execute()


async def get(identity_id: int) -> Optional[dict]:
    """
    Read the live-presence hash.  Returns None if missing/expired.

    Returned dict shape:
        {"camera_id": int, "zone_id": str, "last_seen": str}
    """
    client = await get_client()
    raw = await client.hgetall(_key(identity_id))
    if not raw:
        return None
    return {
        "camera_id": int(raw["camera_id"]),
        "zone_id": raw["zone_id"],
        "last_seen": raw["last_seen"],
    }


async def evict(identity_id: int) -> None:
    """Delete the live-presence hash — called when a person exits."""
    client = await get_client()
    await client.delete(_key(identity_id))


async def evict_all() -> int:
    """
    Delete ALL presence:current:* keys — used by the midnight flush worker.

    Returns the number of keys deleted.
    """
    client = await get_client()
    count = 0
    async for key in client.scan_iter(match="presence:current:*"):
        await client.delete(key)
        count += 1
    return count


async def list_inside() -> list[dict]:
    """
    Convenience — list all currently-inside identities with their cached
    camera/zone.  Useful for admin dashboards.

    NOTE: This is a SCAN over all presence:current:* keys — fine for a
    few thousand identities; do NOT use it in tight polling loops.
    """
    client = await get_client()
    out: list[dict] = []
    async for key in client.scan_iter(match="presence:current:*"):
        # key = "presence:current:123"
        identity_id = int(key.split(":")[-1])
        data = await get(identity_id)
        if data is not None:
            out.append({"identity_id": identity_id, **data})
    return out
