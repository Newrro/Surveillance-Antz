"""
db/models.py
============
SQLAlchemy 2.0 ORM models for the Surveillance Brain (Part 2).

Postgres tables (permanent, structured records + logs):
    identities          — surrogate-key core identity (type + display_label)
    employees           — 1:1 extension for employee-type identities
    visitors            — 1:1 extension for visitor-type identities
    cameras             — facility cameras, with is_exit_camera flag
    presence_sessions   — facility entry/exit sessions (one row per visit)
    detection_events    — raw event ledger (one row per accepted detection)

Embeddings do NOT live here — they live in Qdrant (see db/vector_store.py).
A Qdrant point is keyed by identity_id, so the surrogate-key lifecycle
(promote/demote) never invalidates the vectors.

Identity Lifecycle Rule (CRITICAL):
    `identities.id` is a permanent surrogate key.  When a visitor is
    promoted to an employee, the SAME id row is mutated — the `visitors`
    row is deleted, an `employees` row is inserted, and `display_label`
    flips from `VIS-2026-0001` to `EMP-2026-0005`.  All historical
    detection_events, presence_sessions, and Qdrant vectors stay linked
    to the same id, preserving an unbroken movement log.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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
    EXITED = "exited"


# ---------------------------------------------------------------------------
# identities — the surrogate-key anchor
# ---------------------------------------------------------------------------
class Identity(Base):
    __tablename__ = "identities"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    identity_type: Mapped[IdentityType] = mapped_column(
        SAEnum(IdentityType, name="identity_type", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        index=True,
    )

    # Human-readable, year-scoped, sequence-prefixed label.
    # Visitors → VIS-YYYY-NNNN | Employees → EMP-YYYY-NNNN
    display_label: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)

    # V2 "Right to be Forgotten" helper.
    is_anonymized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    employee: Mapped[Optional["Employee"]] = relationship(
        back_populates="identity", uselist=False, cascade="all, delete-orphan"
    )
    visitor: Mapped[Optional["Visitor"]] = relationship(
        back_populates="identity", uselist=False, cascade="all, delete-orphan"
    )
    sessions: Mapped[List["PresenceSession"]] = relationship(
        back_populates="identity", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Identity id={self.id} type={self.identity_type} label={self.display_label}>"


# ---------------------------------------------------------------------------
# employees — 1:1 with identities (only when identity_type='employee')
# ---------------------------------------------------------------------------
class Employee(Base):
    __tablename__ = "employees"

    identity_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("identities.id", ondelete="CASCADE"), primary_key=True
    )
    employee_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    department: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # Idempotency key for bulk roster import (XLSX/CSV/ZIP): re-importing the
    # same external_id UPDATES the employee instead of duplicating them.
    external_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    hired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    identity: Mapped["Identity"] = relationship(back_populates="employee")

    __table_args__ = (
        UniqueConstraint("year", "employee_seq", name="uq_employees_year_seq"),
        UniqueConstraint("external_id", name="uq_employees_external_id"),
        Index("ix_employees_name", "name"),
    )

    def __repr__(self) -> str:
        return f"<Employee id={self.identity_id} name={self.name} dept={self.department}>"


# ---------------------------------------------------------------------------
# visitors — 1:1 with identities (only when identity_type='visitor')
# ---------------------------------------------------------------------------
class Visitor(Base):
    __tablename__ = "visitors"

    identity_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("identities.id", ondelete="CASCADE"), primary_key=True
    )
    visitor_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)

    name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Confirmation state. A person is UNKNOWN (unconfirmed) until we have BOTH a
    # clear face and a body embedding on file; then confirmed_at is set and they
    # become a real VISITOR — re-identifiable by face on any later day.
    has_face: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_body: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    identity: Mapped["Identity"] = relationship(back_populates="visitor")

    __table_args__ = (
        UniqueConstraint("year", "visitor_seq", name="uq_visitors_year_seq"),
    )

    def __repr__(self) -> str:
        return f"<Visitor id={self.identity_id} seq={self.visitor_seq} year={self.year}>"


# ---------------------------------------------------------------------------
# cameras
# ---------------------------------------------------------------------------
class Camera(Base):
    __tablename__ = "cameras"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    camera_uid: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    zone_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Exit cameras trigger session closure logic in session_tracker.
    is_exit_camera: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    stream_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        tag = "EXIT" if self.is_exit_camera else "ZONE"
        return f"<Camera {self.camera_uid} [{tag}] zone={self.zone_id}>"


# ---------------------------------------------------------------------------
# presence_sessions — facility entry/exit lifecycle
# ---------------------------------------------------------------------------
class PresenceSession(Base):
    __tablename__ = "presence_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    identity_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("identities.id", ondelete="CASCADE"), nullable=False, index=True
    )

    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_camera_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True
    )
    exit_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_camera_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[SessionStatus] = mapped_column(
        SAEnum(SessionStatus, name="session_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=SessionStatus.INSIDE,
        index=True,
    )

    identity: Mapped["Identity"] = relationship(back_populates="sessions")

    __table_args__ = (
        Index("ix_sessions_identity_status", "identity_id", "status"),
        Index("ix_sessions_entry_at", "entry_at"),
    )

    def __repr__(self) -> str:
        return f"<PresenceSession id={self.id} identity={self.identity_id} status={self.status}>"


# ---------------------------------------------------------------------------
# detection_events — raw ledger of every accepted detection (Part 1 → Part 2)
# ---------------------------------------------------------------------------
class DetectionEvent(Base):
    __tablename__ = "detection_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Stable per-detection id from Part 1 (one per accepted person crop).
    detection_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    # NULL when classification='unknown' — preserves the statistical row
    # without linking to a permanent identity.
    identity_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("identities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    camera_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True, index=True
    )

    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    # Part 1 detection confidence, 0.0–1.0.
    detection_conf: Mapped[float] = mapped_column(Float, nullable=False)

    classification: Mapped[Classification] = mapped_column(
        SAEnum(Classification, name="classification", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        index=True,
    )

    # How the identity was matched (face / body / none) + the cosine score.
    matched_by: Mapped[MatchedBy] = mapped_column(
        SAEnum(MatchedBy, name="matched_by", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=MatchedBy.NONE,
    )
    similarity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ---- Sighting evidence group (2026-07 rework) -------------------------
    # ONE sighting = ONE immutable evidence set. Every path below was written
    # ONCE by the pipeline from the SAME captured moment and is never edited,
    # cropped or replaced. Consumers must use these EXPLICIT columns — never
    # derive a companion file from another file's name.
    track_uuid: Mapped[Optional[str]] = mapped_column(String(96), nullable=True, index=True)
    bbox_x1: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bbox_y1: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bbox_x2: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bbox_y2: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    frame_w: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    frame_h: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    face_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    body_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    full_frame_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)            # ORIGINAL, untouched
    full_frame_annotated_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # separate copy, optional

    # Media references — the files themselves live in shared storage / S3;
    # the Brain only stores the path/URL string. snapshot_path is the legacy
    # single-photo column (kept so pre-rework rows stay readable); new rows
    # set it to body_path for back-compat.
    snapshot_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    clip_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ---- Soft delete (report "delete" hides ONE sighting, never history) --
    hidden_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    hidden_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hidden_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return f"<DetectionEvent id={self.id} cls={self.classification} cam={self.camera_id}>"


# ---------------------------------------------------------------------------
# unknown_cases — 1:1 with identities (only when identity_type='unknown')
# ---------------------------------------------------------------------------
class UnknownCase(Base):
    """A persistent grouped record for one UNIDENTIFIED person. Created on the
    first stable faceless track; later tracks re-attach by track continuity or
    the constrained same-camera body link. When a face finally resolves, the
    whole case (all sightings) is folded onto the Visitor/Employee identity."""
    __tablename__ = "unknown_cases"

    identity_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("identities.id", ondelete="CASCADE"), primary_key=True
    )
    unknown_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)

    # The tracker track that opened the case (run-unique, camera-scoped).
    track_uuid: Mapped[Optional[str]] = mapped_column(String(96), nullable=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("year", "unknown_seq", name="uq_unknown_cases_year_seq"),
        UniqueConstraint("track_uuid", name="uq_unknown_cases_track_uuid"),
    )

    def __repr__(self) -> str:
        return f"<UnknownCase id={self.identity_id} seq={self.unknown_seq} year={self.year}>"
