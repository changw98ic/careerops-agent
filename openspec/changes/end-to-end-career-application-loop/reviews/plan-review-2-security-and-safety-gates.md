# Plan Review 2 — Security and Safety Gates

## Review method

This pass reviewed every task that touches model input, credentials, OAuth, external writes, attachments, provider retries, inbound mail, and release qualification against `AGENTS.md`, ADR 0003/0004/0006, the threat model, and the repository's explicit default-deny invariants.

## Findings

### 1. The plan preserves the right send architecture

Tasks 9–10 require trusted recipients, exact payload hashes, CareerOps confirmation, durable intent/outbox, isolated worker, receipt, reconciliation, and application-state update only after provider success. The plan correctly avoids a direct API-to-Gmail shortcut.

### 2. The plan correctly treats model and mail content as untrusted

Tasks 3.7, 8.4, 12.3, 12.8, and 15.5/15.6 constrain model input, block unsupported claims, test prompt injection, and prohibit provider credentials from API/model/parser paths.

### 3. A repository-level safety conflict must be corrected

The current repository instructions explicitly prohibit enabling `CAREEROPS_EXTERNAL_WRITES_ENABLED`, `CAREEROPS_AUTO_SEND_ENABLED`, and `CAREEROPS_GOOGLE_OAUTH_ENABLED`. The plan currently describes later provider/sandbox execution and staged enablement too broadly. This change may implement ports, fakes, contracts, and fail-closed guards, but it must not enable those flags or require a live OAuth/send account.

### 4. Attachment validation is present and testable

Task 9.5 plus the package and email specs cover MIME/type/size/hash/quarantine/retention checks. The plan should keep this validation before intent creation and repeat it at confirmation time.

## Required changes

- State explicitly that all live OAuth/provider activation is outside this change and prohibited by the current repository safety contract.
- Replace “enable” language with “verify gated path using fake/isolated test harness; leave flags disabled”.
- Keep a separate future release/qualification change for any real capability activation.

## Review decision

PASS WITH REQUIRED SAFETY REVISION. The design is safe after removing current-change activation language.
