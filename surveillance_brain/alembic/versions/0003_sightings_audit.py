"""Sightings, Unknown cases, audit log, employee external ids.

1. detection_events grows sighting fields: track_uuid (run-unique track id),
   pixel bbox + frame dimensions, and soft-delete columns (hidden_at/reason/by)
   so a single bad sighting can be hidden without erasing unrelated history.
2. identity_type enum gains 'unknown' + new unknown_cases extension table —
   a persistent case per unidentified human track, so faceless people appear
   in logs as mergeable Unknown cases instead of id-less rows.
3. audit_log — merge / hide / reassign / import / erase actions with context.
4. employees.external_id — the idempotency key for XLSX/CSV/ZIP bulk import.

All changes are additive; downgrade drops them (the enum value stays — Postgres
cannot remove enum values without a rebuild, and an unused value is harmless).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_sightings_audit"
down_revision: Union[str, None] = "0002_visitor_confirmation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- 1. identity_type enum += 'unknown' (PG12+ allows this in a txn) ------
    op.execute("ALTER TYPE identity_type ADD VALUE IF NOT EXISTS 'unknown'")

    # -- 2. detection_events sighting fields ----------------------------------
    op.add_column("detection_events", sa.Column("track_uuid", sa.String(96), nullable=True))
    op.create_index("ix_detection_events_track_uuid", "detection_events", ["track_uuid"])
    op.add_column("detection_events", sa.Column("bbox_x1", sa.Float(), nullable=True))
    op.add_column("detection_events", sa.Column("bbox_y1", sa.Float(), nullable=True))
    op.add_column("detection_events", sa.Column("bbox_x2", sa.Float(), nullable=True))
    op.add_column("detection_events", sa.Column("bbox_y2", sa.Float(), nullable=True))
    op.add_column("detection_events", sa.Column("frame_w", sa.Integer(), nullable=True))
    op.add_column("detection_events", sa.Column("frame_h", sa.Integer(), nullable=True))
    op.add_column("detection_events", sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_detection_events_hidden_at", "detection_events", ["hidden_at"])
    op.add_column("detection_events", sa.Column("hidden_reason", sa.Text(), nullable=True))
    op.add_column("detection_events", sa.Column("hidden_by", sa.String(64), nullable=True))

    # -- 3. unknown_cases ------------------------------------------------------
    op.create_table(
        "unknown_cases",
        sa.Column("identity_id", sa.BigInteger(),
                  sa.ForeignKey("identities.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("unknown_seq", sa.Integer(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("track_uuid", sa.String(96), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("year", "unknown_seq", name="uq_unknown_cases_year_seq"),
        sa.UniqueConstraint("track_uuid", name="uq_unknown_cases_track_uuid"),
    )
    op.create_index("ix_unknown_cases_track_uuid", "unknown_cases", ["track_uuid"])

    # -- 4. audit_log -----------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False, server_default="system"),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.String(96), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_subject_id", "audit_log", ["subject_id"])

    # -- 5. employees.external_id ----------------------------------------------
    op.add_column("employees", sa.Column("external_id", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_employees_external_id", "employees", ["external_id"])


def downgrade() -> None:
    op.drop_constraint("uq_employees_external_id", "employees", type_="unique")
    op.drop_column("employees", "external_id")
    op.drop_index("ix_audit_log_subject_id", table_name="audit_log")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_at", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_unknown_cases_track_uuid", table_name="unknown_cases")
    op.drop_table("unknown_cases")
    op.drop_column("detection_events", "hidden_by")
    op.drop_column("detection_events", "hidden_reason")
    op.drop_index("ix_detection_events_hidden_at", table_name="detection_events")
    op.drop_column("detection_events", "hidden_at")
    op.drop_column("detection_events", "frame_h")
    op.drop_column("detection_events", "frame_w")
    op.drop_column("detection_events", "bbox_y2")
    op.drop_column("detection_events", "bbox_x2")
    op.drop_column("detection_events", "bbox_y1")
    op.drop_column("detection_events", "bbox_x1")
    op.drop_index("ix_detection_events_track_uuid", table_name="detection_events")
    op.drop_column("detection_events", "track_uuid")
    # NOTE: the 'unknown' enum value is intentionally left in place.
