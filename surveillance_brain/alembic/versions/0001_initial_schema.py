"""initial schema: identities, employees, visitors, cameras, presence_sessions, detection_events

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-07-02 00:00:00

Creates the full Part 2 relational schema.  Embeddings live in Qdrant, so
there is NO feature_embeddings table and NO pgvector extension here.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # identities
    # ------------------------------------------------------------------ #
    op.create_table(
        "identities",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("identity_type", sa.Enum("visitor", "employee", name="identity_type"), nullable=False),
        sa.Column("display_label", sa.String(length=32), nullable=False),
        sa.Column("is_anonymized", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("display_label"),
    )
    op.create_index("ix_identities_identity_type", "identities", ["identity_type"])
    op.create_index("ix_identities_display_label", "identities", ["display_label"])

    # ------------------------------------------------------------------ #
    # employees
    # ------------------------------------------------------------------ #
    op.create_table(
        "employees",
        sa.Column("identity_id", sa.BigInteger(), nullable=False),
        sa.Column("employee_seq", sa.Integer(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("department", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=128), nullable=True),
        sa.Column("hired_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["identity_id"], ["identities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("identity_id"),
        sa.UniqueConstraint("year", "employee_seq", name="uq_employees_year_seq"),
    )
    op.create_index("ix_employees_name", "employees", ["name"])

    # ------------------------------------------------------------------ #
    # visitors
    # ------------------------------------------------------------------ #
    op.create_table(
        "visitors",
        sa.Column("identity_id", sa.BigInteger(), nullable=False),
        sa.Column("visitor_seq", sa.Integer(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["identity_id"], ["identities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("identity_id"),
        sa.UniqueConstraint("year", "visitor_seq", name="uq_visitors_year_seq"),
    )

    # ------------------------------------------------------------------ #
    # cameras
    # ------------------------------------------------------------------ #
    op.create_table(
        "cameras",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("camera_uid", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("zone_id", sa.String(length=64), nullable=False),
        sa.Column("is_exit_camera", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("stream_url", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("camera_uid"),
    )
    op.create_index("ix_cameras_camera_uid", "cameras", ["camera_uid"])
    op.create_index("ix_cameras_zone_id", "cameras", ["zone_id"])

    # ------------------------------------------------------------------ #
    # presence_sessions
    # ------------------------------------------------------------------ #
    op.create_table(
        "presence_sessions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("identity_id", sa.BigInteger(), nullable=False),
        sa.Column("entry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_camera_id", sa.BigInteger(), nullable=True),
        sa.Column("exit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_camera_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Enum("inside", "exited", name="session_status"), nullable=False, server_default="inside"),
        sa.ForeignKeyConstraint(["identity_id"], ["identities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entry_camera_id"], ["cameras.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["exit_camera_id"], ["cameras.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_presence_sessions_identity_id", "presence_sessions", ["identity_id"])
    op.create_index("ix_sessions_identity_status", "presence_sessions", ["identity_id", "status"])
    op.create_index("ix_sessions_entry_at", "presence_sessions", ["entry_at"])
    op.create_index("ix_presence_sessions_status", "presence_sessions", ["status"])

    # ------------------------------------------------------------------ #
    # detection_events
    # ------------------------------------------------------------------ #
    op.create_table(
        "detection_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("detection_id", sa.String(length=64), nullable=True),
        sa.Column("identity_id", sa.BigInteger(), nullable=True),
        sa.Column("camera_id", sa.BigInteger(), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detection_conf", sa.Float(), nullable=False),
        sa.Column("classification", sa.Enum("employee", "visitor", "unknown", name="classification"), nullable=False),
        sa.Column("matched_by", sa.Enum("face", "body", "none", name="matched_by"), nullable=False, server_default="none"),
        sa.Column("similarity", sa.Float(), nullable=True),
        sa.Column("snapshot_path", sa.Text(), nullable=True),
        sa.Column("clip_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["identity_id"], ["identities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_detection_events_detection_id", "detection_events", ["detection_id"])
    op.create_index("ix_detection_events_identity_id", "detection_events", ["identity_id"])
    op.create_index("ix_detection_events_camera_id", "detection_events", ["camera_id"])
    op.create_index("ix_detection_events_detected_at", "detection_events", ["detected_at"])
    op.create_index("ix_detection_events_classification", "detection_events", ["classification"])


def downgrade() -> None:
    op.drop_index("ix_detection_events_classification", table_name="detection_events")
    op.drop_index("ix_detection_events_detected_at", table_name="detection_events")
    op.drop_index("ix_detection_events_camera_id", table_name="detection_events")
    op.drop_index("ix_detection_events_identity_id", table_name="detection_events")
    op.drop_index("ix_detection_events_detection_id", table_name="detection_events")
    op.drop_table("detection_events")

    op.drop_index("ix_presence_sessions_status", table_name="presence_sessions")
    op.drop_index("ix_sessions_entry_at", table_name="presence_sessions")
    op.drop_index("ix_sessions_identity_status", table_name="presence_sessions")
    op.drop_index("ix_presence_sessions_identity_id", table_name="presence_sessions")
    op.drop_table("presence_sessions")

    op.drop_index("ix_cameras_zone_id", table_name="cameras")
    op.drop_index("ix_cameras_camera_uid", table_name="cameras")
    op.drop_table("cameras")

    op.drop_table("visitors")

    op.drop_index("ix_employees_name", table_name="employees")
    op.drop_table("employees")

    op.drop_index("ix_identities_display_label", table_name="identities")
    op.drop_index("ix_identities_identity_type", table_name="identities")
    op.drop_table("identities")

    op.execute("DROP TYPE IF EXISTS matched_by")
    op.execute("DROP TYPE IF EXISTS classification")
    op.execute("DROP TYPE IF EXISTS session_status")
    op.execute("DROP TYPE IF EXISTS identity_type")
