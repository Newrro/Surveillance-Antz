"""
config package — central configuration for the Surveillance Brain (Part 2).

Split from the original single config.py into per-section field mixins (see
config/settings.py) for readability. The public surface is UNCHANGED: import
`config` and read `config.DATABASE_DSN`, `config.EMBEDDING_DIMENSIONS`,
`config.get_settings()`, `config.Settings`, etc. exactly as before.

ALL deployment-specific values are read from environment variables / .env so
the same image is promoted across dev / staging / prod without code changes.
Embeddings live in Qdrant, not Postgres; `detection_conf` is 0.0-1.0.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Final, Optional

from .settings import Settings

_settings = Settings()

# ---- Module-level constants (import directly anywhere) --------------------
DATABASE_DSN: Final[str] = _settings.DATABASE_DSN
DATABASE_DSN_SYNC: Final[str] = _settings.DATABASE_DSN_SYNC
REDIS_URL: Final[str] = _settings.REDIS_URL
QDRANT_URL: Final[str] = _settings.QDRANT_URL
QDRANT_LOCAL_PATH: Final[Optional[str]] = _settings.QDRANT_LOCAL_PATH
QDRANT_API_KEY: Final[Optional[str]] = _settings.QDRANT_API_KEY
QDRANT_FACE_COLLECTION: Final[str] = _settings.QDRANT_FACE_COLLECTION
QDRANT_BODY_COLLECTION: Final[str] = _settings.QDRANT_BODY_COLLECTION

DETECTION_CONF_THRESHOLD: Final[float] = _settings.DETECTION_CONF_THRESHOLD
FACE_SIMILARITY_THRESHOLD: Final[float] = _settings.FACE_SIMILARITY_THRESHOLD
FACE_ASSIGN_THRESHOLD: Final[float] = _settings.FACE_ASSIGN_THRESHOLD
FACE_MARGIN: Final[float] = _settings.FACE_MARGIN
FACE_NEW_THRESHOLD: Final[float] = _settings.FACE_NEW_THRESHOLD
FACE_MIN_QUALITY: Final[float] = _settings.FACE_MIN_QUALITY
IDENTITY_SELF_CALIBRATE: Final[bool] = _settings.IDENTITY_SELF_CALIBRATE
CALIB_WARMUP: Final[int] = _settings.CALIB_WARMUP
CALIB_MATCH_K: Final[float] = _settings.CALIB_MATCH_K
CALIB_MERGE_K: Final[float] = _settings.CALIB_MERGE_K
FACE_MATCH_THRESHOLD_DEFAULT: Final[float] = _settings.FACE_MATCH_THRESHOLD_DEFAULT
FACE_MATCH_MARGIN: Final[float] = _settings.FACE_MATCH_MARGIN
FACE_MATCH_MIN: Final[float] = _settings.FACE_MATCH_MIN
FACE_MATCH_MAX: Final[float] = _settings.FACE_MATCH_MAX
FACE_MERGE_THRESHOLD_DEFAULT: Final[float] = _settings.FACE_MERGE_THRESHOLD_DEFAULT
FACE_MERGE_MIN: Final[float] = _settings.FACE_MERGE_MIN
FACE_MERGE_MAX: Final[float] = _settings.FACE_MERGE_MAX
BODY_SIMILARITY_THRESHOLD: Final[float] = _settings.BODY_SIMILARITY_THRESHOLD
BODY_MERGE_THRESHOLD: Final[float] = _settings.BODY_MERGE_THRESHOLD
BODY_MERGE_WINDOW_SECONDS: Final[int] = _settings.BODY_MERGE_WINDOW_SECONDS
TRACK_STICKY_TTL_SECONDS: Final[int] = _settings.TRACK_STICKY_TTL_SECONDS
GALLERY_MAX_VIEWS: Final[int] = _settings.GALLERY_MAX_VIEWS
CONSOLIDATE_FACE_THRESHOLD: Final[float] = _settings.CONSOLIDATE_FACE_THRESHOLD
CONSOLIDATE_ENABLE: Final[bool] = _settings.CONSOLIDATE_ENABLE
CONSOLIDATE_APPLY: Final[bool] = _settings.CONSOLIDATE_APPLY
CONSOLIDATE_EVERY_MINUTES: Final[int] = _settings.CONSOLIDATE_EVERY_MINUTES
CONSOLIDATE_AUTO_FACE_THRESHOLD: Final[float] = _settings.CONSOLIDATE_AUTO_FACE_THRESHOLD
LEARN_SIMILARITY_CEILING: Final[float] = _settings.LEARN_SIMILARITY_CEILING
LEARN_SIMILARITY_FLOOR: Final[float] = _settings.LEARN_SIMILARITY_FLOOR
EMBEDDING_DIMENSIONS: Final[int] = _settings.EMBEDDING_DIMENSIONS

STORAGE_ROOT: Final[str] = _settings.STORAGE_ROOT
LOG_DIR: Final[str] = _settings.LOG_DIR
DATASHEET_DIR: Final[str] = _settings.DATASHEET_DIR

SESSION_TIMEOUT_SECONDS: Final[int] = _settings.SESSION_TIMEOUT_SECONDS
DUPLICATE_WINDOW_SECONDS: Final[int] = _settings.DUPLICATE_WINDOW_SECONDS
MIDNIGHT_FLUSH_CRON: Final[str] = _settings.MIDNIGHT_FLUSH_CRON
ARCHIVE_CRON: Final[str] = _settings.ARCHIVE_CRON
RETENTION_CRON: Final[str] = _settings.RETENTION_CRON
RETENTION_DAYS: Final[int] = _settings.RETENTION_DAYS

ADMIN_USERNAME: Final[str] = _settings.ADMIN_USERNAME
ADMIN_PASSWORD: Final[str] = _settings.ADMIN_PASSWORD

LOG_LEVEL: Final[str] = _settings.LOG_LEVEL
ENVIRONMENT: Final[str] = _settings.ENVIRONMENT


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """FastAPI dependency — returns the cached Settings singleton."""
    return _settings


__all__ = ["Settings", "get_settings"]
