"""Explicit per-sighting evidence media columns.

ONE sighting = ONE immutable evidence set. These columns make every companion
file EXPLICIT — face crop, body crop, untouched original full frame, separate
annotated copy — so no consumer ever derives one path from another file's name
(the `bodyUrl.replace('.jpg','_face.jpg')` convention this replaces).

The backfill translates pre-rework rows from that legacy convention ONCE, here,
so the convention can be deleted from every consumer. Uses IF NOT EXISTS so a
database whose 0003 briefly carried these columns (dev snapshots) migrates
cleanly too.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0004_evidence_media_paths"
down_revision: Union[str, None] = "0003_sightings_audit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE detection_events ADD COLUMN IF NOT EXISTS face_path TEXT")
    op.execute("ALTER TABLE detection_events ADD COLUMN IF NOT EXISTS body_path TEXT")
    op.execute("ALTER TABLE detection_events ADD COLUMN IF NOT EXISTS full_frame_path TEXT")
    op.execute("ALTER TABLE detection_events ADD COLUMN IF NOT EXISTS full_frame_annotated_path TEXT")
    # LAST place the legacy stem convention is ever consulted: translate old
    # rows so their evidence keeps displaying after the UI drops derivation.
    op.execute(
        r"""
        UPDATE detection_events
           SET body_path = snapshot_path,
               face_path = regexp_replace(snapshot_path, '\.jpg$', '_face.jpg'),
               full_frame_path = regexp_replace(snapshot_path, '\.jpg$', '_full.jpg')
         WHERE snapshot_path IS NOT NULL AND body_path IS NULL
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE detection_events DROP COLUMN IF EXISTS full_frame_annotated_path")
    op.execute("ALTER TABLE detection_events DROP COLUMN IF EXISTS full_frame_path")
    op.execute("ALTER TABLE detection_events DROP COLUMN IF EXISTS body_path")
    op.execute("ALTER TABLE detection_events DROP COLUMN IF EXISTS face_path")
