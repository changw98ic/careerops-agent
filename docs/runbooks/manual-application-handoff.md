# Manual application handoff runbook

## Reader and purpose

This runbook is for the CareerOps operator or release reviewer preparing a real-world application
for a human to submit. It covers only Manual Real-World Application Handoff:

`review-required internal draft -> human review -> human-only handoff packet -> user opens public URL -> user submits -> optional user attestation`

The current implementation is an application and persistence capability. This runbook does not
document a shipped console or CLI submit button.

## Preconditions

- Start from a prepared internal application draft for a canonical job.
- Confirm the draft target contains the canonical public HTTPS job URL.
- Confirm at least one source-evidence URL is on the same HTTPS origin as the target URL.
- Confirm the handoff has at least one approved material reference with a stable SHA-256 hash.
- Use one explicit database transaction when persisting the handoff.
- Do not configure ATS credentials, browser cookies, Gmail credentials, provider tokens, or
  side-effect workers for this path.

## Human handoff flow

1. Review the evidence-bound internal draft, then prepare the handoff with
   `ManualApplicationHandoffBuilder`.
2. Review the packet identity: candidate id, canonical job id, job posting id, target URL,
   source draft payload hash, source evidence, approved material refs, and handoff payload hash.
3. Persist the handoff with `PostgresManualApplicationHandoffStore.save_handoff` inside a caller
   owned transaction.
4. Give the human operator the public target URL and approved materials listed in the packet.
5. The human opens the site, handles login/MFA/legal statements/sensitive information/payment/
   assessments personally, and performs the final submission outside CareerOps.
6. If the human wants a local record, save `ManualSubmissionAttestation` with the
   user-provided receipt/reference and the user-reported submission time.

## Expected records

Handoff preparation creates internal evidence only:

- `action_intents.action_kind = create_manual_application_handoff`
- one `action_payload_versions` row bound to the handoff payload hash;
- one `application_events.event_type = manual_handoff_prepared`;
- one audit event with `event_type = manual_application_handoff_prepared`;
- `mode = human_final_submission`;
- `provider_execution = disabled`.

Manual submission recording creates user attestation only:

- `application_events.event_type = manual_submission_recorded`;
- `source_type = user_attestation`;
- `provider_receipt_verified = False`;
- audit event `manual_application_submission_recorded` with `attestation_only = True`.

## Verification

Run the focused unit evidence before relying on this path:

```bash
uv run pytest -q \
  tests/unit/test_application_handoff.py \
  tests/unit/test_application_handoff_repository.py \
  tests/unit/test_policy.py
```

Success means the handoff packet, idempotent SQL statements, policy classification as an internal
action, and non-import of execution surfaces match the current implementation. It does not mean a
real application was submitted.

## Stop conditions

Stop and leave the item in manual review if any of these are true:

- the draft payload hash no longer matches the draft content;
- the target URL is not public HTTPS;
- evidence is missing or only comes from an unrelated origin;
- approved materials are missing or have duplicate ids/hashes;
- the user must provide credentials, MFA, legal declarations, identity material, sensitive data,
  payment, or assessment responses;
- the operator is expecting CareerOps to send through a provider, browser, Gmail, or Outbox
  worker;
- the request is being used as evidence for `limited_autopilot` or `expanded_autopilot`.

## Non-goals

This runbook does not cover provider adapters, browser automation, Gmail sends, ATS login,
credential storage, destination-policy classification, provider receipt verification, autonomous
retry, or autonomous release qualification. The human final submission is the product boundary.
