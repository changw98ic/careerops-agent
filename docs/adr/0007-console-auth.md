# ADR 0007: Use a single-user, loopback-first console security model

- Status: Accepted
- Date: 2026-07-17

## Context

Even a single-user console can send email and create events after later milestones. Binding a development server publicly or treating localhost as authentication would make those actions available to other local processes or a misconfigured proxy.

## Decision

The initial user is created by a one-time local CLI bootstrap. The bootstrap secret is printed once, expires after 15 minutes or first use, is stored hashed and cannot be used as a normal session credential.

Passwords use Argon2id with parameters recorded in the password row for future rehash. Sessions use a cryptographically random opaque token; only its hash is stored. Cookies are `HttpOnly`, `SameSite=Strict` and `Secure` whenever TLS is in use. Session idle TTL is eight hours, absolute TTL seven days, and tokens rotate on login, password change and sensitive integration changes.

Every mutation requires a CSRF token bound to the session and request origin. Login, bootstrap and approval endpoints are rate-limited and audited without logging credentials. Logout, password change and integration revoke invalidate relevant sessions.

Default bind address is loopback. Public access requires an explicitly configured TLS reverse proxy, an allowlisted proxy hop and strict `Forwarded`/`X-Forwarded-*` parsing. Unknown proxy headers are ignored. There is no unauthenticated webhook exception in MVP.

Authorization has one human owner role plus service identities. Human login does not grant direct database/provider credentials; approvals are still checked by ADR 0003.

## Consequences

- A lost password requires a local recovery command with console/host access, not an email reset loop.
- Multi-user roles, SSO and remote administration require a new ADR.
- Localhost deployments still require CSRF, session and audit controls.

## Verification

Tests cover expired/reused bootstrap tokens, Argon2 rehash, fixation/rotation, idle/absolute expiry, CSRF missing/mismatch, Origin mismatch, forged proxy headers, login rate limit and session revoke.
