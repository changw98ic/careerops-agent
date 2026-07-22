# Release qualification control-plane runbook

## Reader and purpose

Use this runbook when preparing or reviewing a rollout from synthetic sandbox evidence toward
`shadow` or `review_required` operation. It documents the G006 control-plane contract; it does
not qualify any real ATS, browser, Gmail, credential, provider, or employer channel.

The current permitted execution boundary remains the deterministic no-network `.test`
synthetic provider documented in
[bounded-autopilot synthetic sandbox runbook](bounded-autopilot-sandbox.md).

## Lifecycle

Every release qualification is scoped to an exact capability, action, rollout mode, provider
adapter, configuration hash, policy hash, fixture/evidence manifest, code revision, migration
state, and runtime credential boundary. A reviewer must be able to reconstruct these facts from
append-only evidence without trusting mutable model output.

Allowed states:

| State | Meaning | Write authority |
| --- | --- | --- |
| `draft` | A runner assembled a candidate evidence package. | Runner/evidence collector. |
| `evaluating` | Automated checks and deterministic fixture runs are collecting evidence. | Runner/evidence collector only. |
| `pending_independent_review` | Reviewer bound the complete immutable evidence-ID set; later evidence is rejected. | Independent reviewer; cannot self-review. |
| `qualified` | Independent reviewer and human operator approved the exact scoped capability. | Reviewer decision plus operator approval. |
| `rejected` | Evidence failed or scope was not acceptable. | Independent reviewer or operator. |
| `expired` | Qualification aged out and cannot authorize new work. | System or operator. |
| `revoked` | Operator removed authority before expiry. | Operator only. |

Separation-of-duty rules:

- the runner/evidence collector can create evidence but cannot review or make the final
  qualification decision;
- the reviewer must bind the complete evidence UUID set, review freezes that set, and the human
  operator's final decision must reuse exactly that reviewed set;
- a model output, generic user approval, previous approval, or synthetic qualification cannot
  qualify a real provider;
- state changes are append-only decisions; corrections create another decision or another
  qualification version;
- `limited_autopilot` and `expanded_autopilot` remain blocked until all acceptance evidence in
  [bounded-autopilot acceptance](../acceptance/bounded-autopilot.md) passes for the exact
  adapter and channel.

## Enabled and excluded channels

Enabled now:

- `.test` synthetic sandbox fixture only;
- no-network synthetic provider only;
- internal synthetic shadow/review measurement records backed by the G006 verification record.

Excluded now:

- real ATS adapters, including Greenhouse, Lever, Ashby, Workday, LinkedIn Easy Apply, and
  employer-hosted custom forms;
- browser automation against real sites;
- Gmail read/write or send-as-submission flows;
- credential entry, cookie reuse, identity-document upload, payment, assessments, interactive
  challenges, or legal attestation;
- any provider write without explicit user authorization for the exact campaign, exact channel,
  exact adapter, exact prepared payload, and exact rollout mode.

## Deterministic shadow/review evaluation

G006 requires a deterministic 50-case synthetic evaluation before claiming operational
measurement readiness:

- at least 50 fixture cases split across would-allow, hard-stop, ambiguous-policy,
  duplicate-like, stale-evidence, kill-switch, revoked-grant, expired-grant, payload-drift,
  and cap-boundary scenarios;
- `shadow` cases record what the agent would have done and prove zero provider writes;
- `review_required` cases require a reviewer decision before any write-capable path is even
  considered;
- every case binds source evidence, policy decision, hard-stop result, rollout stage,
  fixture/provider identity, payload hash, release qualification version, and final reviewer
  outcome where applicable;
- metrics distinguish false allow, false stop, reviewer override, ambiguity, skipped provider
  write, and kill-switch stop.

This is synthetic measurement evidence only. It does not count as real-site permission,
real receipt handling, or production provider qualification.

Run the committed evaluator from the repository root:

```bash
uv run python scripts/evaluate_bounded_autopilot_rollout.py
```

The verified manifest expands 10 explicit scenario templates to 50 observations. Its current
SHA-256 is `43d63315f42760bc65224c37c851e987cfcc666af88b852386c8ea2afe0f99a3`.
The evaluator rejects a manifest unless it includes the mandatory `would_allow`, `hard_stop`,
`ambiguous_policy`, `duplicate_like`, `stale_evidence`, `kill_switch`, `revoked_grant`,
`expired_grant`, `payload_drift`, and `cap_boundary` categories.
The report must retain `external_provider_calls=0`,
`supports_real_provider_qualification=false`, and `no_autonomous_writes=true`.

## Fault matrix

The control plane must fail closed for these drills:

| Fault | Expected result |
| --- | --- |
| Before provider call failure | No provider attempt, no receipt, review/recovery outcome. |
| After call before commit failure | Ambiguous state; no automatic retry until reconciliation proves safety. |
| Timeout with possible success | Reconciliation required before retry. |
| Duplicate receipt | Single canonical receipt or manual reconciliation; no duplicate application claim. |
| Missing receipt | Review/recovery outcome; no provider success claim. |
| Active global kill switch | Blocks every new eligible action. |
| Active campaign kill switch | Blocks matching campaign only. |
| Active provider kill switch | Blocks matching provider only. |
| Revoked or expired grant | Blocks before reservation/outbox eligibility. |
| Final-slot concurrency race | Exactly one eligible reservation/outbox item may consume the final cap slot. |

## Audit drill-down

G006 implementation must expose an audit drill-down that reconstructs:

1. campaign and grant version;
2. target evidence and source provenance;
3. policy outcome and hard-stop classification;
4. prepared payload hash and approved material hashes;
5. rollout stage and release qualification version;
6. reviewer decision or synthetic measurement result;
7. outbox, side-effect attempt, receipt, reconciliation, or proof of zero write;
8. latest global/campaign/provider kill-switch state as of the execution authorization or
   reservation timestamp, including the source event ID and decision anchor time.

Verified read-only operator surfaces:

```bash
careerops-release-evidence show --qualification-id "$QUALIFICATION_ID" --json
careerops-release-evidence trace-intent --intent-id "$INTENT_ID" --json
```

The CLI composes the `careerops_readonly` database role. Qualification output derives effective
lifecycle state from ordered append-only decisions and always reports
`external_write_authorized=false`; it cannot publish outbox work or call a provider.

## Rollback gates

Any of the following must stop promotion or roll back a candidate qualification:

- one false allow against a hard-stop category;
- one autonomous provider write in `shadow`;
- one provider write in `review_required` without exact per-intent human approval;
- missing receipt/reconciliation behavior for a write-capable provider;
- qualification evidence produced and approved by the same actor;
- stale configuration, policy, migration, fixture manifest, credential boundary, or adapter
  version;
- failed global, campaign, or provider kill-switch drill;
- failed final-slot concurrency drill;
- missing explicit enabled/excluded channel list.

Rollback action is conservative: revoke or expire the qualification, activate the relevant kill
switch where needed, leave affected intents in review/recovery, and collect a new evidence
package before retrying promotion.

## Mandatory blockers

The following remain blockers for real-channel operation:

- no Gmail OAuth credential, Gmail read sync, or Gmail send adapter is configured in the current
  G006 scope;
- no real ATS adapter has destination-specific permission evidence or receipt/reconciliation
  proof;
- no browser automation path is qualified for real-site submission;
- no `limited_autopilot` or `expanded_autopilot` rollout has enough real review-required sample
  evidence;
- synthetic `.test` evidence cannot be promoted to real employer submission authority.
