# Spec Review 2 — Security, Privacy, and Authority Boundaries

## Review method

This pass reviewed the specs against the repository's default-deny invariants, `AGENTS.md`, ADR 0003, ADR 0004, ADR 0005, ADR 0006, and the threat model. The focus was whether any model, crawler, inbound email, UI edit, retry, or integration failure could acquire authority it should not have.

## Findings

### 1. User confirmation has the correct meaning

The email requirements now state that confirmation occurs inside CareerOps, binds the exact payload, and causes the system-managed send. This is a human authorization event, not a request for the user to finish in Gmail. The backend still has to pass policy, capability, credential, outbox, worker, receipt, and reconciliation gates.

### 2. Recipient authority is correctly separated from model output

The initial recipient must come from a source-backed recruiting contact. Reply recipients and thread headers come from trusted provider metadata. Model output cannot widen the recipient, target, OAuth scope, or policy facts.

### 3. Prompt injection boundaries are covered

Both job content and recruiting email/attachment content are treated as untrusted. The specs explicitly prohibit tool/policy/state changes from such content and require review-only model proposals.

### 4. Default-deny and high-risk behavior are preserved

External writes, Google integration, and auto-send remain disabled by default. Salary, Offer, visa, identity/bank, withdrawal, and other high-risk categories cannot be auto-sent. Ambiguous provider results stop in reconciliation rather than retrying blindly.

### 5. Two gaps require explicit specification before design is final

1. The email delivery spec should explicitly require attachment quarantine/type/size/hash validation before an attachment can be sent.
2. The recruiting mail spec should explicitly define account revocation behavior: future sync and provider use must stop immediately while prior audit/history remains available under retention rules.

## Review decision

PASS WITH REQUIRED FIXES. The authority model is sound; the two gaps are narrow safety clarifications and are applied below before the third review.
