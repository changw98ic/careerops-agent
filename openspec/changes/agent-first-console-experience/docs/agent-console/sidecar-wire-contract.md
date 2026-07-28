# Ego/browser sidecar wire contract

The sidecar is an optional, dedicated browser worker. It is not the API
process, a Temporal workflow implementation, or a reusable user browser
session. This document freezes the HTTP and signing contract used by the
workflow activity and by Compose/CI readiness tests.

`sidecar-wire.schema.json` is the machine-readable companion. It validates the
direct JSON body for one HTTP message at a time; the schema does not add a
`ready_response`/`run_request` wrapper around the wire body. Its closed
`partial_reason` values are `SOURCE_TIMEOUT`, `SOURCE_RATE_LIMITED`,
`SOURCE_PARTIAL`, and `SOURCE_DEPENDENCY_LOST`; a full success must use
`partial=false` and `partial_reason=null`.

## 1. Transport and common headers

- The sidecar listens on an internal mTLS address only. It is not published to
  the host or the candidate browser. The caller verifies the sidecar service
  certificate and the sidecar verifies the caller certificate against the
  active key bundle.
- Requests and responses are JSON UTF-8, `Content-Type: application/json`,
  `Cache-Control: no-store`, and `Pragma: no-cache`. Request bodies are capped
  at 64 KiB; response bodies are capped at 2 MiB.
- Required request headers on `POST /v1/browser/run` are
  `X-CareerOps-Key-Id`, `X-CareerOps-Nonce`, `X-CareerOps-Timestamp`,
  `X-CareerOps-Signature`, and `X-CareerOps-Trace-Id`. `Timestamp` is an RFC
  3339 UTC instant and must be within ±60 seconds of the sidecar clock.
  `Nonce` is a UUID replay token stored for the key's 24-hour replay window; it
  is unique, not a monotonic sequence. A duplicate nonce is `409 NONCE_REPLAY`.
- The signature is Ed25519 over the UTF-8 bytes of
  `method + "\n" + path + "\n" + key_id + "\n" + nonce + "\n" + timestamp + "\n" + sha256(body)`.
  The sidecar verifies the active key, mTLS principal, timestamp window, nonce
  uniqueness, and candidate/run binding before parsing the operation.
- A task is bounded by a 15-second request deadline, two concurrent source
  tasks, 60 source requests per minute, three redirects, and a 2 MiB total
  normalized result. The deadline is enforced by the interceptor, not just by
  the caller.

## 2. `GET /ready`

No credentials, query parameters, or request body are accepted. The exact
ready response is `200` only when all fields are present and valid:

```json
{
  "ready": true,
  "service": "careerops-ego-sidecar",
  "protocol_version": "browser-v1",
  "image_digest": "sha256:64hex",
  "policy_bundle_digest": "sha256:64hex",
  "egress_proxy_mode": "forced_interceptor",
  "supported_schemes": ["http", "https"],
  "key_id": "key-2026-01",
  "checked_at": "2026-07-28T00:00:00Z"
}
```

When not ready, the sidecar returns `503` with the same shape except
`ready=false` and a bounded `reason_code` from
`IMAGE_NOT_PINNED`, `POLICY_NOT_PINNED`, `INTERCEPTOR_UNAVAILABLE`,
`KEY_NOT_ACTIVE`, or `DEPENDENCY_NOT_READY`. It never returns secret or
environment values. A missing digest, missing interceptor, unsupported
scheme, or invalid key makes readiness false.

## 3. `POST /v1/browser/run`

The exact request body is:

```json
{
  "run_id": "uuid",
  "attempt_id": "uuid",
  "candidate_scope_digest": "sha256",
  "source_id": "source-key",
  "canonical_start_url": "https://jobs.example.test/list",
  "allowlist_version": "source-policy-v4",
  "task_space_nonce": "uuid",
  "budgets": {
    "max_requests": 60,
    "max_concurrent": 2,
    "max_redirects": 3,
    "max_response_bytes": 2097152,
    "timeout_ms": 15000
  },
  "output_schema_version": "posting-v1"
}
```

