"""M7 Release Qualification: immutable record gating auto-send capability.

The Release Qualification record is the second layer of the four-layer auto-send
gate. It binds a specific commit SHA, migration head, dataset versions, and test
results to a validity window. Any change to deployment version, migration head,
or dataset versions invalidates the qualification and auto-disables auto-send.

Invariants:
- The record is immutable once generated (frozen dataclass).
- Invalidation is automatic on version/migration/dataset change.
- Only a valid qualification + explicit user opt-in enables auto-send.
- The record includes a content hash for tamper detection.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class ReleaseQualificationRecord:
    """Immutable Release Qualification record (M7.9).

    Contains commit SHA, migration head, dataset versions, test results,
    and validity window. Any field change produces a new record with a new hash.
    """

    commit_sha: str
    migration_head: str
    dataset_versions: tuple[str, ...]
    test_suite_results: dict[str, bool] = field(default_factory=lambda: {})
    security_scan_passed: bool = False
    coverage_percent: float = 0.0
    generated_at: datetime | None = None
    expires_at: datetime | None = None
    record_hash: str = ""

    def __post_init__(self) -> None:
        if self.record_hash and self.record_hash != self._compute_hash():
            raise ValueError("record_hash does not match content; record may be tampered")

    def _compute_hash(self) -> str:
        canonical = json.dumps(
            {
                "commit_sha": self.commit_sha,
                "migration_head": self.migration_head,
                "dataset_versions": list(self.dataset_versions),
                "test_suite_results": self.test_suite_results,
                "security_scan_passed": self.security_scan_passed,
                "coverage_percent": self.coverage_percent,
                "generated_at": self.generated_at.isoformat() if self.generated_at else None,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            },
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def content_hash(self) -> str:
        return self._compute_hash()


def generate_release_qualification(
    *,
    commit_sha: str,
    migration_head: str,
    dataset_versions: tuple[str, ...],
    test_suite_results: dict[str, bool],
    security_scan_passed: bool,
    coverage_percent: float,
    generated_at: datetime,
    validity_days: int = 30,
) -> ReleaseQualificationRecord:
    """Generate an immutable Release Qualification record.

    All required test suites must pass and security scan must be clean.
    """
    if not commit_sha or len(commit_sha) < 7:
        raise ValueError("commit_sha must be a valid git SHA (>= 7 chars)")
    if not migration_head:
        raise ValueError("migration_head must not be empty")
    if not all(test_suite_results.values()):
        failed = [k for k, v in test_suite_results.items() if not v]
        raise ValueError(f"test suites failed: {', '.join(failed)}")
    if not security_scan_passed:
        raise ValueError("security scan must pass for release qualification")
    if coverage_percent < 75.0:
        raise ValueError(f"coverage {coverage_percent:.1f}% below 75% threshold")

    expires_at = generated_at + timedelta(days=validity_days)
    record = ReleaseQualificationRecord(
        commit_sha=commit_sha,
        migration_head=migration_head,
        dataset_versions=dataset_versions,
        test_suite_results=dict(test_suite_results),
        security_scan_passed=security_scan_passed,
        coverage_percent=coverage_percent,
        generated_at=generated_at,
        expires_at=expires_at,
    )
    # Freeze with hash
    return ReleaseQualificationRecord(
        commit_sha=record.commit_sha,
        migration_head=record.migration_head,
        dataset_versions=record.dataset_versions,
        test_suite_results=record.test_suite_results,
        security_scan_passed=record.security_scan_passed,
        coverage_percent=record.coverage_percent,
        generated_at=record.generated_at,
        expires_at=record.expires_at,
        record_hash=record.content_hash,
    )


def is_qualification_valid(
    record: ReleaseQualificationRecord,
    *,
    current_commit_sha: str,
    current_migration_head: str,
    current_dataset_versions: tuple[str, ...],
    now: datetime,
) -> tuple[bool, tuple[str, ...]]:
    """Check whether a Release Qualification is still valid.

    Invalidation triggers (M7.10):
    - Record expired.
    - Commit SHA changed (deployment version change).
    - Migration head changed.
    - Dataset versions changed.
    - Record hash mismatch (tamper detection).
    """
    reasons: list[str] = []

    if record.record_hash and record.record_hash != record.content_hash:
        reasons.append("RECORD_HASH_MISMATCH")

    if record.expires_at is not None and now >= record.expires_at:
        reasons.append("QUALIFICATION_EXPIRED")

    if record.commit_sha != current_commit_sha:
        reasons.append("COMMIT_SHA_CHANGED")

    if record.migration_head != current_migration_head:
        reasons.append("MIGRATION_HEAD_CHANGED")

    if record.dataset_versions != current_dataset_versions:
        reasons.append("DATASET_VERSIONS_CHANGED")

    return (len(reasons) == 0, tuple(reasons))
