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
