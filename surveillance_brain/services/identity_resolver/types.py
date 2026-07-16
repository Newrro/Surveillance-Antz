"""identity_resolver/types.py — the resolve() return type."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from db.models import Classification, MatchedBy


@dataclass(frozen=True)
class ResolutionResult:
    """Returned by resolve()."""
    classification: Classification
    identity_id: Optional[int]
    matched_by: MatchedBy = MatchedBy.NONE
    similarity: Optional[float] = None
    label: Optional[str] = None

    def __repr__(self) -> str:
        return (
            f"<ResolutionResult cls={self.classification.value} id={self.identity_id} "
            f"by={self.matched_by.value} sim={self.similarity} label={self.label}>"
        )
