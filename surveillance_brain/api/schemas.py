"""
api/schemas.py
==============
Pydantic v2 request/response models for the Brain API.

Contracts:
    Part 1 → Part 2 : DetectionEventIn   (the ingest payload)
    Part 2 → Part 3 : EventOut / EventsResponse / PersonProfile / EmployeeOut
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import config


# ---------------------------------------------------------------------------
# POST /events  — Part 1 → Part 2 ingest payload
# ---------------------------------------------------------------------------
class DetectionEventIn(BaseModel):
    """
    Inbound payload from Part 1 (Perception Pipeline).

    Both embeddings are optional individually, but AT LEAST ONE must be
    present (face is strongly preferred).  `detection_conf` is 0.0–1.0.
    """
    detection_id: Optional[str] = Field(None, max_length=64, description="Part 1 per-detection id")
    camera_id: str = Field(..., description="String camera UID, e.g. 'GATE-01'")
    timestamp: datetime = Field(..., description="ISO8601 UTC detection time from the edge")
    detection_conf: float = Field(..., ge=0.0, le=1.0, description="Detection confidence 0.0–1.0")
    face_embedding: Optional[List[float]] = Field(None, description="512-dim face embedding")
    body_embedding: Optional[List[float]] = Field(None, description="512-dim body ReID embedding")
    snapshot_path: Optional[str] = Field(None, description="Path/URL to the person snapshot")
    clip_path: Optional[str] = Field(None, description="Path/URL to the short clip")

    @field_validator("face_embedding", "body_embedding")
    @classmethod
    def _check_dim(cls, v: Optional[List[float]]) -> Optional[List[float]]:
        if v is not None and len(v) != config.EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"embedding length {len(v)} != EMBEDDING_DIMENSIONS {config.EMBEDDING_DIMENSIONS}"
            )
        return v

    @model_validator(mode="after")
    def _require_one_embedding(self) -> "DetectionEventIn":
        if not self.face_embedding and not self.body_embedding:
            raise ValueError("at least one of face_embedding or body_embedding is required")
        return self


# ---------------------------------------------------------------------------
# Event object  — Part 2 → Part 3 (also POST /events reply)
# ---------------------------------------------------------------------------
class EventOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_id: Optional[int] = None
    detection_id: Optional[str] = None
    time: Optional[str] = None
    camera: Optional[str] = None
    camera_id: Optional[int] = None
    zone_id: Optional[str] = None
    person_id: Optional[str] = None          # display_label, e.g. "EMP-2026-0001"
    identity_id: Optional[int] = None
    label: Optional[str] = None              # "Employee" | "Visitor" | "Unknown"
    name: Optional[str] = None
    confidence: Optional[float] = None
    matched_by: Optional[str] = None         # "face" | "body" | "none"
    similarity: Optional[float] = None
    snapshot: Optional[str] = None
    clip: Optional[str] = None
    duplicate: Optional[bool] = None


class EventsResponse(BaseModel):
    count: int
    events: List[EventOut]


class RosterResponse(BaseModel):
    """One aggregated row per person for the Report/roster. Each person is their
    LATEST sighting (same shape as an event) plus first_seen + sighting_count."""
    count: int
    people: List[EventOut]


# ---------------------------------------------------------------------------
# GET /person/{id}
# ---------------------------------------------------------------------------
class PersonProfile(BaseModel):
    model_config = ConfigDict(extra="allow")

    identity_id: int
    label: str
    type: str
    name: Optional[str] = None
    department: Optional[str] = None
    email: Optional[str] = None
    first_seen_at: Optional[str] = None
    live: Dict[str, Any] = Field(default_factory=dict)
    photos: List[str] = Field(default_factory=list)
    history: List[Dict[str, Any]] = Field(default_factory=list)
    sessions: List[Dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# /employees
# ---------------------------------------------------------------------------
class EmployeePhotoIn(BaseModel):
    """Enroll an employee from uploaded face PHOTO(S). The browser base64-encodes
    the image file(s); the Brain shells out to the AI face extractor to embed them
    (avoids a python-multipart dependency and keeps the ML model in the AI venv)."""
    name: str = Field(..., min_length=1, max_length=128)
    department: str = Field(..., min_length=1, max_length=64)
    email: Optional[str] = None
    images: List[str] = Field(..., min_length=1, description="base64 JPEG/PNG (data URL ok)")


class EmployeeIn(BaseModel):
    """
    Enroll an employee.  The embedding is produced by Part 1 (the Brain runs
    no ML) — the UI uploads a photo to Part 1's extractor and passes the
    resulting embedding here.  `photo_path` is stored for display only.
    """
    name: str = Field(..., min_length=1, max_length=128)
    department: str = Field(..., min_length=1, max_length=64)
    email: Optional[str] = None
    face_embedding: List[float] = Field(..., description="512-dim face embedding")
    body_embedding: Optional[List[float]] = None
    photo_path: Optional[str] = None

    @field_validator("face_embedding", "body_embedding")
    @classmethod
    def _check_dim(cls, v: Optional[List[float]]) -> Optional[List[float]]:
        if v is not None and len(v) != config.EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"embedding length {len(v)} != EMBEDDING_DIMENSIONS {config.EMBEDDING_DIMENSIONS}"
            )
        return v


class EmployeeOut(BaseModel):
    identity_id: int
    label: str
    name: str
    department: str
    email: Optional[str] = None
    photo_path: Optional[str] = None


class EmployeeRecord(BaseModel):
    identity_id: int
    label: Optional[str] = None
    name: str
    department: str
    email: Optional[str] = None
    hired_at: Optional[str] = None


class EmployeeListResponse(BaseModel):
    count: int
    employees: List[EmployeeRecord]


# ---------------------------------------------------------------------------
# /identities promote/demote
# ---------------------------------------------------------------------------
class PromoteRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    department: str = Field(..., min_length=1, max_length=64)
    email: Optional[str] = None


class NameRequest(BaseModel):
    # Empty string is allowed — it clears the name back to unnamed.
    name: str = Field(..., max_length=128)


class ConversionResponse(BaseModel):
    identity_id: int
    new_label: str
    new_type: str


class MergeRequest(BaseModel):
    """Fold `duplicate_ids` into `primary_id` (all become one person). The caller
    picks which id survives — the UI keeps an employee, else the oldest."""
    primary_id: int
    duplicate_ids: List[int] = Field(..., min_length=1)


class DeleteIdentitiesRequest(BaseModel):
    """Permanently delete these identities (sightings, sessions, vectors, row)."""
    identity_ids: List[int] = Field(..., min_length=1)


class DeleteEventsRequest(BaseModel):
    """Delete individual sightings (detection_events) by id."""
    event_ids: List[int] = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# /search
# ---------------------------------------------------------------------------
class SearchResponse(BaseModel):
    status: str  # "inside" | "not_in_facility" | "not_found"
    identity_id: Optional[int] = None
    label: Optional[str] = None
    camera_id: Optional[int] = None
    zone_id: Optional[str] = None
    last_seen: Optional[str] = None
    stream_url: Optional[str] = None


# ---------------------------------------------------------------------------
# /logs/individual
# ---------------------------------------------------------------------------
class SessionLogEntry(BaseModel):
    session_id: int
    entry_at: Optional[str] = None
    exit_at: Optional[str] = None
    entry_camera_id: Optional[int] = None
    exit_camera_id: Optional[int] = None
    duration_seconds: Optional[float] = None
    status: str


class IndividualLogResponse(BaseModel):
    identity_id: int
    sessions: List[SessionLogEntry]


# ---------------------------------------------------------------------------
# Generic
# ---------------------------------------------------------------------------
class HealthResponse(BaseModel):
    status: str = "ok"
    database: str = "unknown"
    redis: str = "unknown"
    qdrant: str = "unknown"
    version: str = "2.0.0"


class ErrorResponse(BaseModel):
    detail: str
