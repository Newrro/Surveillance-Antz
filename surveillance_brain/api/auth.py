"""
api/auth.py
===========
HTTP Basic Authentication for admin endpoints.

Per the spec's "Environment & Setup Specification" doc:
    "For Version 1, admin endpoints (like promoting/demoting identities)
     will be protected using standard Basic Authentication."

Credentials are loaded from the ADMIN_USERNAME / ADMIN_PASSWORD env vars
(configurable in `.env`).  The default values `admin:changeme` MUST be
changed before any non-local deployment.

Usage in a router:
    from api.auth import require_admin
    @router.post("/promote", dependencies=[Depends(require_admin)])
    async def promote(...): ...
"""

from __future__ import annotations

import logging
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import config

logger = logging.getLogger(__name__)

_security = HTTPBasic(realm="Surveillance Admin")


def require_admin(credentials: HTTPBasicCredentials = Depends(_security)) -> str:
    """
    FastAPI dependency that enforces HTTP Basic Auth against the configured
    ADMIN_USERNAME / ADMIN_PASSWORD.

    Uses `secrets.compare_digest` to avoid timing attacks.

    Returns the username on success (so handlers can log who performed
    the action if desired).  Raises 401 on failure.
    """
    is_user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        config.ADMIN_USERNAME.encode("utf-8"),
    )
    is_pass_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        config.ADMIN_PASSWORD.encode("utf-8"),
    )

    if not (is_user_ok and is_pass_ok):
        logger.warning(
            "Failed admin auth attempt: username=%r remote=???",
            credentials.username,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username
