#!/usr/bin/env python3
"""
scripts/prune_events.py — age out old detection_events rows (Postgres-only).

The Brain's APScheduler retention job (workers/midnight_flush.run_retention)
covers Docker mode. Under run.sh (native, embedded Qdrant) the scheduler is
disabled (ENABLE_MIDNIGHT_FLUSH=0), so run.sh runs THIS instead — it touches
only Postgres (no Qdrant client), so it's safe alongside the embedded-Qdrant
Brain process.

Deletes detection_events older than config.RETENTION_DAYS (0 = keep forever).
Rows are exported to JSONL by the archive job before this trims the live ledger.

Run once:      python scripts/prune_events.py
Run as a loop: python scripts/prune_events.py --loop   (every PRUNE_INTERVAL_S, default 1h)
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                       # noqa: E402
from db.connection import get_session  # noqa: E402
from sqlalchemy import text         # noqa: E402

PRUNE_INTERVAL_S = float(os.environ.get("PRUNE_INTERVAL_S", "3600"))


async def prune_once() -> int:
    if config.RETENTION_DAYS <= 0:
        return 0
    async with get_session() as session:
        result = await session.execute(
            text(
                "DELETE FROM detection_events "
                "WHERE detected_at < NOW() - make_interval(days => :days)"
            ),
            {"days": int(config.RETENTION_DAYS)},
        )
    deleted = result.rowcount or 0
    if deleted:
        print(f"[prune-events] deleted {deleted} row(s) older than "
              f"{config.RETENTION_DAYS}d", flush=True)
    return deleted


async def _loop() -> None:
    print(f"[prune-events] loop every {PRUNE_INTERVAL_S:g}s "
          f"(keep {config.RETENTION_DAYS}d)", flush=True)
    while True:
        try:
            await prune_once()
        except Exception as e:  # noqa: BLE001 — keep the loop alive
            print(f"[prune-events] error: {e}", flush=True)
        await asyncio.sleep(PRUNE_INTERVAL_S)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    args = ap.parse_args()
    asyncio.run(_loop() if args.loop else prune_once())


if __name__ == "__main__":
    main()
