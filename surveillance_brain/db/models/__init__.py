"""
db/models package — SQLAlchemy 2.0 ORM models for the Surveillance Brain.

Split from the original single db/models.py into one file per table (plus
enums) for readability. Every public name is re-exported here, so existing
imports such as `from db.models import DetectionEvent` and `db.models.Base`
continue to work unchanged. Importing this package registers every model on
the shared Base.metadata (needed for Alembic autogenerate and string-based
relationship resolution).

Embeddings do NOT live here — they live in Qdrant (see db/vector_store.py).
The `identities.id` surrogate key is permanent across promote/demote.
"""

from .enums import (
    Base,
    Classification,
    IdentityType,
    MatchedBy,
    SessionStatus,
)
from .identity import Identity
from .employee import Employee
from .visitor import Visitor
from .camera import Camera
from .session import PresenceSession
from .detection_event import DetectionEvent
from .unknown_case import UnknownCase

__all__ = [
    "Base",
    "IdentityType",
    "Classification",
    "MatchedBy",
    "SessionStatus",
    "Identity",
    "Employee",
    "Visitor",
    "Camera",
    "PresenceSession",
    "DetectionEvent",
    "UnknownCase",
]
