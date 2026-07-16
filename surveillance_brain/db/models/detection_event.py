from __future__ import annotations

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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .enums import Base, Classification, IdentityType, MatchedBy, SessionStatus

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
