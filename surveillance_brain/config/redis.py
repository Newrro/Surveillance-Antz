from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class _RedisSettings(BaseModel):
    # ---- Redis ----------------------------------------------------------
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: Optional[str] = None
