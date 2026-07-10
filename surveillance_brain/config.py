"""
config.py
=========
Central configuration for the Surveillance Brain (Project RUNG01 — Part 2).

ALL deployment-specific values are read from environment variables so the
same image can be promoted across dev / staging / prod without code changes.
Sensible localhost defaults are provided for `docker compose up`.

Sections:
  1. PostgreSQL 16          — structured records + logs (permanent)
  2. Qdrant                 — face + body embedding vector search
  3. Redis                  — live presence cache (TTL) + duplicate guard
  4. Matching thresholds    — detection confidence + cosine similarity
  5. Local storage          — JSON/JSONL archive of logs + datasheet
  6. Session + cron         — presence TTL, midnight flush, archive
  7. Admin Basic-Auth
  8. Runtime flags

Architecture note (vs. the old single-DB Brain):
  * Embeddings no longer live in Postgres/pgvector — they live in Qdrant.
  * Part 1 (Perception) now sends TWO embeddings per person: a face
    embedding (primary identity signal) and a body ReID embedding
    (cross-camera fallback), plus a `detection_id`, `snapshot_path`
    and `clip_path`.
  * `detection_conf` is a 0.0–1.0 probability (NOT a 0–100 percentage).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Final, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- PostgreSQL (async) ---------------------------------------------
    POSTGRES_USER: str = "surveillance"
    POSTGRES_PASSWORD: str = "surveillance"
    POSTGRES_DB: str = "surveillance"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432

    # ---- Qdrant (vector DB) ---------------------------------------------
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_API_KEY: Optional[str] = None
    # Docker-free / embedded mode: when set, the client runs Qdrant in-process
    # against this on-disk path instead of connecting to a server (host/port are
    # then ignored). Only one process may hold the path at a time — seed runs to
    # completion and closes before the API opens it. Leave empty for a server.
    QDRANT_LOCAL_PATH: Optional[str] = None
    # Two collections — one per embedding modality.
    QDRANT_FACE_COLLECTION: str = "faces"
    QDRANT_BODY_COLLECTION: str = "bodies"

    # ---- Redis ----------------------------------------------------------
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: Optional[str] = None

    # ---- Matching thresholds --------------------------------------------
    # Part 1 sends `detection_conf` in the range 0.0–1.0.  Below this the
    # detection is classified UNKNOWN and NO vector search is performed.
    # This gates IDENTITY, not detection quality — gating high dumped real
    # gate-distance people (detected at 0.5–0.8) into un-deduped Unknown rows.
    DETECTION_CONF_THRESHOLD: float = 0.50

    # Cosine similarity floor (1 - cosine_distance) for a FACE match.
    # AdaFace on CCTV: same-person ~0.35+, impostors <0.2. 0.65 was so strict
    # face never won and everything fell back to body ReID.
    FACE_SIMILARITY_THRESHOLD: float = 0.42

    # ── OPEN-SET 1:N face identification (IDENTITY_REDESIGN.md Phase A) ──────
    # Identity is FACE-ONLY and tuned for PRECISION. Three-way decision on the
    # tracklet's face template vs the gallery, where s1/s2 = top-1/top-2 cosine to
    # DISTINCT identities:
    #   assign to id1  iff  s1 >= FACE_ASSIGN_THRESHOLD AND (s1 - s2) >= FACE_MARGIN
    #   new provisional iff s1 <  FACE_NEW_THRESHOLD
    #   otherwise      ABSTAIN → Unknown (ambiguous; never guess, never merge)
    # The MARGIN is what rejects look-alikes; the abstain band is what stops the
    # over-merge magnets. Calibrate on tools/mtmct_eval (drive FPIR down).
    FACE_ASSIGN_THRESHOLD: float = 0.62      # confident same-person (assign + confirm)
    FACE_MARGIN: float = 0.08                # top-1 must beat top-2 by this
    FACE_NEW_THRESHOLD: float = 0.45         # below this to everyone → clearly new person
    # Quality floor (AdaFace-norm × sharpness proxy) a face must clear to be used
    # for identity at all. Low-quality faces contribute nothing (→ tracklet may be
    # faceless → Unknown). This is the single biggest precision lever.
    FACE_MIN_QUALITY: float = 18.0

    # Cosine similarity floor for a BODY ReID match — used only as a
    # fallback when the face embedding is absent or below threshold.
    # OSNet is noisy, so this must be conservative: too low merges different
    # people into one identity across cameras. 0.85 favours splitting over
    # merging — confidence over coverage (fragments are cleaned nightly).
    BODY_SIMILARITY_THRESHOLD: float = 0.85

    # Constrained body RE-LINK (identity_resolver step 4). Before creating a NEW
    # visitor from an unmatched face, we check the body vector against identities
    # seen on the SAME camera within BODY_MERGE_WINDOW_SECONDS. A hit at/above
    # BODY_MERGE_THRESHOLD re-links to that identity instead of minting a
    # duplicate. Much higher than the plain body fallback + gated by camera and a
    # short time window, so it re-joins a fragmented sighting of ONE person
    # without merging two strangers in similar clothing.
    BODY_MERGE_THRESHOLD: float = 0.82
    BODY_MERGE_WINDOW_SECONDS: int = 90

    # Offline gallery consolidation (Phase 2, consolidate_identities script).
    # Two VISITOR identities whose FACE centroids match at/above this cosine are
    # judged the same person and merged (the older id kept). Higher than the live
    # match floor (0.42) so an offline batch merge is conservative — it only
    # collapses clear duplicates that slipped through, never near-strangers.
    CONSOLIDATE_FACE_THRESHOLD: float = 0.55

    # Scheduled consolidation (Phase 2): run the face-centroid merge as a periodic
    # IN-PROCESS job (must be in-process — it shares the embedded Qdrant the Brain
    # holds). SAFE DEFAULT: log the merge plan but DO NOT apply — auto-merging a
    # borderline face match can fold two people together (measured a 0.58 cross-
    # person plan). Flip CONSOLIDATE_APPLY=1 only once the threshold is trusted;
    # otherwise use the Settings → "Merge duplicate visitors" button (human review).
    # Gallery hygiene (Phase 3d): cap the stored views per identity per modality.
    # Newest kept, oldest pruned on each new insert — bounds vector-set growth and
    # lets stale views age out (a person's look drifts over days). 0 = unbounded.
    GALLERY_MAX_VIEWS: int = 12

    CONSOLIDATE_ENABLE: bool = True
    CONSOLIDATE_APPLY: bool = False              # False = log-only dry run each cycle
    CONSOLIDATE_EVERY_MINUTES: int = 60
    # A STRICTER threshold for unattended auto-apply than the manual button uses.
    CONSOLIDATE_AUTO_FACE_THRESHOLD: float = 0.62

    # Progressive learning: when a matched sighting scores BELOW this, store its
    # embedding(s) as an additional view for that identity, so future sightings
    # from a new angle (or with the face now visible) still match — this is what
    # stops one person fragmenting into many ids. Above it the view is a near-
    # duplicate we already have, so we skip it (keeps the vector set from bloating).
    LEARN_SIMILARITY_CEILING: float = 0.92

    # Must match Part 1's model output dimension (face + body share it here).
    EMBEDDING_DIMENSIONS: int = 512

    # ---- Local storage (JSON/JSONL archive) -----------------------------
    # Postgres is the permanent source of truth; these files are the
    # human-readable export/archive each feature writes for offline use.
    STORAGE_ROOT: str = "storage"          # snapshots/clips referenced by Part 1 live here too
    LOG_DIR: str = "storage/logs"          # append-only events.jsonl
    DATASHEET_DIR: str = "storage/datasheet"  # per-person datasheet snapshots (JSON)

    # ---- Session + cron -------------------------------------------------
    # TTL on the Redis live-presence hash (seconds).  This is the ONLY
    # place a TTL is applied — Postgres records are permanent.
    SESSION_TIMEOUT_SECONDS: int = 300

    # Short window (seconds) during which repeated detections of the same
    # person on the same camera are treated as duplicates and NOT re-logged.
    DUPLICATE_WINDOW_SECONDS: int = 30

    # APScheduler cron expressions (UTC).
    MIDNIGHT_FLUSH_CRON: str = "0 0 * * *"   # close dangling sessions @ 00:00
    ARCHIVE_CRON: str = "*/30 * * * *"       # export logs+datasheet every 30 min
    RETENTION_CRON: str = "30 0 * * *"       # prune old detection_events @ 00:30

    # Delete detection_events older than this many days (0 = keep forever). They
    # are archived to JSONL first (ARCHIVE_CRON), so this only trims the live
    # ledger — the unbounded table on a 24/7 system. Vectors are per-identity
    # (bounded) and unknowns are cleared daily, so only this table needs ageing.
    RETENTION_DAYS: int = 7

    # ---- Admin Basic-Auth -----------------------------------------------
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "changeme"

    # ---- Runtime --------------------------------------------------------
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    ENVIRONMENT: str = "dev"  # dev | prod

    # ---- Derived connection strings -------------------------------------
    @property
    def DATABASE_DSN(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def DATABASE_DSN_SYNC(self) -> str:
        """Sync DSN used by Alembic only."""
        return (
            f"postgresql+psycopg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def REDIS_URL(self) -> str:
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def QDRANT_URL(self) -> str:
        return f"http://{self.QDRANT_HOST}:{self.QDRANT_PORT}"


_settings = Settings()

# ---- Module-level constants (import directly anywhere) --------------------
DATABASE_DSN: Final[str] = _settings.DATABASE_DSN
DATABASE_DSN_SYNC: Final[str] = _settings.DATABASE_DSN_SYNC
REDIS_URL: Final[str] = _settings.REDIS_URL
QDRANT_URL: Final[str] = _settings.QDRANT_URL
QDRANT_LOCAL_PATH: Final[Optional[str]] = _settings.QDRANT_LOCAL_PATH
QDRANT_API_KEY: Final[Optional[str]] = _settings.QDRANT_API_KEY
QDRANT_FACE_COLLECTION: Final[str] = _settings.QDRANT_FACE_COLLECTION
QDRANT_BODY_COLLECTION: Final[str] = _settings.QDRANT_BODY_COLLECTION

DETECTION_CONF_THRESHOLD: Final[float] = _settings.DETECTION_CONF_THRESHOLD
FACE_SIMILARITY_THRESHOLD: Final[float] = _settings.FACE_SIMILARITY_THRESHOLD
FACE_ASSIGN_THRESHOLD: Final[float] = _settings.FACE_ASSIGN_THRESHOLD
FACE_MARGIN: Final[float] = _settings.FACE_MARGIN
FACE_NEW_THRESHOLD: Final[float] = _settings.FACE_NEW_THRESHOLD
FACE_MIN_QUALITY: Final[float] = _settings.FACE_MIN_QUALITY
BODY_SIMILARITY_THRESHOLD: Final[float] = _settings.BODY_SIMILARITY_THRESHOLD
BODY_MERGE_THRESHOLD: Final[float] = _settings.BODY_MERGE_THRESHOLD
BODY_MERGE_WINDOW_SECONDS: Final[int] = _settings.BODY_MERGE_WINDOW_SECONDS
GALLERY_MAX_VIEWS: Final[int] = _settings.GALLERY_MAX_VIEWS
CONSOLIDATE_FACE_THRESHOLD: Final[float] = _settings.CONSOLIDATE_FACE_THRESHOLD
CONSOLIDATE_ENABLE: Final[bool] = _settings.CONSOLIDATE_ENABLE
CONSOLIDATE_APPLY: Final[bool] = _settings.CONSOLIDATE_APPLY
CONSOLIDATE_EVERY_MINUTES: Final[int] = _settings.CONSOLIDATE_EVERY_MINUTES
CONSOLIDATE_AUTO_FACE_THRESHOLD: Final[float] = _settings.CONSOLIDATE_AUTO_FACE_THRESHOLD
LEARN_SIMILARITY_CEILING: Final[float] = _settings.LEARN_SIMILARITY_CEILING
EMBEDDING_DIMENSIONS: Final[int] = _settings.EMBEDDING_DIMENSIONS

STORAGE_ROOT: Final[str] = _settings.STORAGE_ROOT
LOG_DIR: Final[str] = _settings.LOG_DIR
DATASHEET_DIR: Final[str] = _settings.DATASHEET_DIR

SESSION_TIMEOUT_SECONDS: Final[int] = _settings.SESSION_TIMEOUT_SECONDS
DUPLICATE_WINDOW_SECONDS: Final[int] = _settings.DUPLICATE_WINDOW_SECONDS
MIDNIGHT_FLUSH_CRON: Final[str] = _settings.MIDNIGHT_FLUSH_CRON
ARCHIVE_CRON: Final[str] = _settings.ARCHIVE_CRON
RETENTION_CRON: Final[str] = _settings.RETENTION_CRON
RETENTION_DAYS: Final[int] = _settings.RETENTION_DAYS

ADMIN_USERNAME: Final[str] = _settings.ADMIN_USERNAME
ADMIN_PASSWORD: Final[str] = _settings.ADMIN_PASSWORD

LOG_LEVEL: Final[str] = _settings.LOG_LEVEL
ENVIRONMENT: Final[str] = _settings.ENVIRONMENT


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """FastAPI dependency — returns the cached Settings singleton."""
    return _settings
