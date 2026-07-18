# ADR 0002: Separate canonical jobs from source postings

- Status: Accepted
- Date: 2026-07-17

## Context

A single `NormalizedJob(source_id, external_id)` cannot represent the same logical job appearing on multiple official sources, independent source closure, reversible dedup or historical evidence.

## Decision

The domain uses these aggregates:

- `companies`: verified organization identity and official domains.
- `job_sources`: one official ATS/site feed, unique on `(company_id, source_type, source_identifier)`.
- `job_postings`: one source occurrence, unique on `(source_id, external_id)`; stores canonical URL, source state and timestamps.
- `job_posting_versions`: immutable captured content, unique on `(job_posting_id, content_hash)`; references raw snapshot and parser version.
- `canonical_jobs`: the user-visible logical job, canonicalized title/company and aggregate state.
- `job_merge_decisions`: append-only merge/split/rollback evidence, score, actor and algorithm version. A posting has at most one active canonical association.
- `job_aliases`: normalized title, location and URL aliases used for deterministic matching.

Dedup runs in decreasing authority: exact source identity → canonical URL → verified company/title/location keys → stable fingerprint → semantic candidate proposal. Semantic similarity alone never performs an automatic merge.

A canonical job is active while at least one trusted posting is active. A source disappearing once does not close a posting; adapter-specific consecutive observations and explicit source status decide closure. Reappearance creates a new immutable version and a reopen event rather than erasing history.

All positive decisions retain source URL, captured time, content hash, evidence span and rule/parser/model version. Raw content can expire under ADR 0005, but the minimal evidence record cannot become dangling.

## Consequences

- Read paths join one logical job to multiple postings; this cost is accepted for correct provenance.
- Merge decisions require rollback APIs and audit records from their first implementation.
- Vector similarity can rank proposals but cannot own identity.

## Verification

M0 migrations must enforce the listed unique constraints. M1 tests must prove idempotent recrawl, one-source closure without canonical closure, content-version insertion exactly once, and reversible merge/split behavior.
