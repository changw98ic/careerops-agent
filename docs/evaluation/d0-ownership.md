# D0 ownership and pilot protocol

- Status: engineering contracts and deterministic evidence checks ready; real pilot evidence pending
- Data curator role: repository owner
- Independent reviewer role: a person who did not implement the evaluated capability
- Adjudicator: independent reviewer for safety-critical labels; repository owner for non-safety labels after disagreement review
- Current full-gate evidence: `0/286` real pilot rows; no real pilot is complete

## Evidence boundary

M-1 verifier output is scoped to `d0_pilot_engineering_consistency`.
The engineering verifier establishes internal consistency of repository evidence:
strict schema conformance, repository-local path containment, referenced-file
hashes, derived row/split/group counts, bounded exact-provenance leakage checks,
label-review sample/gold binding, derived agreement metrics, and reproducible
technical-pattern scanning.

It does not establish sample authenticity, lawful source, valid consent,
comprehensive de-identification, reviewer identity, legal/privacy clearance, or
sealed-holdout custody. SHA-256 values detect drift relative to a reviewed
manifest; they do not authenticate a person, establish a trusted timestamp, or
make an artifact immutable. Those claims require separate human, legal, and
operational attestations.

## Responsibilities

The data curator records source/consent/retention evidence, collects and
de-identifies samples, assigns stable group IDs, and prepares manifests without
viewing model predictions. The independent reviewer performs second labels and
checks substantive label quality, class balance, semantic/paraphrase leakage,
and technical-scan suppressions. The capability implementer may use
development/validation data but cannot approve the holdout they are optimizing
against.

Identity strings in a manifest are declarations, not authenticated identities or
cryptographic signatures. Release qualification still requires the responsible
human to verify role independence and sign or otherwise authenticate the
attestation outside the current engineering check.

At the current gate granularity, `safety_critical` is dataset-level. Every pilot
row in Remote, Email, Policy/Injection, Calendar, and Reconciliation is therefore
100% double-labeled. Discovery/Parser, Dedup, Evidence Match, and Contact require
at least 20% double-label coverage. Categorical agreement requires Cohen's kappa
>= 0.80; structured/time labels require exact agreement >= 0.95. Narrowing the
100% rule to selected hard-false/high-risk/conflict rows would first require a
row-level safety contract and verifier support.

## Pilot protocol

For every dataset, the pilot target is 10% of its minimum final size and follows
the final row schema. It tests acquisition, de-identification, grouping, label
clarity, agreement, and manifest tooling; pilot results are never a final
product-quality claim.

Synthetic/adversarial rows may exercise schemas and fault paths only when
`synthetic=true` and `split=development`. The full verifier enforces that
restriction. Rows declared `synthetic=false` are eligible for the derived pilot
count, but that flag is not authenticity evidence: source review and human
attestation must establish that the samples are genuinely real and lawfully
usable. Synthetic data never replaces real redacted Email, Remote, or Contact
evidence.

A pilot is qualified only when its artifact and manifest provide the required
real-row count, valid source/consent/retention references, split/group evidence,
label-review evidence, derived agreement, completed disagreement adjudication,
and a reproducible technical scan with no unsuppressed findings. Actor-bound
label and final-adjudication structure, role binding, and referenced-file hash
checks are implemented as engineering structure only; this is not a claim that
any real actor, label, or adjudication evidence already exists or that a human
identity is authenticated.

## Implemented engineering checks

- Dataset, evidence-manifest, label-review, and technical-scan JSON contracts are
  strict and fail closed on unexpected fields or invalid evidence.
- Artifact, manifest reference, label-review, source/consent/retention, scan, and
  reviewed-suppression evidence hashes are recomputed from repository-local
  files.
- Row, real/synthetic, split, and group counts are derived from the artifact;
  declared counts cannot override them.
- The leakage check rejects a shared `group_id` and exact cross-split collisions
  for the frozen provenance-key set. It does not detect semantic paraphrases or
  prove that a curator chose the correct groups.
