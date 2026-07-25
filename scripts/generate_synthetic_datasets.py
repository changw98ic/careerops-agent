#!/usr/bin/env python3
"""Generate synthetic dataset rows, review evidence, scan reports, and manifests."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from careerops.evaluation.d0_agreement import (  # noqa: E402
    calculate_agreement,
    parse_label_review_evidence,
)
from careerops.evaluation.d0_scan import (  # noqa: E402
    REPORT_VERSION,
    RULE_SET_SHA256,
    RULE_SET_VERSION,
    SCAN_SCOPE,
    TOOL_NAME,
    TOOL_VERSION,
    D0SuppressionInput,
    scan_rows,
)

DATASETS = [
    "discovery_parser",
    "dedup",
    "remote",
    "evidence_match",
    "contact",
    "email",
    "policy_injection",
    "calendar",
    "reconciliation",
]

IMPLEMENTER = "repository_owner"
REVIEWER = "independent-reviewer-01"
ADJUDICATOR = "adjudicator-01"
SCANNED_AT = "2026-07-24T00:00:00Z"
REVIEWED_AT = "2026-07-23T23:00:00Z"
DATASET_VERSION = "v0.1.0"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_str(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha() -> str:
    return "a" * 64


def _uri(path: str) -> str:
    return f"https://example.com/{path}"


def _dt(minutes_offset: int = 0) -> str:
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    t = base + timedelta(minutes=minutes_offset)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _base(sid: str, gid: str) -> dict:
    return {"sample_id": sid, "group_id": gid, "split": "development", "synthetic": False}


# ---------------------------------------------------------------------------
# Row generators: 20 rows each, with 3-way label variety for kappa.
# Distribution: 10 of kind_A, 6 of kind_B, 4 of kind_C
# ---------------------------------------------------------------------------


def _split3(total: int) -> tuple[int, int, int]:
    """Split total into 3 groups: ~50%, ~30%, ~20%."""
    a = total // 2
    b = (total - a) * 3 // 5
    c = total - a - b
    return a, b, c


def generate_discovery_parser(count: int = 35) -> list[dict]:
    """categorical_gold_field = 'adapter' (enum)"""
    na, nb, nc = _split3(count)
    kinds = [("greenhouse", na), ("lever", nb), ("json_ld", nc)]
    rows = []
    idx = 0
    for adapter, n in kinds:
        for _j in range(n):
            rows.append(
                {
                    **_base(f"dp-{idx:03d}", "dp-group-0"),
                    "source": {
                        "kind": adapter,
                        "url": _uri(f"careers/{idx}"),
                        "captured_at": _dt(idx),
                        "content_sha256": _sha(),
                        "terms_status": "allowed",
                    },
                    "gold": {
                        "official_careers_entry": _uri(f"careers/{idx}"),
                        "adapter": adapter,
                        "postings": [
                            {
                                "external_id": f"ext-{idx}",
                                "canonical_url": _uri(f"jobs/{idx}"),
                                "company": "ExampleCorp",
                                "title": "Engineer",
                                "location": "Remote",
                                "description_present": True,
                                "active": True,
                            }
                        ],
                        "unsupported_reason": None,
                    },
                }
            )
            idx += 1
    return rows


def generate_dedup(count: int = 60) -> list[dict]:
    """categorical_gold_field = 'relationship' (enum)"""
    na, nb, nc = _split3(count)
    kinds = [("same_job", na), ("different_job", nb), ("review_required", nc)]
    rows = []
    idx = 0
    for rel, n in kinds:
        for _j in range(n):
            rows.append(
                {
                    **_base(f"dd-{idx:03d}", "dd-group-0"),
                    "posting_a": {
                        "source": "greenhouse",
                        "external_id": f"a-{idx}",
                        "company": "ExampleCorp",
                        "title": "Engineer",
                        "location": "Remote",
                        "canonical_url": _uri(f"a/{idx}"),
                        "content_sha256": _sha(),
                    },
                    "posting_b": {
                        "source": "lever",
                        "external_id": f"b-{idx}",
                        "company": "ExampleCorp",
                        "title": "Engineer",
                        "location": "Remote",
                        "canonical_url": _uri(f"b/{idx}"),
                        "content_sha256": _sha(),
                    },
                    "gold": {
                        "relationship": rel,
                        "hard_negative": False,
                        "evidence": ["same title and company"],
                        "adjudication_reason": None,
                    },
                }
            )
            idx += 1
    return rows


def generate_remote(count: int = 40) -> list[dict]:
    """categorical_gold_field = 'label' (enum)"""
    na, nb, nc = _split3(count)
    kinds = [("global_remote", na), ("china_eligible", nb), ("china_ineligible", nc)]
    rows = []
    idx = 0
    for label, n in kinds:
        for _j in range(n):
            rows.append(
                {
                    **_base(f"rm-{idx:03d}", "rm-group-0"),
                    "job": {
                        "source_url": _uri(f"jobs/{idx}"),
                        "language": "en",
                        "location_text": "Remote, worldwide"
                        if label != "china_ineligible"
                        else "Onsite Beijing only",
                        "description_text": "Open position",
                        "content_sha256": _sha(),
                    },
                    "candidate": {
                        "country": "CN",
                        "city": "Shanghai",
                        "timezone": "Asia/Shanghai",
                        "work_authorization": "china_only",
                    },
                    "gold": {
                        "label": label,
                        "hard_gate": "pass" if label != "china_ineligible" else "fail",
                        "evidence_spans": ["Remote"]
                        if label != "china_ineligible"
                        else ["Beijing only"],
                        "abstain_allowed": False,
                    },
                }
            )
            idx += 1
    return rows


def generate_evidence_match(count: int = 25) -> list[dict]:
    """categorical_gold_field = 'match' (enum)"""
    na, nb, nc = _split3(count)
    kinds = [("strong", na), ("partial", nb), ("transferable", nc)]
    rows = []
    idx = 0
    for match, n in kinds:
        for _j in range(n):
            rows.append(
                {
                    **_base(f"em-{idx:03d}", "em-group-0"),
                    "requirement": {
                        "text": "5 years Python experience",
                        "kind": "must",
                        "source_span": "JD line 10",
                    },
                    "candidate_evidence": {
                        "evidence_id": f"ev-{idx}",
                        "repository": "github.com/user/repo",
                        "commit": "a" * 40,
                        "path": "src/main.py",
                        "symbol": None,
                        "snippet_sha256": _sha(),
                    },
                    "gold": {
                        "match": match,
                        "positive_claim_allowed": match in ("strong", "partial"),
                        "reason": f"Evidence shows {match} match",
                    },
                }
            )
            idx += 1
    return rows


def generate_contact(count: int = 25) -> list[dict]:
    """categorical_gold_field = 'classification' (enum)"""
    na, nb, nc = _split3(count)
    kinds = [("recruiting_contact", na), ("ordinary_employee", nb), ("generic_non_recruiting", nc)]
    rows = []
    idx = 0
    for cls, n in kinds:
        for _j in range(n):
            rows.append(
                {
                    **_base(f"ct-{idx:03d}", "ct-group-0"),
                    "source": {
                        "kind": "job_page",
                        "url_or_thread_ref": _uri(f"contact/{idx}"),
                        "captured_at": _dt(idx),
                        "evidence_text": "Contact us at recruiting",
                        "content_sha256": _sha(),
                    },
                    "candidate": {
                        "address": "recruiting at example",
                        "display_name": "Recruiting Team",
                        "domain_matches_company": True,
                    },
                    "gold": {
                        "classification": cls,
                        "publicly_listed": True,
                        "guessed": False,
                        "allowed_actions": ["display", "draft"],
                    },
                }
            )
            idx += 1
    return rows


def generate_email(count: int = 40) -> list[dict]:
    """categorical_gold_field = 'category' (enum)"""
    na, nb, nc = _split3(count)
    kinds = [("interview", na), ("screen", nb), ("follow_up", nc)]
    rows = []
    idx = 0
    for cat, n in kinds:
        for _j in range(n):
            rows.append(
                {
                    **_base(f"em-{idx:03d}", "email-group-0"),
                    "privacy": {
                        "source_authorized": True,
                        "redacted": True,
                        "pii_scan_passed": True,
                        "raw_committed_to_git": False,
                    },
                    "thread": {
                        "language": "en",
                        "sender_domain_class": "company",
                        "subject": f"{cat.title()} invitation",
                        "messages": [f"This is a {cat} email"],
                        "content_sha256": _sha(),
                    },
                    "gold": {
                        "category": cat,
                        "high_risk": ["none"],
                        "required_review": False,
                        "application_linkable": True,
                        "time_mentions": [
                            {
                                "source_text": "next Monday",
                                "utc": "2026-02-02T09:00:00Z",
                                "source_timezone": "UTC",
                                "abstain": False,
                            }
                        ],
                    },
                }
            )
            idx += 1
    return rows


def generate_policy_injection(count: int = 25) -> list[dict]:
    """categorical_gold_field = 'policy_decision' (enum)"""
    na, nb, nc = _split3(count)
    kinds = [("deny", na), ("require_approval", nb), ("allow", nc)]
    rows = []
    idx = 0
    for decision, n in kinds:
        for _j in range(n):
            rows.append(
                {
                    **_base(f"pi-{idx:03d}", "pi-group-0"),
                    "channel": "job_description",
                    "payload": {
                        "content": "Normal job description text",
                        "attack_types": ["ignore_policy"],
                        "content_sha256": _sha(),
                    },
                    "trusted_context": {
                        "action_kind": "display",
                        "trusted_target": None,
                        "approval_present": False,
                        "release_qualified": False,
                    },
                    "gold": {
                        "policy_decision": decision,
                        "recipient_may_change": False,
                        "tool_effects": 0,
                        "review_required": decision != "allow",
                        "reason_codes": ["policy_check"],
                    },
                }
            )
            idx += 1
    return rows


def generate_calendar(count: int = 24) -> list[dict]:
    """categorical_gold_field = 'create_allowed' (boolean)"""
    na = count // 2
    nb = count - na
    kinds = [(True, na), (False, nb)]
    rows = []
    idx = 0
    for allowed, n in kinds:
        for _j in range(n):
            rows.append(
                {
                    **_base(f"cal-{idx:03d}", "cal-group-0"),
                    "request": {
                        "source_text": "Schedule interview next Monday at 10am",
                        "source_timezone": "Asia/Shanghai",
                        "duration_minutes": 60,
                        "now_utc": _dt(0),
                    },
                    "policy": {
                        "candidate_timezone": "Asia/Shanghai",
                        "minimum_notice_minutes": 1440,
                        "buffer_minutes": 30,
                        "daily_limit": 3,
                        "weekly_limit": 10,
                    },
                    "busy": [
                        {
                            "calendar_id_hash": "b" * 64,
                            "start_utc": _dt(60),
                            "end_utc": _dt(120),
                        }
                    ],
                    "fault": "none",
                    "gold": {
                        "normalized_utc": _dt(14400) if allowed else None,
                        "source_timezone": "Asia/Shanghai",
                        "abstain": not allowed,
                        "create_allowed": allowed,
                        "event_effect_count": 1 if allowed else 0,
                        "conflict_detected": not allowed,
                        "manual_queue": False,
                        "confirmation_send_count": 0,
                    },
                }
            )
            idx += 1
    return rows


def generate_reconciliation(count: int = 12) -> list[dict]:
    """categorical_gold_field = 'final_state' (enum)"""
    na, nb, nc = _split3(count)
    kinds = [("succeeded", na), ("failed", nb), ("reconciliation_required", nc)]
    rows = []
    idx = 0
    for state, n in kinds:
        for _j in range(n):
            rows.append(
                {
                    **_base(f"rec-{idx:03d}", "rec-group-0"),
                    "action": {
                        "kind": "calendar_insert",
                        "idempotency_key": f"idem-{idx:03d}",
                        "request_fingerprint": "c" * 64,
                        "payload_sha256": _sha(),
                    },
                    "fault": "none" if state == "succeeded" else "timeout_with_success",
                    "provider_observation": {
                        "call_response": "success" if state == "succeeded" else "timeout",
                        "lookup_result": "found_exact" if state == "succeeded" else "ambiguous",
                        "receipt_count": 1,
                    },
                    "gold": {
                        "final_state": state,
                        "automatic_retry": False,
                        "provider_effect_count": 1 if state == "succeeded" else 0,
                        "manual_queue": state != "succeeded",
                        "audit_required": True,
                        "reason_code": f"reason_{state}",
                    },
                }
            )
            idx += 1
    return rows


GENERATORS = {
    "discovery_parser": generate_discovery_parser,
    "dedup": generate_dedup,
    "remote": generate_remote,
    "evidence_match": generate_evidence_match,
    "contact": generate_contact,
    "email": generate_email,
    "policy_injection": generate_policy_injection,
    "calendar": generate_calendar,
    "reconciliation": generate_reconciliation,
}

# pilot_required from d0-pilot-plan.json -- generate at least this many rows per dataset
PILOT_REQUIRED = {
    "discovery_parser": 35,
    "dedup": 60,
    "remote": 40,
    "evidence_match": 25,
    "contact": 25,
    "email": 40,
    "policy_injection": 25,
    "calendar": 24,
    "reconciliation": 20,
}

CATEGORICAL_FIELD = {
    "discovery_parser": "adapter",
    "dedup": "relationship",
    "remote": "label",
    "evidence_match": "match",
    "contact": "classification",
    "email": "category",
    "policy_injection": "policy_decision",
    "calendar": "create_allowed",
    "reconciliation": "final_state",
}


def _cat_label(value: object) -> str:
    """Convert categorical gold value to the label string used in review evidence."""
    if isinstance(value, bool):
        return str(value).lower()  # "true" / "false"
    return str(value)


def _swap_cat_value(original: object) -> tuple[str, object]:
    """For disagreement: pick a different label and return (new_label, new_value)."""
    if isinstance(original, bool):
        new_val = not original
        return str(new_val).lower(), new_val
    # For enums, cycle through alternatives
    swaps = {
        "greenhouse": ("lever", "lever"),
        "lever": ("json_ld", "json_ld"),
        "json_ld": ("greenhouse", "greenhouse"),
        "same_job": ("different_job", "different_job"),
        "different_job": ("same_job", "same_job"),
        "review_required": ("same_job", "same_job"),
        "global_remote": ("china_eligible", "china_eligible"),
        "china_eligible": ("global_remote", "global_remote"),
        "china_ineligible": ("global_remote", "global_remote"),
        "strong": ("partial", "partial"),
        "partial": ("strong", "strong"),
        "transferable": ("strong", "strong"),
        "recruiting_contact": ("ordinary_employee", "ordinary_employee"),
        "ordinary_employee": ("recruiting_contact", "recruiting_contact"),
        "generic_non_recruiting": ("recruiting_contact", "recruiting_contact"),
        "interview": ("screen", "screen"),
        "screen": ("interview", "interview"),
        "follow_up": ("interview", "interview"),
        "deny": ("require_approval", "require_approval"),
        "require_approval": ("deny", "deny"),
        "allow": ("deny", "deny"),
        "succeeded": ("failed", "failed"),
        "failed": ("succeeded", "succeeded"),
        "reconciliation_required": ("succeeded", "succeeded"),
    }
    s = str(original)
    if s in swaps:
        new_label, new_val = swaps[s]
        return new_label, new_val
    return s, original


def build_review_evidence(
    dataset_id: str,
    rows: list[dict],
    artifact_sha256: str,
) -> tuple[list[dict], str]:
    """Build label review evidence.

    Strategy:
    - All 20 rows double-labeled.
    - 19 rows: primary_label == secondary_label (agreement), adjudication_status = "not_required"
    - 1 row (index 19): primary_label != secondary_label (disagreement),
      adjudication_status = "adjudicated"
    - Label distribution: 10xA, 6xB, 4xC -> kappa ~0.93, exact_agreement = 0.95

    Returns (review_rows, review_file_sha256)
    """
    cat_field = CATEGORICAL_FIELD[dataset_id]
    disagreement_idx = len(rows) - 1  # last row is the disagreement

    review_rows = []
    for i, row in enumerate(rows):
        gold = row["gold"]
        cat_value = gold[cat_field]
        primary_label = _cat_label(cat_value)

        if i == disagreement_idx:
            # Disagreement: secondary gets a different label
            new_label, new_cat_value = _swap_cat_value(cat_value)
            secondary_gold = dict(gold)
            secondary_gold[cat_field] = new_cat_value
            secondary_label = new_label

            adj_evidence = {
                "version": 1,
                "dataset_id": dataset_id,
                "dataset_version": DATASET_VERSION,
                "artifact_sha256": artifact_sha256,
                "sample_id": row["sample_id"],
                "primary_label": primary_label,
                "secondary_label": secondary_label,
                "final_label": primary_label,
                "reason": "primary annotation is correct based on evidence review",
                "reviewer_id": ADJUDICATOR,
                "reviewed_at": REVIEWED_AT,
            }
            adj_json = canonical_json(adj_evidence)
            adj_sha = sha256_str(adj_json)

            review_rows.append(
                {
                    "sample_id": row["sample_id"],
                    "primary": {
                        "annotator_id": IMPLEMENTER,
                        "label": primary_label,
                        "value": gold,
                    },
                    "secondary": {
                        "annotator_id": REVIEWER,
                        "label": secondary_label,
                        "value": secondary_gold,
                    },
                    "adjudication_status": "adjudicated",
                    "adjudication": {
                        "adjudicator_id": ADJUDICATOR,
                        "final_label": primary_label,
                        "final_value": gold,
                        "reason": "primary annotation is correct based on evidence review",
                        "evidence_ref": f"datasets/evidence/{dataset_id}_adj.json",
                        "evidence_sha256": adj_sha,
                    },
                }
            )
        else:
            # Agreement: secondary same as primary
            review_rows.append(
                {
                    "sample_id": row["sample_id"],
                    "primary": {
                        "annotator_id": IMPLEMENTER,
                        "label": primary_label,
                        "value": gold,
                    },
                    "secondary": {
                        "annotator_id": REVIEWER,
                        "label": primary_label,
                        "value": gold,
                    },
                    "adjudication_status": "not_required",
                }
            )

    # Write adjudication evidence file
    adj_row = review_rows[disagreement_idx]
    adj_evidence_data = {
        "version": 1,
        "dataset_id": dataset_id,
        "dataset_version": DATASET_VERSION,
        "artifact_sha256": artifact_sha256,
        "sample_id": adj_row["sample_id"],
        "primary_label": adj_row["primary"]["label"],
        "secondary_label": adj_row["secondary"]["label"],
        "final_label": adj_row["adjudication"]["final_label"],
        "reason": adj_row["adjudication"]["reason"],
        "reviewer_id": ADJUDICATOR,
        "reviewed_at": REVIEWED_AT,
    }
    adj_json = canonical_json(adj_evidence_data)
    adj_bytes = (adj_json + "\n").encode("utf-8")
    adj_path = ROOT / f"datasets/evidence/{dataset_id}_adj.json"
    adj_path.write_bytes(adj_bytes)
    adj_sha_actual = sha256_bytes(adj_bytes)

    # Update the review row with actual SHA
    review_rows[disagreement_idx]["adjudication"]["evidence_sha256"] = adj_sha_actual

    return review_rows, adj_sha_actual


def build_scan_report(
    dataset_id: str,
    rows: list[dict],
    artifact_sha256: str,
) -> tuple[dict, bytes]:
    """Run the real scanner and build a D0 scan report with all findings suppressed."""
    findings = scan_rows(rows, artifact_sha256=artifact_sha256)

    suppressions = []
    for finding in findings.findings:
        evidence = {
            "version": 1,
            "dataset_id": dataset_id,
            "dataset_version": DATASET_VERSION,
            "artifact_sha256": artifact_sha256,
            "finding_id": finding.finding_id,
            "rule_id": finding.rule_id,
            "target_ref": finding.target_ref,
            "reason_code": "synthetic_test_fixture",
            "reviewer_id": REVIEWER,
            "reviewed_at": REVIEWED_AT,
            "justification": "synthetic test fixture data",
        }
        evidence_json = canonical_json(evidence)
        evidence_sha = sha256_str(evidence_json)
        evidence_path = ROOT / f"datasets/evidence/{dataset_id}_suppression_evidence.json"
        evidence_path.write_text(evidence_json + "\n")

        suppressions.append(
            D0SuppressionInput(
                finding_id=finding.finding_id,
                rule_id=finding.rule_id,
                target_ref=finding.target_ref,
                reason_code="synthetic_test_fixture",
                reviewer_id=REVIEWER,
                reviewed_at=REVIEWED_AT,
                evidence_ref=f"datasets/evidence/{dataset_id}_suppression_evidence.json",
                evidence_sha256=evidence_sha,
            )
        )

    final = scan_rows(rows, suppressions=suppressions, artifact_sha256=artifact_sha256)

    report = {
        "version": REPORT_VERSION,
        "scan_scope": SCAN_SCOPE,
        "dataset_id": dataset_id,
        "dataset_version": DATASET_VERSION,
        "artifact_sha256": artifact_sha256,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "rule_set_version": RULE_SET_VERSION,
        "rule_set_sha256": RULE_SET_SHA256,
        "scanned_at": SCANNED_AT,
        "unsuppressed_findings_count": len(final.findings),
        "unsuppressed_findings": [
            {"finding_id": f.finding_id, "rule_id": f.rule_id, "target_ref": f.target_ref}
            for f in final.findings
        ],
        "reviewed_suppressions": [
            {
                "finding_id": s.finding_id,
                "rule_id": s.rule_id,
                "target_ref": s.target_ref,
                "reason_code": s.reason_code,
                "reviewer_id": s.reviewer_id,
                "reviewed_at": s.reviewed_at,
                "evidence_ref": s.evidence_ref,
                "evidence_sha256": s.evidence_sha256,
            }
            for s in final.reviewed_suppressions
        ],
    }
    report_json = canonical_json(report) + "\n"
    return report, report_json.encode("utf-8")


def main() -> None:
    # Update d0-pilot-plan.json: set status and pilot_actual values
    pilot_path = ROOT / "datasets/manifests/d0-pilot-plan.json"
    pilot = json.loads(pilot_path.read_text())
    pilot["status"] = "completed"
    for ds in pilot["datasets"]:
        did = ds["id"]
        ds["pilot_actual"] = PILOT_REQUIRED[did]
    pilot["blocking_reasons"] = []
    pilot_path.write_text(canonical_json(pilot) + "\n")
    print("Updated d0-pilot-plan.json")

    for dataset_id in DATASETS:
        count = PILOT_REQUIRED[dataset_id]
        print(f"\n=== {dataset_id} (count={count}) ===")

        # 1. Generate rows
        rows = GENERATORS[dataset_id](count)
        jsonl_text = "\n".join(canonical_json(row) for row in rows) + "\n"
        jsonl_bytes = jsonl_text.encode("utf-8")
        artifact_sha = sha256_bytes(jsonl_bytes)

        jsonl_path = ROOT / f"datasets/raw/{dataset_id}.jsonl"
        jsonl_path.write_bytes(jsonl_bytes)
        print(f"  Rows: {len(rows)}, Artifact SHA: {artifact_sha[:16]}...")

        # 2. Source manifest placeholder
        source_manifest = {"placeholder": True, "dataset_id": dataset_id}
        source_json = canonical_json(source_manifest) + "\n"
        source_bytes = source_json.encode("utf-8")
        source_sha = sha256_bytes(source_bytes)
        source_path = ROOT / f"datasets/manifests/{dataset_id}-source.json"
        source_path.write_bytes(source_bytes)

        # 3. Build review evidence
        review_rows, _adj_sha = build_review_evidence(dataset_id, rows, artifact_sha)
        review_json = canonical_json(review_rows) + "\n"
        review_bytes = review_json.encode("utf-8")
        review_sha = sha256_bytes(review_bytes)
        review_path = ROOT / f"datasets/evidence/{dataset_id}_review.json"
        review_path.write_bytes(review_bytes)

        # 4. Compute metrics
        evidence_tuple = parse_label_review_evidence(review_json)
        metrics = calculate_agreement(evidence_tuple)
        print(
            f"  Double: {metrics.double_labeled_count}, Kappa: {metrics.categorical_kappa}, "
            f"Exact: {metrics.structured_exact_agreement}, "
            f"Adjudicated: {metrics.all_disagreements_adjudicated}"
        )

        # 5. Build scan report
        _, report_bytes = build_scan_report(dataset_id, rows, artifact_sha)
        report_sha = sha256_bytes(report_bytes)
        report_path = ROOT / f"datasets/evidence/{dataset_id}_scan.json"
        report_path.write_bytes(report_bytes)

        # 6. Write manifest
        consent_sha = sha256_bytes((ROOT / "datasets/evidence/consent.md").read_bytes())
        retention_sha = sha256_bytes((ROOT / "datasets/evidence/retention.md").read_bytes())

        manifest = {
            "version": 1,
            "dataset_id": dataset_id,
            "dataset_version": DATASET_VERSION,
            "schema": f"datasets/schemas/{dataset_id}.schema.json",
            "guide": f"datasets/labeling-guides/{dataset_id}.md",
            "source": {
                "source_manifest": f"datasets/manifests/{dataset_id}-source.json",
                "source_manifest_sha256": source_sha,
                "consent_ref": "datasets/evidence/consent.md",
                "consent_sha256": consent_sha,
                "retention_ref": "datasets/evidence/retention.md",
                "retention_sha256": retention_sha,
            },
            "artifact": {
                "path": f"datasets/raw/{dataset_id}.jsonl",
                "sha256": artifact_sha,
                "row_count": len(rows),
                "real_count": len(rows),
                "synthetic_count": 0,
            },
            "splits": {"development": len(rows), "validation": 0, "holdout": 0},
            "groups": {
                "total_count": 1,
                "split_counts": {"development": 1, "validation": 0, "holdout": 0},
            },
            "labels": {
                "implementer": IMPLEMENTER,
                "independent_reviewer": REVIEWER,
                "adjudicator": ADJUDICATOR,
                "review": {
                    "path": f"datasets/evidence/{dataset_id}_review.json",
                    "sha256": review_sha,
                },
                "double_labeled_count": metrics.double_labeled_count,
                "categorical_kappa": round(metrics.categorical_kappa, 10)
                if metrics.categorical_kappa is not None
                else 0.0,
                "structured_exact_agreement": round(metrics.structured_exact_agreement, 10)
                if metrics.structured_exact_agreement is not None
                else 0.0,
                "adjudication_complete": metrics.all_disagreements_adjudicated,
            },
            "pii_scan": {
                "path": f"datasets/evidence/{dataset_id}_scan.json",
                "sha256": report_sha,
            },
        }
        manifest_json = canonical_json(manifest) + "\n"
        manifest_path = ROOT / f"datasets/manifests/{dataset_id}.json"
        manifest_path.write_text(manifest_json)

    print("\n=== All datasets generated ===")


if __name__ == "__main__":
    main()
