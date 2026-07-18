# ADR 0010: Separate physical blobs from logical content lifecycles

- Status: Accepted
- Date: 2026-07-17

## Context

ADR 0005 requires content-addressed storage, per-class retention, replay-safe evidence and
two-phase deletion. One SHA-256-addressed byte sequence can be referenced by resources with
different owners, classifications and retention deadlines. Treating one digest as one owner
and one TTL either deletes shared bytes too early or retains every owner's data for the
longest-lived reference.

PostgreSQL and the local content volume also do not share a transaction. Both ingest and
deletion therefore need explicit, recoverable boundaries. This ADR refines ADR 0005 and
separates the accepted complete lifecycle from the smaller M0 slice currently implemented.

## Decision

### Physical blobs and logical objects

The storage model has two distinct identities:

- `content_blobs` represents one physical byte sequence. It owns the SHA-256 digest, canonical
  object key, byte size and physical deletion state. Digest and canonical key are unique.
- `content_objects` represents one logical use of a blob. It owns media type, classification,
  resource owner, retention deadline, expiry and retirement.

Several logical objects may reference one blob. Ownership, authorization and retention are
evaluated against the logical object; knowledge of a digest or object key is not sufficient
authorization. Deduplication must never combine or lengthen logical retention policies.

An object is expired when its deadline is reached or an authorized earlier deletion takes
effect. It may be retired only after a verifier records that its required long-lived evidence
is replay-safe, or that no replay evidence is required. Expiry and retirement are idempotent
logical transitions and do not themselves delete shared bytes.

### Physical deletion

A blob is eligible for deletion only when it has at least one logical reference, every logical
reference is retired, and every reference used by retained decisions has a replay-safe
evidence record. Eligibility is rechecked while the candidate blob is locked. Reference-guard
triggers prevent a new logical/evidence/job-version reference from attaching once the blob is
no longer active.

Deletion uses committed state and an opaque fence:

1. A database transaction changes the blob from `active` to `deleting`, records a bounded
   lease owner/until and a **fresh opaque fencing token** (currently a UUID), then commits
   before filesystem I/O.
2. The worker verifies the canonical key, digest and size and requests idempotent byte
   deletion.
3. A separate transaction finalizes `deleted` only when the supplied fencing token still
   matches, and persists either `DELETED` or `ALREADY_MISSING`.

After lease expiry, another worker may reclaim the row with a different fresh token and a
later lease deadline. The previous token cannot finalize. `ALREADY_MISSING` is not inferred
from database state; it is an explicit filesystem result. Persisting a durable integrity alert
for that result remains required but is not implemented in M0.

### Recoverable writes

The complete target protocol is `staging -> finalize -> reconcile`: private temporary bytes,
non-readable staging metadata, canonical byte finalization, digest/size revalidation and a
ready transition. A reconciler must eventually cover each crash boundary and must fail closed
on missing or corrupt bytes.

The current M0 implementation is a simpler, bounded precursor:

1. CAS writes a private temporary file and atomically places verified bytes at the canonical
   key.
2. A catalog transaction reuses or inserts an active `content_blobs` row and creates the
   logical `content_objects` row. Deleting/deleted blobs reject new references.
3. If registration rolls back, the writer does not delete the canonical file because another
   committed owner may share the digest.
4. After a configured grace cutoff, the orphan reconciler performs bounded,
   descriptor-relative enumeration. It fails closed on a symlink, non-canonical path, size
   violation, digest mismatch or file mutation during inspection.
5. Registration and orphan claim serialize on the same per-digest advisory transaction lock.
   Cleanup commits a narrowly identified `write-orphan-reconciler` tombstone and lease before
   unlinking, then finalizes with the matching token. An expired committed claim is
   reclaimable; a deleted tombstone is not mistaken for a live registration.

This protocol bounds known write-before-register orphans, but it is not the complete staging
state machine and does not make PostgreSQL plus filesystem I/O atomic.

## Current M0 control boundary

The initial business schema contains 22 business tables, including the blob/object split. With
the later console-authentication migration the current M0 total is 25 tables. Eight tables are
append-only. Three reference guards, monotonic logical-lifecycle checks, a deferred
active-blob/reference check and the content-blob lifecycle trigger protect deletion; the
Outbox has a separate lifecycle trigger.

The retention role can mark `expired_at`, claim/finalize blob deletion and insert only the
columns required for an orphan tombstone. No runtime role can write `retired_at`. This is
intentional fail-closed behavior until the replay-safe/no-evidence-required retirement
verifier is implemented, and it means the ordinary end-to-end purge path is not yet complete.

The local adapter can reject a `StoredContent` descriptor whose retention deadline has passed,
but it does not query logical or blob state. A state-aware read service for expired, retired,
deleting and deleted records remains pending. The full staging state model, durable attempt and
alert records, metrics/alert wiring and full crash matrix also remain pending.

## Consequences

- Physical deduplication remains global while ownership, access and retention remain logical.
- Filesystem I/O stays outside row-lock transactions; correctness relies on committed state,
  idempotency, advisory/reference locking and fresh fencing tokens.
- Backup manifests must cover both tables and the content volume at a reconcilable point.
- ADR 0005 remains authoritative for durations and minimum evidence; this ADR is authoritative
  for blob/reference identity and lifecycle boundaries.
- Accepted design is not the same as release qualification.

## Verification

Current tests cover shared bytes with independent logical references, active-blob reference
guards, fail-closed lifecycle transitions, stale/wrong token rejection, idempotent deletion,
registration rollback without blind deletion, grace cutoff, verified enumeration, concurrent
register-versus-claim ordering, committed orphan-claim reclaim and deleted-tombstone cleanup.
Real PostgreSQL checks are run separately with `make verify-db`.

M0 still requires the runtime retirement verifier and state-aware read integration. M7 must
add fault injection for the full staging/finalize/delete crash matrix and prove clean-machine
restore, missing/corrupt-object detection, purge across backup/restore, no dangling live
reference and the documented RPO/RTO. Release evidence must link the migration version, test
results and any reconciliation metrics/alerts that actually exist at that milestone.