The URL is canonicalized with IDNA2008 A-label conversion, lower-case host,
one trailing-dot removal, no credentials/fragments/wildcards, explicit
`http`/`https`, and port 80/443 only. The sidecar rechecks all A/AAAA answers,
CNAME hops (maximum five), IPv4-mapped IPv6, and the connected socket address
on every redirect and subresource. The request is rejected if the source
allowlist is absent, has `None`, or does not contain the canonical host.

Success is `200` and has exactly this bounded shape:

```json
{
  "run_id": "uuid",
  "attempt_id": "uuid",
  "result_state": "succeeded",
  "partial": false,
  "partial_reason": null,
  "postings": [{
    "source_id": "source-key", "source_url": "https://jobs.example.test/p/1",
    "external_id": "job-1", "title": "后端工程师", "organization": "示例公司",
    "location": "上海", "description_digest": "sha256",
    "published_at": "2026-07-27T00:00:00Z", "observed_at": "2026-07-28T00:00:00Z"
  }],
  "counts": {"requests": 4, "redirects": 1, "bytes": 12000},
  "provenance_digest": "sha256",
  "policy_bundle_digest": "sha256:64hex",
  "trace_id": "uuid"
}
```

`description_digest` is a digest, not the description. The sidecar returns no
HTML, screenshot, page source, download, cookie, header, credential, or
browser error text. The caller resolves approved normalized fields through the
existing crawl repository.

## 4. Typed failures and HTTP status

Every failure body is:

```json
{
  "run_id": "uuid|null", "attempt_id": "uuid|null",
  "result_state": "blocked|failed|cancelled",
  "reason_code": "SOURCE_POLICY_DENIED", "retryable": false,
  "safe_message": "该来源暂不可访问，请检查来源策略。",
  "trace_id": "uuid"
}
```

| HTTP | Codes | Retry rule |
| --- | --- | --- |
| `400` | `INVALID_REQUEST`, `URL_NOT_CANONICAL`, `BUDGET_INVALID` | never |
| `401/403` | `MTLS_DENIED`, `SIGNATURE_INVALID`, `SOURCE_POLICY_DENIED`, `UNSAFE_SUBREQUEST` | never; audit |
| `404` | `SOURCE_NOT_CONFIGURED` | never |
| `409` | `NONCE_REPLAY`, `TASK_SPACE_REUSED`, `RUN_ATTEMPT_CONFLICT` | caller must not replay body |
| `429` | `RATE_LIMITED`, `BUDGET_EXCEEDED` | only after gateway budget delay, never beyond run deadline |
| `503` | `INTERCEPTOR_UNAVAILABLE`, `DNS_DEPENDENCY_NOT_READY`, `DEPENDENCY_NOT_READY` | at most two activity attempts |
| `504` | `FETCH_TIMEOUT` | at most one retry if no partial result was committed |
| `200` | `succeeded` or an explicit partial normalized result | partial result must carry `partial=true` and a typed `partial_reason`; a full result carries `partial=false` and `partial_reason=null` |

Any WebSocket upgrade, `POST`/`PUT`/`PATCH`/`DELETE`, upload, download,
arbitrary header, service-worker request, OAuth/session access, private or
metadata IP, unallowlisted redirect, or task-space reuse is a hard deny and
terminates the task. The interceptor covers top-level navigation, image,
iframe, stylesheet, script, `fetch`, XHR, and WebSocket attempts; page
JavaScript cannot bypass it.

## 5. Deployment and verification contract

The Compose service image and policy bundle are digest-pinned. The worker
refuses to call the sidecar unless `/ready` is `200 ready=true` and the
returned digests match the configured digests. If the sidecar is absent, the
workflow records `dependency_not_ready`; HTTP-only/manual source paths remain
available only if their own source policy allows them.

The `CRAWL-02` evidence must include ready and absent Compose runs, a fake
interceptor that records every subrequest, DNS/CNAME/IDN/private-IP/IPv4-mapped
IPv6 cases, signature/nonce replay, fresh-task-space proof, redirect and
budget limits, and the exact success/failure JSON above. No evidence may
contain real credentials or raw external page content.
