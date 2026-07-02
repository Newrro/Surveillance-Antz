"""
services/live_broadcaster.py
============================
In-process pub/sub for the WS /live real-time event feed (Part 2 → Part 3).

Every accepted, non-duplicate detection is published here by
ingestion_service; every connected WebSocket subscribes and receives the
event object as JSON.

Design:
    - A set of asyncio.Queue subscribers.
    - publish() fan-outs to all queues (drops on full queue rather than
      blocking ingest — the live feed is best-effort, the ledger is the
      source of truth).
    - Each WebSocket handler owns one subscription for its lifetime.

Scaling note:
    This is single-process.  Running multiple app workers means each only
    sees the events it ingested.  To scale horizontally, swap this module's
    internals for Redis Pub/Sub (publish to a channel, each worker
    subscribes) — the public API (publish / subscribe) stays identical.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Dict, Set

logger = logging.getLogger(__name__)

_subscribers: Set["asyncio.Queue[Dict[str, Any]]"] = set()
_MAX_QUEUE = 100


async def publish(event: Dict[str, Any]) -> None:
    """Fan-out an event to all live subscribers (best-effort, non-blocking)."""
    dead: list = []
    for q in list(_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Live subscriber queue full — dropping event for one client")
        except Exception:  # noqa: BLE001
            dead.append(q)
    for q in dead:
        _subscribers.discard(q)


class Subscription:
    """Async context manager yielding an event stream for one WebSocket."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=_MAX_QUEUE)

    async def __aenter__(self) -> "Subscription":
        _subscribers.add(self._queue)
        logger.debug("Live subscriber added (total=%d)", len(_subscribers))
        return self

    async def __aexit__(self, *exc: object) -> None:
        _subscribers.discard(self._queue)
        logger.debug("Live subscriber removed (total=%d)", len(_subscribers))

    async def stream(self) -> AsyncIterator[Dict[str, Any]]:
        while True:
            yield await self._queue.get()


def subscriber_count() -> int:
    return len(_subscribers)
