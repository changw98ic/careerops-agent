# D0 pilot intake runbook

## Purpose and boundary

Use this runbook to intake lawful, real D0 pilot data for the current
`d0_pilot_engineering_consistency` contract. It does not qualify a release,
complete product evaluation, or enable Auto-send.

The current pilot plan is `planned` with `0/286` real pilot rows. The first
operational tranche is:

| Dataset | Pilot rows | Source contract |
| --- | ---: | --- |
| `discovery_parser` | 35 | `datasets/schemas/discovery_parser.schema.json` and `datasets/labeling-guides/discovery_parser.md` |
| `dedup` | 60 | `datasets/schemas/dedup.schema.json` and `datasets/labeling-guides/dedup.md` |

All nine D0 pilot datasets total 286 required real rows:

| Dataset | Pilot rows | Double-label rule |
| --- | ---: | --- |
| `discovery_parser` | 35 | At least 20% random double-label |
| `dedup` | 60 | At least 20% random double-label |
| `remote` | 40 | 100% double-label |
| `evidence_match` | 25 | At least 20% random double-label |
| `contact` | 25 | At least 20% random double-label |
| `email` | 40 | 100% double-label |
| `policy_injection` | 25 | 100% double-label |
| `calendar` | 24 | 100% double-label |
| `reconciliation` | 12 | 100% double-label |

## Roles

- Data curator: collects lawful source material, records source/consent/retention
  evidence, de-identifies rows, assigns stable `group_id` values, and prepares
  manifests without using model predictions.
- Independent reviewer: performs required second labels and reviews class
  balance, semantic leakage, labeling quality, and technical-scan suppressions.
- Adjudicator: resolves disagreements from anonymized sample IDs and evidence.
  The adjudicator must be separate from the curator and reviewer in the current
  full gate.
- Holdout custodian: controls sealed holdout access outside routine
  implementation. Current repository checks do not implement custody, access
  logs, signatures, or a cryptographic seal; collect that evidence separately.

Manifest identity strings are declarations only. They do not authenticate a
person, prove role independence, or replace signed human attestation.

## Storage boundary

Keep raw email, PII, private candidate material, credentials, and unredacted
source captures out of Git. The public repository may contain only redacted rows,
repository-local evidence manifests, hashes, scan reports, label-review evidence,
and reviewed suppression evidence that is safe to publish.

Source, consent, and retention hashes prove only that referenced files have not
changed relative to the manifest. They do not prove lawful source, consent,
privacy clearance, de-identification quality, or holdout custody.

Synthetic or paraphrased data must never be substituted for required real pilot
rows. If synthetic rows exist, they must be marked `synthetic=true` and remain in
the `development` split only. They cannot satisfy `pilot_required`.

## Operator workflow

Inspect the supported CLI surface before changing an intake script:

```bash
uv run careerops-d0 --root . --help
uv run careerops-d0 --root . status --help
uv run careerops-d0 --root . scaffold --help
uv run careerops-d0 --root . validate --help
```

### 1. Status

Use `status` to derive the current intake state from the plan and any referenced
evidence:

```bash
uv run careerops-d0 --root . status --json
```

Expected current state: `plan_status_declared` is `planned`,
`pilot_rows_required` is `286`, derived actual rows are `0`, no manifest files or
scan reports are present, `release_qualification_authorized` is `false`, and
reviewer/adjudicator roles are unassigned.

For each dataset, `datasets[].scan.binding_verified` is a lightweight
digest/envelope/dataset/artifact binding signal for Status output. It is `true`
only when `status` can read the manifest-referenced scan report, verify the
report file digest, and match the report envelope to the expected dataset ID,
dataset version, artifact SHA-256, scanner tool, rule-set metadata, report
version, and scan scope. It does not replay the technical scan, re-evaluate
suppressions, prove zero findings by itself, or qualify a release. Use
`validate --json` or `make verify-m1-full` for the full D0 engineering evidence
gate.

`datasets[].artifact.digest_verified` similarly means that the currently read
artifact bytes match the SHA-256 declared by its manifest.
`datasets[].manifest.identity_verified` requires that the manifest names the
same dataset as its pilot-plan entry and supplies a non-empty dataset version.
The raw `pilot_actual_derived` count remains parsing telemetry; only
`counts.pilot_rows_derived_from_verified_artifacts` and
`counts.datasets_satisfied_by_derived_rows` require both bindings. The
top-level `roles.top_level_role_values_are_distinct` signal compares non-empty
role declaration strings only. It does not authenticate people or establish
real-world independence.

`operator_guidance` is a bounded fixed-code checklist derived from the same
Status telemetry. It never copies pilot-plan `blocking_reasons`, role values,
paths, sample data, or scan findings. Its `status_is_authoritative_gate` value
is always `false`; `authoritative_gate: d0_validate_full` means the operator
must still run `validate --json` or `make verify-m1-full`. In
`operator_guidance.remaining`, `pilot_rows` counts only rows from
identity-verified manifests with digest-verified artifacts; `manifests` and
`scan_reports` count missing files, while the fixed blocker codes distinguish
identity, digest, binding, and finding problems.

### 2. Scaffold