- Label agreement is derived from review rows rather than trusted from manifest
  metric declarations. Primary/secondary actors must be distinct and bound to
  manifest roles; adjudicated disagreements require final label/value, reason,
  adjudicator, and repository-local evidence whose hash is recomputed.
- The v2 technical scanner is rerun over the artifact rows; the overall Gate
  separately requires those rows to pass their dataset schema. Findings use
  opaque locators and identifiers bound to the artifact, matched content, rule, and
  occurrence; report findings and reviewed suppressions must match the reproduced
  result exactly.

## Sealing and access

The current M-1 gate requires repository-local, de-identified pilot artifacts
and verifies their digests. It does not implement controlled external retrieval,
holdout custody enforcement, access logs, manifest signing, or a cryptographic
seal. Those remain target-state requirements for sealed-holdout qualification.
Until that custody evidence exists, a hash check must not be described as proof
that a holdout was hidden from routine implementation access.

Evaluators should ultimately return aggregates and bounded opaque error IDs.
Label corrections require an issue, adjudication, and a new dataset version and
hash; they must not overwrite a frozen version to preserve a passing score.

## Current M-1 status

Schemas, guides, role definitions, the pilot plan, agreement/leakage tooling,
and the v2 technical-scan path are engineering deliverables. The current plan is
still `planned`, all nine per-dataset manifests are absent, reviewer/adjudicator
roles are unassigned, and the full gate reports `0/286` real pilot rows.

The only M-1 verifier output scope is `d0_pilot_engineering_consistency`.
`release_qualification_allowed` must remain strict `false`; any other value
fails the full gate. M-1 cannot authorize Release Qualification, release, or
Auto-send.

`scripts/verify_m1.py --contracts-only` may pass in this state because it checks
static contracts only. The default full M-1 gate must continue to fail. Neither
result may be described as completion of real source collection, double-labeling,
adjudication, legal review, de-identification review, or holdout sealing.

## Evidence manifest contract

The full verifier does not trust `pilot_actual` or
`release_qualification_allowed` as completion proof. For each dataset,
`datasets/manifests/d0-pilot-plan.json` must point to a repository-local evidence
manifest matching `datasets/schemas/d0_dataset_manifest.schema.json`.

Each dataset manifest must bind:

- the dataset ID/version, schema path, and labeling-guide path;
- source, consent, and retention references plus the SHA-256 of each referenced
  artifact; these hashes prove integrity relative to the manifest, not the truth
  or legal sufficiency of the referenced statements;
- an artifact path plus SHA-256, with row/real/synthetic/split/group counts that
  match the artifact content;
- implementer, independent-reviewer, and adjudicator declarations, with reviewer
  and adjudicator separated from the implementer;
- a hashed label-review artifact matching
  `datasets/schemas/d0_label_review.schema.json`, with the same sample set as the
  dataset artifact and with gold/actor/adjudication bindings required by the
  active contract;
- double-label coverage, Cohen's kappa, structured exact agreement, and
  adjudication completeness derived from the review artifact; manifest metric
  fields are consistency declarations and cannot override derived values;
- a v2 technical-pattern scan report path plus SHA-256.

The v2 scan report must bind the same dataset version and artifact hash, the
fixed tool and rule-set versions/hashes, and zero unsuppressed findings after the
verifier reproduces the scan from the artifact rows. Reviewed suppressions must
bind the reproduced finding and a repository-local evidence record; the
independent reviewer must be distinct from the implementer.

A passing v2 technical scan means only that the frozen deterministic rules found
no unsuppressed technical-pattern match. It is not proof that the artifact is
PII-free, comprehensively de-identified, lawfully sourced, consented, or
privacy-compliant. Public recruiting contacts and documented technical false
positives remain human-reviewed exceptions, not legal clearance.

For M-1, `release_qualification_allowed` is required to be `false`; the verifier
fails any missing or non-false value. No repo-local boolean, path, or hash can
substitute for M7's separate trusted human, legal, privacy, custody, security,
and product release evidence.
