# Plan Review 1 — Dependency Order and Executability

## Review method

This pass reviewed the 146 implementation tasks as a dependency graph. It checked whether a task consumes a domain object, API, repository, provider, migration, or UI state that is only created later, and whether each group can be completed in a verifiable session.

## Findings

### 1. Overall order is sound

The main dependency chain is correct:

`contracts → schema/identity → profile/crawl → crawl execution → inbox → application → package → contacts/payload → send → inbound sync → mail proposals → replies/follow-up → UI hardening → pilot`.

### 2. Two implicit dependencies need to be made explicit

- Application channel eligibility depends on the trusted-contact resolver in group 9. The application group should define only the interface and defer the concrete email-contact implementation to group 9.
- Application package binding depends on package-version contracts in group 8. The application group should define the binding point, not duplicate package implementation.

### 3. Existing data migration/backfill is missing

The current repository already contains jobs, candidates, applications, resume references, email drafts, and possibly local data. New ownership/profile/cycle/provenance fields need an explicit additive backfill task so old records do not become unreadable or silently attach to the wrong user.

### 4. The plan needs vertical-slice checkpoints

The groups are technically ordered but could still produce a long period with no usable product. Each major product boundary should have a fake/in-memory or read-only end-to-end checkpoint before proceeding to the next boundary.

## Required changes

- Clarify group 7 interface tasks and their dependencies on groups 8 and 9.
- Add an additive migration/backfill task with explicit ambiguity handling.
- Add vertical-slice gates after crawl, package, system-managed fake send, and inbound mail proposal stages.

## Review decision

PASS WITH REQUIRED PLAN REVISIONS. The order is viable after these clarifications.
