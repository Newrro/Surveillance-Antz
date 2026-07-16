from __future__ import annotations

from pydantic import BaseModel


class _PostgresSettings(BaseModel):
    # ---- PostgreSQL (async) ---------------------------------------------
    POSTGRES_USER: str = "surveillance"
    POSTGRES_PASSWORD: str = "surveillance"
    POSTGRES_DB: str = "surveillance"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
