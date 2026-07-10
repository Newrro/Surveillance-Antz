"""
repositories/embedding_repo.py
==============================
Embedding persistence + similarity search — now backed by Qdrant.

This is a thin repository over db/vector_store.py.  It exists so the
service layer keeps a stable "repository" seam and never imports the
Qdrant client directly.  Two modalities:

    face  → config.QDRANT_FACE_COLLECTION
    body  → config.QDRANT_BODY_COLLECTION

Similarity is Qdrant's cosine score (higher = closer).  The caller
(feature_matcher) applies the per-modality threshold.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple

import config
from db import vector_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Face
# ---------------------------------------------------------------------------
async def insert_face(identity_id: int, embedding: Sequence[float], source: Optional[str] = None,
                      quality: Optional[float] = None) -> str:
    pid = await vector_store.upsert_embedding(
        config.QDRANT_FACE_COLLECTION, identity_id, embedding, source, quality
    )
    await _cap_views(config.QDRANT_FACE_COLLECTION, identity_id)
    return pid


async def _cap_views(collection: str, identity_id: int) -> None:
    """Best-effort gallery view-cap (Phase 3d) — never break ingestion over it."""
    try:
        await vector_store.enforce_view_cap(collection, identity_id, config.GALLERY_MAX_VIEWS)
    except Exception as e:  # noqa: BLE001
        logger.warning("view-cap failed for identity %d: %s", identity_id, e)


async def search_face(embedding: Sequence[float], limit: int = 5) -> List[Tuple[int, float]]:
    return await vector_store.search(config.QDRANT_FACE_COLLECTION, embedding, limit=limit)


# ---------------------------------------------------------------------------
# Body (ReID)
# ---------------------------------------------------------------------------
async def insert_body(identity_id: int, embedding: Sequence[float], source: Optional[str] = None,
                      quality: Optional[float] = None) -> str:
    pid = await vector_store.upsert_embedding(
        config.QDRANT_BODY_COLLECTION, identity_id, embedding, source, quality
    )
    await _cap_views(config.QDRANT_BODY_COLLECTION, identity_id)
    return pid


async def search_body(embedding: Sequence[float], limit: int = 5) -> List[Tuple[int, float]]:
    return await vector_store.search(config.QDRANT_BODY_COLLECTION, embedding, limit=limit)


# ---------------------------------------------------------------------------
# Combined helpers
# ---------------------------------------------------------------------------
async def store_embeddings(
    identity_id: int,
    face_embedding: Optional[Sequence[float]] = None,
    body_embedding: Optional[Sequence[float]] = None,
    source: Optional[str] = None,
) -> None:
    """Persist whichever embeddings were provided for an identity."""
    if face_embedding is not None:
        await insert_face(identity_id, face_embedding, source)
    if body_embedding is not None:
        await insert_body(identity_id, body_embedding, source)


async def delete_embeddings_for_identity(identity_id: int) -> None:
    """Remove all embeddings (face + body) for an identity — V2 PII deletion."""
    await vector_store.delete_for_identity(identity_id)


async def fetch_face_vectors(identity_id: int) -> List[List[float]]:
    """All face vectors stored for an identity (for centroid consolidation)."""
    return await vector_store.fetch_vectors_for_identity(config.QDRANT_FACE_COLLECTION, identity_id)
