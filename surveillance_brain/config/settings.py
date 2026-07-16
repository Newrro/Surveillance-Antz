"""config/settings.py — the composed Settings object.

The fields are grouped into small per-section mixins (postgres, qdrant, redis,
matching, storage, session, admin) and combined here. Behaviour is identical to
the old single Settings class: pydantic-settings reads every inherited field
from the environment / .env exactly as before.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from .postgres import _PostgresSettings
from .qdrant import _QdrantSettings
from .redis import _RedisSettings
from .matching import _MatchingSettings
from .storage import _StorageSettings
from .session import _SessionCronSettings
from .admin import _AdminRuntimeSettings


class Settings(
    _PostgresSettings,
    _QdrantSettings,
    _RedisSettings,
    _MatchingSettings,
    _StorageSettings,
    _SessionCronSettings,
    _AdminRuntimeSettings,
    BaseSettings,
):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

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
