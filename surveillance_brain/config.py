"""
config.py
=========
Central configuration for the Surveillance Brain (Project RUNG01 — Part 2).

ALL deployment-specific values are read from environment variables so the
same image can be promoted across dev / staging / prod without code changes.
Sensible localhost defaults are provided for `docker compose up`.

Sections:
  1. PostgreSQL 16          — structured records + logs (permanent)
  2. Qdrant                 — face + body embedding vector search
  3. Redis                  — live presence cache (TTL) + duplicate guard
  4. Matching thresholds    — detection confidence + cosine similarity
  5. Local storage          — JSON/JSONL archive of logs + datasheet
  6. Session + cron         — presence TTL, midnight flush, archive
  7. Admin Basic-Auth
  8. Runtime flags

Architecture note (vs. the old single-DB Brain):
  * Embeddings no longer live in Postgres/pgvector — they live in Qdrant.
  * Part 1 (Perception) now sends TWO embeddings per person: a face
    embedding (primary identity signal) and a body ReID embedding
    (cross-camera fallback), plus a `detection_id`, `snapshot_path`
    and `clip_path`.
  * `detection_conf` is a 0.0–1.0 probability (NOT a 0–100 percentage).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Final, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- PostgreSQL (async) ---------------------------------------------
    POSTGRES_USER: str = "surveillance"
    POSTGRES_PASSWORD: str = "surveillance"
    POSTGRES_DB: str = "surveillance"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432

    # ---- Qdrant (vector DB) ---------------------------------------------
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_API_KEY: Optional[str] = None
    # Two collections — one per embedding modality.
    QDRANT_FACE_COLLECTION: str = "faces"
    QDRANT_BODY_COLLECTION: str = "bodies"

    # ---- Redis ----------------------------------------------------------
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: Optional[str] = None

    # ---- Matching thresholds --------------------------------------------
    # Part 1 sends `detection_conf` in the range 0.0–1.0.  Below this the
    # detection is classified UNKNOWN and NO vector search is performed.
    # The architecture note says ">80%", i.e. 0.80.
    DETECTION_CONF_THRESHOLD: float = 0.80

    # Cosine similarity floor (1 - cosine_distance) for a FACE match.
    FACE_SIMILARITY_THRESHOLD: float = 0.65

    # Cosine similarity floor for a BODY ReID match — used only as a
    # fallback when the face embedding is absent or below threshold.
    # Body ReID is noisier than face, so this is intentionally a touch
    # looser but still conservative.
    BODY_SIMILARITY_THRESHOLD: float = 0.60

    # Must match Part 1's model output dimension (face + body share it here).
    EMBEDDING_DIMENSIONS: int = 512

    # ---- Local storage (JSON/JSONL archive) -----------------------------
    # Postgres is the permanent source of truth; these files are the
    # human-readable export/archive each feature writes for offline use.
    STORAGE_ROOT: str = "storage"          # snapshots/clips referenced by Part 1 live here too
    LOG_DIR: str = "storage/logs"          # append-only events.jsonl
    DATASHEET_DIR: str = "storage/datasheet"  # per-person datasheet snapshots (JSON)

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

    # ---- Admin Basic-Auth -----------------------------------------------
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "changeme"

    # ---- Runtime --------------------------------------------------------
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    ENVIRONMENT: str = "dev"  # dev | prod

    # ---- Derived connection strings -------------------------------------
    @property
    def DATABASE_DSN(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def DATABASE_DSN_SYNC(self) -> str:
        """Sync DSN used by Alembic only."""
        return (
            f"postgresql+psycopg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def REDIS_URL(self) -> str:
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def QDRANT_URL(self) -> str:
        return f"http://{self.QDRANT_HOST}:{self.QDRANT_PORT}"


_settings = Settings()

# ---- Module-level constants (import directly anywhere) --------------------
DATABASE_DSN: Final[str] = _settings.DATABASE_DSN
DATABASE_DSN_SYNC: Final[str] = _settings.DATABASE_DSN_SYNC
REDIS_URL: Final[str] = _settings.REDIS_URL
QDRANT_URL: Final[str] = _settings.QDRANT_URL
QDRANT_API_KEY: Final[Optional[str]] = _settings.QDRANT_API_KEY
QDRANT_FACE_COLLECTION: Final[str] = _settings.QDRANT_FACE_COLLECTION
QDRANT_BODY_COLLECTION: Final[str] = _settings.QDRANT_BODY_COLLECTION

DETECTION_CONF_THRESHOLD: Final[float] = _settings.DETECTION_CONF_THRESHOLD
FACE_SIMILARITY_THRESHOLD: Final[float] = _settings.FACE_SIMILARITY_THRESHOLD
BODY_SIMILARITY_THRESHOLD: Final[float] = _settings.BODY_SIMILARITY_THRESHOLD
EMBEDDING_DIMENSIONS: Final[int] = _settings.EMBEDDING_DIMENSIONS

STORAGE_ROOT: Final[str] = _settings.STORAGE_ROOT
LOG_DIR: Final[str] = _settings.LOG_DIR
DATASHEET_DIR: Final[str] = _settings.DATASHEET_DIR

SESSION_TIMEOUT_SECONDS: Final[int] = _settings.SESSION_TIMEOUT_SECONDS
DUPLICATE_WINDOW_SECONDS: Final[int] = _settings.DUPLICATE_WINDOW_SECONDS
MIDNIGHT_FLUSH_CRON: Final[str] = _settings.MIDNIGHT_FLUSH_CRON
ARCHIVE_CRON: Final[str] = _settings.ARCHIVE_CRON

ADMIN_USERNAME: Final[str] = _settings.ADMIN_USERNAME
ADMIN_PASSWORD: Final[str] = _settings.ADMIN_PASSWORD

LOG_LEVEL: Final[str] = _settings.LOG_LEVEL
ENVIRONMENT: Final[str] = _settings.ENVIRONMENT


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """FastAPI dependency — returns the cached Settings singleton."""
    return _settings
