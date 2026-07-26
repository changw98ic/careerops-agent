# Plan Review 3 — Data Model, API, and Compatibility

## Review method

This pass reviewed the plan against the current SQLAlchemy/Alembic schema, repository protocols, `RuntimeResources` wiring, existing FastAPI routes, Vue client, and the single-user ownership model. It focused on migration safety, API identity, backward compatibility, and whether the planned UI can consume durable projections.

## Findings

### 1. Additive migration strategy is appropriate

Tasks 2.1–2.10 cover ownership, profile/resume/evidence, application cycles, new projection fields, role grants, and legacy backfill. Quarantining ambiguous legacy links is necessary and correctly avoids silently reassigning history.

### 2. Runtime wiring needs an explicit task

The current repository constructs repositories and services in `src/careerops/infrastructure/runtime.py` and exposes them through `api/app.py`. New repositories/services must be wired there as a deliberate task; otherwise route contracts can exist while runtime returns dependency-not-ready responses.

### 3. API identity and idempotency are covered but list behavior needs one explicit contract

The plan includes ownership, idempotency, and cursor pagination for the job inbox. Crawl runs, email threads/messages, applications, proposals, drafts, and reminders also need bounded pagination and no-store behavior for sensitive responses.

### 4. Existing email/read-only records need a compatibility mapping

The current code has internal `ReplyDraft`, `EmailThread`, `EmailMessage`, and receipt concepts. The implementation must map existing records to the new application/thread/timeline projections without interpreting old approved drafts as sent messages.

### 5. No new dependency is required

The plan can use existing FastAPI, PostgreSQL/Alembic, Redis, Temporal, LangGraph, Vue, and current provider ports. Adding a new queue or ORM is unnecessary and would increase migration risk.

## Required changes

- Add an explicit runtime wiring task.
- Add bounded pagination/no-store API contract tasks for sensitive collection endpoints.
- Add compatibility tests proving legacy internal drafts are not reported as provider-sent.

## Review decision

PASS WITH REQUIRED COMPATIBILITY REVISIONS. The data/API direction is sound after these tasks are added.
