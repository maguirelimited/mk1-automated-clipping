"""Structured artifact records for Storage & Data Management.

Classification metadata only. No retention planning or deletion behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EligibilityValue = Literal["true", "false", "unknown"]


@dataclass(frozen=True)
class DeletionEligibility:
    """Descriptive eligibility only — the planner (Phase 4) decides retention.

    ``eligible`` is never computed from retention periods in this phase.
    """

    eligible: EligibilityValue
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"eligible": self.eligible, "reason": self.reason}


@dataclass(frozen=True)
class ArtifactRecord:
    """One classified artifact.

    Fields are descriptive. Protection flags and deletion_eligibility expose
    state for later phases; they do not authorize deletion.
    """

    artifact_type: str
    path: str
    environment: str
    job_id: str | None = None
    run_id: str | None = None
    owner: str | None = None
    size_bytes: int | None = None
    created_at: str | None = None
    modified_at: str | None = None
    age_seconds: float | None = None
    current_state: str | None = None
    classification_source: str = "unclassified"
    protection_flags: tuple[str, ...] = field(default_factory=tuple)
    deletion_eligibility: DeletionEligibility = field(
        default_factory=lambda: DeletionEligibility(
            eligible="unknown",
            reason="planner_not_implemented",
        )
    )
    notes: tuple[str, ...] = field(default_factory=tuple)
    exists: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "path": self.path,
            "environment": self.environment,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "owner": self.owner,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "modified_at": self.modified_at,
            "age_seconds": self.age_seconds,
            "current_state": self.current_state,
            "classification_source": self.classification_source,
            "protection_flags": list(self.protection_flags),
            "deletion_eligibility": self.deletion_eligibility.to_dict(),
            "notes": list(self.notes),
            "exists": self.exists,
        }
