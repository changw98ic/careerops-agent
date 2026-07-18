# ADR 0005: Store content by hash and enforce retention by data class

- Status: Accepted
- Date: 2026-07-17

## Context

The MVP must retain enough evidence to replay decisions without accumulating an indefinite
copy of webpages, mail, attachments and model inputs. PostgreSQL, object content and Temporal
state also need a coherent restore boundary.

## Decision

Binary/text content is stored on a local persistent content-addressed volume behind a
`StoragePort`. The canonical key is derived from SHA-256 and never from an untrusted filename.
As refined by ADR 0010, PostgreSQL separates the two identities involved:

- `content_blobs` owns the physical digest, object key, byte size and deletion lifecycle;
- `content_objects` owns one logical resource reference, including media type, classification,
  owner and retention deadline.

Several logical objects may reference one physical blob without sharing an owner or TTL.
Writes enforce a size limit, compute and optionally verify the digest, flush a private
temporary file, atomically rename it to the canonical path and avoid following symlinks.

Default retention is:

| Data class | Default |
| --- | --- |
| normalized jobs and job history | 24 months |
| raw webpage body | 30 days |
| raw HTTP headers | 14 days |
| public recruiting contact | 12 months after job closure |
| recruiting email body | 12 months, user-configurable downward |
| non-recruiting email body | never persisted |
| raw model prompt/response | disabled by default; temporary debug capture max 24 hours |
| model metadata/hashes | 90 days |
| audit events and minimal decision evidence | 24 months |
| OAuth credential ciphertext | until revoke/delete |
| rejected/quarantined attachment bytes | maximum 24 hours |
| accepted safe extracted text | follows its owning email/application and may be shortened by user policy |
| temporary browser data | not applicable in MVP; any future data is deleted at task end |

Before raw content deletion, the purge workflow must verify that every retained decision has
the minimum evidence tuple: source URL/provider ID, captured time, sanitized span, content
hash and extractor/rule/model version. Evidence snippets are bounded and cannot contain
secrets merely to preserve replayability.

Physical deletion is fenced across the PostgreSQL/filesystem boundary. The database first
commits `deleting` state, a bounded lease and a fresh opaque token. The worker then performs an
idempotent, digest-and-size-verified byte deletion and finalizes only with the matching token.
`ALREADY_MISSING` is persisted as an explicit outcome. A stale lease may be reclaimed with a
new token; an old worker cannot finalize it.

Backups are required to cover the PostgreSQL business schema, content volume, configuration
identifiers and encrypted credential references in one reconcilable manifest. Temporal uses a
separate schema/credential and consistent backup point. The master encryption key is restored
through the external secret mechanism, not bundled in backup media.

## Current M0 implementation boundary

The implemented write path is deliberately smaller than the complete staging design in ADR
0010:

1. local CAS writes and verifies canonical bytes;
2. one catalog transaction registers or reuses the active blob and creates the logical object;
3. a failed registration leaves the bytes in place rather than blindly deleting a digest that
   may already be shared;
4. after a grace cutoff, the orphan reconciler enumerates canonical files with
   descriptor-relative no-follow I/O, verifies digest and size, and commits a per-digest
   `deleting` tombstone before unlink/finalize. Expired orphan claims can be reclaimed.

This is not a PostgreSQL/filesystem atomic transaction and is not the full
`staging -> finalize -> reconcile` protocol. The adapter rejects an expired descriptor, but a
database-state-aware read service covering every logical/blob lifecycle state is not yet
wired. Retention can mark a logical object expired; no runtime role can mark it retired until
the replay-safe/no-evidence-required verifier exists. Consequently the reference checks fail
closed and end-to-end physical purge is not yet release-complete.

Durable missing-byte alerts, the complete crash-boundary matrix, mutually consistent restore
and the documented RPO/RTO have not been demonstrated.

## Consequences

- Content reference integrity and purge tests are required from M0, not deferred to M7.
- User export omits decrypted tokens and quarantined bytes.
- The target RPO is at most five minutes and single-node RTO is at most thirty minutes; M7
  must prove both on a clean machine.
- Accepted retention durations are policy limits, not evidence that every lifecycle worker is
  implemented.

## Verification

Current automated coverage exercises bounded/hash-checked writes, traversal and symlink
rejection, descriptor expiry, shared-blob preservation, registration rollback followed by
grace-bounded orphan cleanup, tombstone reclaim and fenced/idempotent deletion. The real
PostgreSQL suite is separate from `make verify` and must be run with `make verify-db`.

Still required before release are state-aware read integration, a replay-safe retirement
transition, the complete staging/crash matrix, durable alert evidence, backup/restore with
missing or corrupt objects, and clean-machine RPO/RTO proof.
