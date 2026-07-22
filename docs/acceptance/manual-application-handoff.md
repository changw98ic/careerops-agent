# Manual application handoff acceptance

- Status date: 2026-07-20
- Current status: implemented as an evidence-bound, human-only handoff packet and optional
  user attestation record.
- Release scope: a human can review evidence-bound CareerOps materials and complete
  the final real-world application submission outside CareerOps.

This document defines the current acceptance scope for Manual Real-World Application Handoff. It
does not authorize autonomous application submission and does not qualify `limited_autopilot` or
`expanded_autopilot`.

## Capability boundary

Manual handoff creates an immutable packet from a review-required internal application draft:

`internal draft -> public HTTPS job URL -> same-origin source evidence -> approved materials -> manual handoff packet -> human final submission -> optional user attestation`

The final submission is a human action. CareerOps can prepare and persist the packet, then record
what the user says they submitted. It does not send the application.

## Acceptance matrix

| ID | Requirement | Current evidence | Acceptance status |
| --- | --- | --- | --- |
| MH.1 | The handoff starts from an immutable internal draft only. | `ManualApplicationHandoffBuilder` rejects non-`create_internal_draft` actions, non-`canonical_job` resources, mismatched canonical job ids, and tampered draft payload hashes. | Implemented and unit-tested. |
| MH.2 | The target is a public HTTPS application URL backed by source evidence on the same origin. | URL normalization rejects non-HTTPS URLs, credentials, fragments, invalid hosts, and evidence from an unrelated origin. | Implemented and unit-tested. |
| MH.3 | The packet binds the source draft, candidate, canonical job, job posting, target URL, source evidence, and approved material hashes. | `manual_handoff_payload_hash` derives the packet identity from those fields plus `manual-application-handoff.v1` instructions. The draft itself binds its candidate id; the builder rejects a different candidate. Tests prove the hash changes when evidence or materials change. | Implemented and unit-tested. |
| MH.4 | The packet is explicitly human-only. | Payloads use `mode: human_final_submission` and `provider_execution: disabled`. Instructions tell the user to handle login, MFA, legal declarations, sensitive information, identity materials, payment, and assessments themselves. | Implemented and unit-tested. |
| MH.5 | Persistence records preparation without external execution. | `PostgresManualApplicationHandoffStore.save_handoff` inserts an action intent, payload version, `manual_handoff_prepared` application event, and `manual_application_handoff_prepared` audit event. Repository tests assert no Outbox or provider receipt statement is used. | Implemented with SQL/unit evidence; real PostgreSQL execution remains required. |
| MH.6 | Submission records are user attestations, not provider receipts. | `record_manual_submission` records `manual_submission_recorded` with `source_type: user_attestation`, `provider_receipt_verified: False`, and an audit event with `attestation_only: True`. | Implemented with SQL/unit evidence; real PostgreSQL execution remains required. |
| MH.7 | Retrying the same handoff or attestation is idempotent. | Handoff creation uses the stable handoff idempotency key; manual submission events use a deterministic event id from handoff identity plus user-provided reference and submission time. | Implemented and unit-tested. |
| MH.8 | Manual handoff remains separate from autonomous submission authority. | `create_manual_application_handoff` is an internal action in the default policy rules. The repository imports no Outbox, provider, browser, credential, Playwright, or side-effect-worker execution surfaces. | Implemented and unit-tested. |

## Explicit non-goals

- No provider adapter execution.
- No browser driver, Playwright session, cookie jar, credential entry, ATS login, or form fill.
- No Gmail, email provider, calendar provider, or third-party send.
- No Outbox job that performs an external write.
- No provider receipt verification. A saved receipt/reference is user-provided attestation only.
- No legal attestation, MFA, assessment, identity document, sensitive personal-data disclosure,
  payment, or destination-policy decision by CareerOps.
- No release qualification for `limited_autopilot`, `expanded_autopilot`, or any other
  autonomous submission stage.

## Verification record

The focused verification command is:

```bash
uv run pytest -q \
  tests/unit/test_application_handoff.py \
  tests/unit/test_application_handoff_repository.py \
  tests/unit/test_policy.py
```

Passing this command proves the current in-code handoff contract and SQL construction only. It
does not prove real PostgreSQL execution, a UI workflow, a CLI workflow, provider behavior, or any
real-world submission.
