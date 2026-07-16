"""db/models/enums.py — declarative Base and the persisted enum types."""

from __future__ import annotations

import enum

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 declarative base shared by all models."""


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class IdentityType(str, enum.Enum):
    """Discriminator for which 1:1 extension table applies."""
    VISITOR = "visitor"
    EMPLOYEE = "employee"


class Classification(str, enum.Enum):
    """Resolution outcome / event label returned by identity_resolver."""
    EMPLOYEE = "employee"
    VISITOR = "visitor"
    UNKNOWN = "unknown"


class MatchedBy(str, enum.Enum):
    """Which modality produced the identity match for a detection."""
    FACE = "face"      # matched on the face embedding (primary)
    BODY = "body"      # matched on the body ReID embedding (fallback)
    NONE = "none"      # unknown / brand-new / no vector search


class SessionStatus(str, enum.Enum):
    """Lifecycle of a presence_sessions row."""
    INSIDE = "inside"
