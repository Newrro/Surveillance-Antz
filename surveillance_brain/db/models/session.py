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
