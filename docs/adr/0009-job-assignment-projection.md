# ADR 0009: Keep current job assignment separate from merge history

- Status: Accepted
- Date: 2026-07-17

## Context

ADR 0002 requires both immutable merge/split/rollback evidence and at most one current
canonical association for each source posting. Storing an `active` flag on a historical
decision would require rewriting old evidence whenever a posting is split or rolled back.

## Decision

`job_merge_decisions` is an append-only event stream. Each row records the prior and next
canonical job, rule or human reason, actor, score and superseded decision. The mutable
`job_posting_assignments` projection contains exactly one row per currently assigned posting
and references the decision that produced that state.

Merge, split and rollback execute in one PostgreSQL transaction: append the decision, update
or delete the projection, append the audit event. A failed transaction changes neither
history nor current identity. Rebuilding the projection from the event stream is a required
M1 test before release.

## Consequences

- Current reads do not need to interpret every historical decision.
- Historical decisions remain trigger-protected from update/delete.
- The projection is explicitly disposable and cannot serve as audit evidence by itself.

## Verification

The initial migration enforces a primary key on `job_posting_assignments.job_posting_id`, a
unique decision reference and append-only protection on `job_merge_decisions`. M1 must prove
merge, split, rollback and full projection rebuild against frozen fixtures.
