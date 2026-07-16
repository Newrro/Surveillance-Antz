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
