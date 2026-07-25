# Plan Review 4 — Testability, Recovery, and Operations

## Review method

This pass reviewed whether the plan can prove correctness after crashes, retries, stale data, partial sync, provider ambiguity, migration failure, and frontend dependency outages. It also checked whether operators have enough metrics and runbooks to diagnose a broken crawl or pending send.

## Findings

### 1. Recovery coverage is strong in the core path

Tasks 5.8, 10.6/10.9, 11.3/11.9, 15.2–15.4, and 16.6 cover crawl retry, provider ambiguity, mail cursor recovery, state transitions, Temporal replay, and rollback.

### 2. Verification should be staged, not only final

The added vertical-slice gates are useful. They should be paired with explicit per-phase test commands or recorded evidence so a later failure does not obscure which earlier boundary was last healthy.

### 3. Operational controls need runbook and alert tasks

Metrics are specified, but the plan should explicitly document alerts and operator actions for stuck crawl runs, repeated source denial, stale mail cursors, pending approvals, reconciliation backlog, revoked accounts, and failed retention/purge.

### 4. Existing legacy scripts need an authority audit

Because this repository contains scripts that historically sent email or crawled directly, a final grep/contract test should prove no legacy entry point bypasses the current policy/kernel boundary.

### 5. Performance/size budgets are partly covered

Frontend bundle size is covered. The plan should add bounded API response/worker batch budgets and a crawl/mail backpressure test so a large source or mailbox cannot create unbounded memory or UI payloads.

## Required changes

- Add staged verification evidence tasks.
- Add operations runbook/alert tasks.
- Add legacy-authority audit and backpressure/batch-bound tests.

## Review decision

PASS WITH REQUIRED OPERATIONS REVISIONS. The recovery design is credible after these operational tasks are added.
