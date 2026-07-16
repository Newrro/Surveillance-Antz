from __future__ import annotations

from pydantic import BaseModel


class _AdminRuntimeSettings(BaseModel):
    # ---- Admin Basic-Auth -----------------------------------------------
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "changeme"

    # ---- Runtime --------------------------------------------------------
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    ENVIRONMENT: str = "dev"  # dev | prod
