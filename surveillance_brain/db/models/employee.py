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