Use `scaffold` only to create explicit incomplete skeleton files. Skeletons are
not evidence and are not referenced by the pilot plan:

```bash
uv run careerops-d0 --root . scaffold --dataset discovery_parser --dataset dedup --json
```

The command creates files under `datasets/d0-intake/<dataset>/` and will not
overwrite existing files unless `--force` is passed. `--force` overwrites only
exact, untouched skeletons generated by the current CLI. It refuses edited or
merely marker-bearing files so operator work cannot be discarded accidentally.

Prepare each dataset version against these contracts:

1. Read the dataset schema and labeling guide before collecting rows.
2. Collect only lawful source material and record source, consent, retention,
   owner, TTL, and deletion evidence in repository-safe references.
3. Create a UTF-8 artifact as a JSON object, JSON array, or JSONL rows matching
   the dataset schema. Each row needs `sample_id`, `group_id`, `split`, and
   `synthetic`.
4. Split rows group-aware across `development`, `validation`, and `holdout`.
   No `group_id` or exact frozen provenance key may cross splits.
5. Create label-review evidence matching
   `datasets/schemas/d0_label_review.schema.json`.
6. Produce a v2 technical-pattern scan report matching
   `datasets/schemas/d0_scan_report.schema.json`.
7. Create a per-dataset evidence manifest matching
   `datasets/schemas/d0_dataset_manifest.schema.json`.
8. Point `datasets/manifests/d0-pilot-plan.json` at the per-dataset manifest
   only when artifact, source/consent/retention references, label review, scan
   report, and hashes are all present.

Do not commit placeholder manifests that imply completed evidence. Do not change
`release_qualification_allowed` to `true`.

### 3. Validate

Use only the implemented validation commands:

```bash
uv run careerops-d0 --root . validate --contracts-only --json
uv run careerops-d0 --root . validate --json
make verify-m1-contracts
make verify-m1-full
```

`validate --contracts-only` and `make verify-m1-contracts` validate static ADR,
metric, schema, and guide contracts. `validate` without `--contracts-only` and
`make verify-m1-full` run the full D0 engineering evidence gate and must fail
until all real pilot evidence is complete.

### 4. M1 implementation handoff

Do not begin M1 automatic-crawl implementation from `status` output or a
contracts-only pass. The handoff requires a frozen, independently reviewed D0
pilot and a zero-exit result from `uv run careerops-d0 --root . validate --json`
or `make verify-m1-full`. Before opening M1 work, the responsible humans must
also complete the separate source-lawfulness, consent, de-identification, role
independence, and holdout-custody checks described in this runbook.

That handoff permits M1's safe-HTTP and source-adapter implementation work; it
does not enable recurring crawling, Release Qualification, or Auto-send. Those
remain subject to M1's own crawl-safety exit gate and the separate M7 release
evidence.

## Evidence chain

For each dataset version, preserve this chain:

| Evidence | Required binding |
| --- | --- |
| Dataset artifact | Repository-local path, SHA-256, row count, real count, synthetic count, split counts, group counts |
| Evidence manifest | Dataset ID/version, schema, guide, artifact hash, source/consent/retention references and hashes |
| Label review | Same sample set as the artifact, primary and secondary actor labels, final adjudication when required |
| Technical scan | Dataset ID/version, artifact hash, fixed tool/rule-set metadata, zero unsuppressed findings |
| Suppression evidence | Independent reviewer, reproduced finding binding, repository-local evidence hash, justification |

The verifier recomputes hashes, derived counts, agreement metrics, exact
group/provenance leakage, label-review bindings, and technical scan output. Human
review must still cover authenticity, legal use, de-identification sufficiency,
class balance, semantic leakage, and suppression correctness.

## Label thresholds

- Safety-critical datasets (`remote`, `email`, `policy_injection`, `calendar`,
  `reconciliation`) require 100% double-label coverage at current gate
  granularity.
- Other datasets (`discovery_parser`, `dedup`, `evidence_match`, `contact`)
  require at least 20% double-label coverage.
- Categorical labels require Cohen's kappa `>= 0.80`.
- Structured and time labels require exact agreement `>= 0.95`.
- Disagreements that require adjudication must include final label/value,
  reason, adjudicator, evidence reference, and evidence hash.

## Stop rules

Stop intake or expansion and fix the source evidence, guide, or labels before
continuing when any of these occur:

- source legality, consent, retention, or de-identification is uncertain;
- raw PII, private material, credentials, or unredacted source content would enter
  the public repository;
- synthetic rows are proposed as substitutes for real pilot rows;
- a blocked crawl/terms source is included;
- the same group or exact frozen provenance key crosses splits;
- double-label coverage is below the dataset rule;
- Cohen's kappa is below `0.80` or structured exact agreement is below `0.95`;
- adjudication is incomplete or performed by a non-independent actor;
- the technical scan has any unsuppressed finding;
- `release_qualification_allowed` is missing or anything other than `false`;
- any process change would auto-enable release, Release Qualification, or
  Auto-send.

Current M-1 evidence cannot authorize release, Release Qualification, or
Auto-send. Auto-send remains disabled until separate M7 human, legal, privacy,
custody, security, and product release evidence exists.
