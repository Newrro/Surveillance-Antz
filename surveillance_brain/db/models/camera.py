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
