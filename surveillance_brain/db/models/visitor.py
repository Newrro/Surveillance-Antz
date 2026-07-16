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
