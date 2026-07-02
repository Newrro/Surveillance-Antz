"""
db/vector_store.py
==================
Qdrant vector-store wrapper — the home of all face + body embeddings.

Two collections:
    faces   — face embeddings   (primary identity signal)
    bodies  — body ReID embeddings (cross-camera fallback)

Both use cosine distance and dimension = config.EMBEDDING_DIMENSIONS.

Point model:
    Each stored vector is one Qdrant point with a UUID point-id and a
    payload of {"identity_id": <int>, "source": <str>}.  We key on the
    payload (not the point-id) because a single identity can accumulate
    several embeddings over time (different angles, lighting, aging).
    Search results carry the payload, so the caller reads
    `payload["identity_id"]` to know who matched.

Why a separate vector DB (not pgvector):
    Per the Part 2 architecture decision, embeddings scale independently
    of the relational records and get their own service (Qdrant).  The
    relational surrogate-key `identity_id` is the join key between the two.

Everything here is async (AsyncQdrantClient) so it composes with the
FastAPI event loop.
"""

from __future__ import annotations

import logging
import uuid
from typing import List, Optional, Sequence, Tuple

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

import config

logger = logging.getLogger(__name__)

_client: Optional[AsyncQdrantClient] = None


# ---------------------------------------------------------------------------
# Connection — lazy singleton so import-time never blocks.
# ---------------------------------------------------------------------------
def get_client() -> AsyncQdrantClient:
    """Return the shared async Qdrant client (singleton)."""
    global _client
    if _client is None:
        _client = AsyncQdrantClient(
            url=config.QDRANT_URL,
            api_key=config.QDRANT_API_KEY,
            prefer_grpc=False,
            timeout=5.0,
        )
    return _client


async def close_client() -> None:
    """Close the Qdrant client — called on app shutdown."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None


# ---------------------------------------------------------------------------
# Collection bootstrap
# ---------------------------------------------------------------------------
async def ensure_collections() -> None:
    """
    Create the face + body collections if they don't already exist.
    Idempotent — safe to call on every startup.
    """
    client = get_client()
    existing = {c.name for c in (await client.get_collections()).collections}

    for name in (config.QDRANT_FACE_COLLECTION, config.QDRANT_BODY_COLLECTION):
        if name in existing:
            logger.debug("Qdrant collection %r already exists", name)
            continue
        await client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(
                size=config.EMBEDDING_DIMENSIONS,
                distance=Distance.COSINE,
            ),
        )
        logger.info("Created Qdrant collection %r (dim=%d, cosine)", name, config.EMBEDDING_DIMENSIONS)


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------
async def upsert_embedding(
    collection: str,
    identity_id: int,
    vector: Sequence[float],
    source: Optional[str] = None,
) -> str:
    """
    Insert a new embedding point into a collection.  Returns the point id.

    A fresh UUID point-id is generated every call, so multiple embeddings
    can coexist for one identity (we never overwrite).
    """
    if len(vector) != config.EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"Embedding length {len(vector)} != EMBEDDING_DIMENSIONS "
            f"{config.EMBEDDING_DIMENSIONS}"
        )
    client = get_client()
    point_id = str(uuid.uuid4())
    await client.upsert(
        collection_name=collection,
        points=[
            PointStruct(
                id=point_id,
                vector=list(vector),
                payload={"identity_id": int(identity_id), "source": source},
            )
        ],
    )
    return point_id


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
async def search(
    collection: str,
    vector: Sequence[float],
    limit: int = 5,
) -> List[Tuple[int, float]]:
    """
    Nearest-neighbour search in a collection.

    Returns a list of (identity_id, similarity) tuples, best first.
    Qdrant's cosine `score` is already the cosine similarity (higher =
    closer), so no `1 - distance` conversion is needed.  The caller
    applies the similarity threshold.
    """
    if len(vector) != config.EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"Embedding length {len(vector)} != EMBEDDING_DIMENSIONS "
            f"{config.EMBEDDING_DIMENSIONS}"
        )
    client = get_client()
    hits = await client.search(
        collection_name=collection,
        query_vector=list(vector),
        limit=limit,
        with_payload=True,
    )
    out: List[Tuple[int, float]] = []
    for h in hits:
        payload = h.payload or {}
        identity_id = payload.get("identity_id")
        if identity_id is not None:
            out.append((int(identity_id), float(h.score)))
    return out


# ---------------------------------------------------------------------------
# Delete (V2 PII "Right to be Forgotten")
# ---------------------------------------------------------------------------
async def delete_for_identity(identity_id: int) -> None:
    """Delete ALL points (face + body) belonging to an identity."""
    client = get_client()
    flt = Filter(
        must=[FieldCondition(key="identity_id", match=MatchValue(value=int(identity_id)))]
    )
    for name in (config.QDRANT_FACE_COLLECTION, config.QDRANT_BODY_COLLECTION):
        await client.delete(collection_name=name, points_selector=flt)


async def ping() -> bool:
    """Liveness check used by /health."""
    client = get_client()
    await client.get_collections()
    return True
