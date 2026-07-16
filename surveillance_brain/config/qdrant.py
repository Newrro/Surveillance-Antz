from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class _QdrantSettings(BaseModel):
    # ---- Qdrant (vector DB) ---------------------------------------------
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_API_KEY: Optional[str] = None
    # Docker-free / embedded mode: when set, the client runs Qdrant in-process
    # against this on-disk path instead of connecting to a server (host/port are
    # then ignored). Only one process may hold the path at a time — seed runs to
    # completion and closes before the API opens it. Leave empty for a server.
    QDRANT_LOCAL_PATH: Optional[str] = None
    # Two collections — one per embedding modality.
    QDRANT_FACE_COLLECTION: str = "faces"
    QDRANT_BODY_COLLECTION: str = "bodies"
