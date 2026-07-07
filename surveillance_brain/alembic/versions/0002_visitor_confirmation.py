"""visitor confirmation state: has_face, has_body, confirmed_at

A person starts as an UNKNOWN (enrolled visitor row, unconfirmed). They are
promoted to a real VISITOR only once we have BOTH a clear face and a body
embedding on file (confirmed_at set). This is what makes a Visitor a person we
can re-identify with confidence — by face — on any later day.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_visitor_confirmation"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("visitors", sa.Column("has_face", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("visitors", sa.Column("has_body", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("visitors", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_visitors_confirmed_at", "visitors", ["confirmed_at"])


def downgrade() -> None:
    op.drop_index("ix_visitors_confirmed_at", table_name="visitors")
    op.drop_column("visitors", "confirmed_at")
    op.drop_column("visitors", "has_body")
    op.drop_column("visitors", "has_face")
