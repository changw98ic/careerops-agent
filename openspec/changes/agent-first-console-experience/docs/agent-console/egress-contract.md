# Agent console egress contract

## Model provider

`ProviderPolicy` is server-owned and contains `policy_id`, `version`, `provider_id`, `model_id`, allowlisted HTTPS host, `verified_tls=true`, region, retention days, `training_allowed=false`, a known/supported deletion policy with a bounded SLA, approved operations/fields, effective/expiry timestamps, and approval source. Its canonical `model_operation` enum is `job_matching`, `resume_review`, `interview_preparation`, or `smart_form_intake` (the UI may display “smart-form intake”). Missing/expired/unknown policy fields deny before connection.

`ConsentEnvelope` contains server UUID `consent_id`, candidate, operation, immutable source/version digest set, field-set hash, provider policy version, `ttl_seconds=900`, granted/expiry time (15 minutes), revocation time, and single-use nonce. The worker rechecks it when claiming a queued run and immediately before provider connection. A changed source, field set, provider policy, revoked consent, expired nonce, or budget failure blocks the run.

Allowed fields are operation-specific:

- `job_matching`: normalized job title/location/requirements, candidate target titles, normalized skills, confirmed evidence claim text/version IDs.
- `resume_review`: the matching fields plus confirmed evidence claims and presentation metadata; never raw PDF/resume bytes or direct contact/authorization/compensation fields by default.
- `interview_preparation`: selected job facts, confirmed evidence claims, and explicitly selected user practice goals as untrusted text.
- `smart_form_intake`: bounded user-entered intake text and the selected non-sensitive form target.

Mailbox content, browser HTML, cookies, credentials, arbitrary URLs, unconfirmed evidence, raw prompts, raw responses, and provider SDK request bodies are excluded. The fake-provider test captures the exact JSON and checks the allowlist and server-generated field-set hash.

## Canonical invariants

The closed `model_operation` identifiers are case-sensitive:
`job_matching`, `resume_review`, `interview_preparation`, and
`smart_form_intake`. The same four-value enum is used by `ProviderPolicy`,
`ConsentEnvelope`, preflight, and egress decisions; the UI label “smart-form
intake” is not an API value. The durable Agent workflow uses the explicit
three-value `agent_run_operation` subset
`job_matching|resume_review|interview_preparation`; smart intake remains an
adapter-only model operation and is never a workflow operation.

`ProviderPolicy.verified_tls` is required and must be `true`; `expires_at` is a
finite timestamp after `effective_at` and an expired policy denies. Its
`approved_fields` object contains exactly one closed field array for each
approved operation and no field array for an unapproved operation. The schema
and server validator enforce both directions of that relationship.

An `EgressDecision` with `allowed=true` must carry a non-null, active
`consent_id`; the consent must match candidate, operation, source digests,
field-set hash, provider policy version, one-time nonce, and the 15-minute
expiry at queue claim and immediately before provider connection. A deny may
carry a null consent only when the reason is prior to consent issuance. A
revoked/expired/mismatched consent is a deny, never a best-effort provider
call.

The provider-call quota is ten attempts per hour in aggregate across all
providers for one candidate; a provider name is attribution, not a separate
bucket. Every allowed decision also passes the atomic run/candidate budget
contract. Unknown provider, TLS, retention/training/deletion, operation,
field, budget, or consent state is denied before network connection.

## Browser/HTTP source

The browser sidecar must expose `GET /ready` and `POST /v1/browser/run`. Readiness includes an immutable image digest, policy-bundle digest, egress-proxy/interceptor mode, supported schemes, and key ID. The run request contains only signed candidate/run/attempt IDs, source ID, canonical start URL, allowlist version, fresh task-space nonce, budgets, and output schema version.

The egress proxy/interceptor observes top-level navigation, redirects, images, iframes, scripts, WebSockets, `fetch`, and XHR. Every request validates canonical IDN/punycode host, scheme, port 80/443, allowlist, DNS/CNAME chain, and connected socket IP. Loopback/private/link-local/multicast/IPv6-local/metadata addresses, rebinding, unsafe schemes, unallowlisted hosts, and non-default ports are denied. Only GET/HEAD are allowed; WebSocket upgrades, uploads, form POSTs, downloads, and arbitrary headers are denied. Redirects ≤3, response ≤2 MiB, timeout ≤15s, source concurrency ≤2, and source rate ≤60 requests/minute.

Each task space is fresh: no reused tab, cookies, service worker, cache, local/session storage, OAuth/session material, Authorization header, upload, or arbitrary header. A missing host allowlist is deny, not allow-all. Raw HTML, screenshots, downloads, page source, and browser errors are deleted at task completion or their configured retention deadline.

The worker signs a canonical request with an active key ID, RFC 3339 timestamp,
and unique UUID replay nonce; the sidecar verifies mTLS/signature, enforces a
±60-second clock window, and rejects replay. The nonce is a replay token, not a
monotonic sequence. Key rotation has a 24-hour overlap; keys are never logged.
Sidecar absence or inability to prove subrequest isolation yields
`dependency_not_ready` and preserves only policy-approved HTTP/manual
alternatives.

Canonicalization uses IDNA2008 A-label/punycode lowercasing, strips one trailing dot, rejects wildcards, embedded credentials, fragments, and non-ASCII comparison ambiguity, and follows at most five CNAME hops. All A/AAAA answers are checked, including IPv4-mapped IPv6. The provider client verifies the certificate hostname against the allowlisted host, rejects proxy bypass, and never treats an empty host allowlist as allow-all. Provider-side deletion is not assumed; a provider with unknown retention/training/deletion policy is denied before connection.
