"""
scripts/seed.py
===============
Idempotent seed — run once after `alembic upgrade head`.

Injects:
    - Cameras from the shared registry (surveillance_Camera_config) — the same
      source Part 1 and the UI use, so camera UIDs line up across all parts.
    - NO people. The facility starts empty; employees are enrolled via
      POST /employees (real Part-1 embedding) or by promoting a visitor.
      Set SEED_SAMPLE_EMPLOYEE=1 to re-enable the "Asha R." demo record.

Idempotency:
    - Cameras inserted only if the camera_uid is missing.
    - The (optional) sample employee is inserted only if EMP-2026-0001 doesn't exist.

The synthetic embeddings are deterministic (SHA256-seeded, L2-normalized)
so re-running never pollutes Qdrant with new random vectors — but note that
Qdrant upserts use fresh point-ids, so the guard is the Postgres identity
check above.

Usage:
    python scripts/seed.py
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))          # surveillance_brain
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))   # repo root (shared config)

import config  # noqa: E402
from db import vector_store  # noqa: E402
from db.connection import get_session  # noqa: E402
from db.models import IdentityType  # noqa: E402
from repositories import camera_repo, embedding_repo, identity_repo  # noqa: E402
from surveillance_Camera_config.loader import load_cameras, to_brain_records  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("seed")


# Cameras come from the shared registry (surveillance_Camera_config) — the SAME
# source the UI and Part 1 use, so camera UIDs line up across all three parts.
CAMERAS = to_brain_records(load_cameras(active_only=True))

SAMPLE_EMPLOYEE = {
    "name": "Asha R.",
    "department": "Engineering",
    "email": "asha.r@example.com",
    "label": "EMP-2026-0001",
    "seq": 1,
    "year": 2026,
}


def _synthetic_embedding(seed_string: str) -> list[float]:
    """Deterministic, L2-normalized embedding of length EMBEDDING_DIMENSIONS."""
    dim = config.EMBEDDING_DIMENSIONS
    floats: list[float] = []
    counter = 0
    while len(floats) < dim:
        h = hashlib.sha256(f"{seed_string}:{counter}".encode("utf-8")).digest()
        for i in range(0, len(h), 4):
            val = int.from_bytes(h[i:i + 4], "big", signed=False) / 2**32 - 0.5
            floats.append(val * 2.0)
            if len(floats) >= dim:
                break
        counter += 1
    vec = floats[:dim]
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm > 0 else vec


async def _wait_for_qdrant(retries: int = 30, delay: float = 2.0) -> None:
    """Qdrant may still be warming up at compose boot — retry the bootstrap."""
    for attempt in range(1, retries + 1):
        try:
            await vector_store.ensure_collections()
            logger.info("Qdrant collections ready")
            return
        except Exception as e:  # noqa: BLE001
            logger.info("Qdrant not ready (attempt %d/%d): %s", attempt, retries, e)
            await asyncio.sleep(delay)
    raise RuntimeError("Qdrant did not become ready in time")


async def seed_cameras(session) -> int:
    inserted = 0
    for cam in CAMERAS:
        if await camera_repo.fetch_camera_by_uid(session, cam["camera_uid"]) is not None:
            continue
        await camera_repo.insert_camera(session, **cam)
        inserted += 1
        logger.info("Inserted camera: %s (%s)", cam["camera_uid"], cam["name"])
    return inserted


async def seed_sample_employee(session) -> bool:
    if await identity_repo.fetch_identity_by_label(session, SAMPLE_EMPLOYEE["label"]) is not None:
        logger.info("Sample employee %s already exists — skipping", SAMPLE_EMPLOYEE["label"])
        return False

    identity = await identity_repo.create_identity(
        session, IdentityType.EMPLOYEE, SAMPLE_EMPLOYEE["label"]
    )
    await identity_repo.insert_employee(
        session,
        identity_id=identity.id,
        employee_seq=SAMPLE_EMPLOYEE["seq"],
        year=SAMPLE_EMPLOYEE["year"],
        name=SAMPLE_EMPLOYEE["name"],
        department=SAMPLE_EMPLOYEE["department"],
        email=SAMPLE_EMPLOYEE["email"],
    )
    await embedding_repo.store_embeddings(
        identity.id,
        face_embedding=_synthetic_embedding(SAMPLE_EMPLOYEE["label"] + ":face"),
        body_embedding=_synthetic_embedding(SAMPLE_EMPLOYEE["label"] + ":body"),
        source="seed_synthetic",
    )
    logger.info("Inserted sample employee: %s (%s)", SAMPLE_EMPLOYEE["label"], SAMPLE_EMPLOYEE["name"])
    return True


async def main() -> None:
    logger.info("Seeding (pg=%s qdrant=%s)", config.DATABASE_DSN.split("@")[-1], config.QDRANT_URL)
    await _wait_for_qdrant()
    async with get_session() as session:
        cams = await seed_cameras(session)
        # Sample employee intentionally NOT seeded: the facility starts with zero
        # enrolled people. Employees are created via POST /employees (with a real
        # Part-1 embedding) or by promoting a visitor. Set SEED_SAMPLE_EMPLOYEE=1
        # to re-enable the "Asha R." demo record.
        emp = False
        if os.environ.get("SEED_SAMPLE_EMPLOYEE", "0") == "1":
            emp = await seed_sample_employee(session)
    await vector_store.close_client()
    logger.info("Seed complete: cameras_inserted=%d sample_employee_inserted=%s", cams, emp)


if __name__ == "__main__":
    asyncio.run(main())
