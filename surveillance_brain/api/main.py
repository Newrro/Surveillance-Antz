"""
api/main.py
===========
FastAPI application entrypoint for the Surveillance Brain (Part 2).

Wires up:
    - routers: events, person, employees, live (WS), search, identities, logs
    - lifespan: ping Postgres, ensure Qdrant collections + storage dirs,
      start the midnight-flush + archive schedulers
    - /health endpoint (DB + Redis + Qdrant status)
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware

import config
from api.routers import (
    admin, attendance, employees, events, identities, live, logs, person, search,
)
from api.schemas import HealthResponse
from db import vector_store
from db.connection import init_db
from services import presence_cache

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Booting Surveillance Brain (env=%s)", config.ENVIRONMENT)

    # Postgres liveness (log but don't crash — /health reports the truth).
    try:
        await init_db()
    except Exception as e:  # noqa: BLE001
        logger.error("DB liveness check failed on startup: %s", e)

    # Qdrant collections (face + body).
    try:
        await vector_store.ensure_collections()
    except Exception as e:  # noqa: BLE001
        logger.error("Qdrant collection bootstrap failed: %s", e)

    # Local storage dirs for the archive.
    for d in (config.STORAGE_ROOT, config.LOG_DIR, config.DATASHEET_DIR):
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error("Could not create storage dir %s: %s", d, e)

    # Background schedulers (midnight flush + archive) unless disabled.
    if os.environ.get("ENABLE_MIDNIGHT_FLUSH", "1") == "1":
        try:
            from workers.midnight_flush import start_scheduler
            start_scheduler()
            logger.info(
                "Schedulers started (flush=%s, archive=%s)",
                config.MIDNIGHT_FLUSH_CRON, config.ARCHIVE_CRON,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to start schedulers: %s", e)

    # Periodic gallery consolidation — independent of the flush scheduler (so it
    # runs in native mode too) and in-process (shares the embedded Qdrant).
    try:
        from workers import consolidation
        consolidation.start()
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to start consolidation worker: %s", e)

    yield

    logger.info("Shutting down — closing Redis + Qdrant")
    try:
        from workers.midnight_flush import stop_scheduler
        await stop_scheduler()
    except Exception as e:  # noqa: BLE001
        logger.warning("Scheduler shutdown error: %s", e)
    try:
        from workers import consolidation
        await consolidation.stop()
    except Exception as e:  # noqa: BLE001
        logger.warning("Consolidation shutdown error: %s", e)
    await presence_cache.close_client()
    await vector_store.close_client()


app = FastAPI(
    title="Surveillance Brain",
    description=(
        "Project RUNG01 — Part 2.  Database + Tracking + Identity engine for "
        "an AI-driven facility surveillance network.  Ingests detections from "
        "Part 1 (Perception), resolves identities against Qdrant embeddings, "
        "tracks live presence, and serves logs + live feed to Part 3 (UI)."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(events.router)
app.include_router(person.router)
app.include_router(employees.router)
app.include_router(live.router)
app.include_router(search.router)
app.include_router(identities.router)
app.include_router(logs.router)
app.include_router(admin.router)
app.include_router(attendance.router)


@app.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    tags=["meta"],
    summary="Liveness probe — checks DB + Redis + Qdrant connectivity",
)
async def health() -> HealthResponse:
    db_status = "ok"
    redis_status = "ok"
    qdrant_status = "ok"

    try:
        await init_db()
    except Exception as e:  # noqa: BLE001
        db_status = f"error: {e}"

    try:
        client = await presence_cache.get_client()
        await client.ping()
    except Exception as e:  # noqa: BLE001
        redis_status = f"error: {e}"

    try:
        await vector_store.ping()
    except Exception as e:  # noqa: BLE001
        qdrant_status = f"error: {e}"

    return HealthResponse(
        database=db_status,
        redis=redis_status,
        qdrant=qdrant_status,
        version="2.0.0",
    )


@app.get("/", tags=["meta"], summary="Service info")
async def root() -> dict:
    return {
        "service": "Surveillance Brain",
        "part": "Project RUNG01 — Part 2 (Database + Tracking + Identity)",
        "version": "2.0.0",
        "docs": "/docs",
        "health": "/health",
    }
