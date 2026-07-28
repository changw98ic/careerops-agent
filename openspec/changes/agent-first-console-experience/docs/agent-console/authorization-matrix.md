# Agent console authorization matrix

This is a normative annex to the Agent-first console change. Candidate scope is derived from the authenticated session. A request-body candidate ID is never an authority input.

| Resource/operation | Candidate (own) | Scoped operator | Agent worker | Retention service | Cross-candidate/unknown |
| --- | --- | --- | --- | --- | --- |
| action queue read | read own | redacted scoped read | deny | deny | non-enumerating 404 |
| action accept/snooze/dismiss/complete | mutate own with CSRF + idempotency | scoped stop/override only with audit | deny | deny | 404 + audit |
| context create/read/invalidate | own with CSRF; read own | scoped metadata only | read signed input; cannot create/invalidate | metadata due check | 404 |
| capability/preflight/preview read | own redacted | scoped redacted aggregate | read policy/consent for assigned run | deny | 404 |
| preview apply/reject | own with CSRF + consent + confirmation + idempotency | no candidate content by default | deny | deny | 404 |
| run/stage list/detail | own; `no-store` | explicit operational scope, redacted | assigned run only | retention metadata | 404 |
| run start/retry/stop/review | own with CSRF + idempotency | scoped stop only, audited | assigned workflow signal/write only | deny | 404 |
| crawl plan create/activate/run | own with CSRF + idempotency + source policy | scoped operational action | assigned workflow only | deny | 404 |
| resume/evidence/profile read/apply | own through existing contract | no raw content by default | signed version IDs only | retention metadata | 404 |
| package edit/approve/export | own with CSRF + idempotency + 10-minute confirmation nonce | scoped review metadata | deny | retention metadata | 404 |
| aggregate usage/latency metrics | own candidate aggregate | approved bounded aggregate | assigned run counters | retention counters | no candidate leakage |
| audit read | own approved redacted events | approved scope through read function | deny | purge metadata only | deny |
| outbound send/OAuth/mail sync | deny in this change | deny unless a separate approved capability exists | deny | deny | deny |

All state-changing requests require the configured console `Origin`, SameSite cookies, CSRF, and `Idempotency-Key`. Missing/mismatched Origin or CSRF is denied before a state change. Wildcard CORS and frame embedding are denied. Candidate-facing errors are non-enumerating `404` for an unowned resource. Operator and worker identities are server-issued principals; no client field can select them.

The idempotency record is bound to `(actor_id, candidate_id, resource_type, resource_id, operation, canonical_body_hash)`, retained for 24 hours, and protected by a database uniqueness constraint. Same key/same hash returns the original receipt; same key/different hash returns `409 IDEMPOTENCY_CONFLICT`.

Endpoint-level result contract:

| Endpoint family | Own candidate | Foreign candidate | Unknown policy/dependency | Replay/conflict |
| --- | --- | --- | --- | --- |
| `GET /api/v1/agent-console/actions`, `GET /api/v1/agent-console/contexts/{context_id}`, `GET /api/v1/agents/runs`, `GET /api/v1/agents/runs/{run_id}`, `GET /api/v1/agents/runs/{run_id}/stages`, `GET /api/v1/agents/runs/{run_id}/reviews` | `200` redacted/no-store | `404 NOT_FOUND` | `503 DEPENDENCY_NOT_READY` or blocked read model | n/a |
| `POST /api/v1/agent-console/actions/{action_key}/accept` | `200` receipt | `404 NOT_FOUND` | `422 PREREQUISITE_BLOCKED` / `403 POLICY_DENIED` | same key `200` replay; changed body `409 IDEMPOTENCY_CONFLICT` |
| `POST /api/v1/agent-console/actions/{action_key}/snooze` | `200` receipt | `404 NOT_FOUND` | `422 INVALID_REQUEST` / `403 POLICY_DENIED` | same key `200` replay; changed body `409 IDEMPOTENCY_CONFLICT` |
| `POST /api/v1/agent-console/actions/{action_key}/dismiss` | `200` receipt | `404 NOT_FOUND` | `409 STALE` / `403 POLICY_DENIED` | same key `200` replay; changed body `409 IDEMPOTENCY_CONFLICT` |
| `POST /api/v1/agent-console/actions/{action_key}/complete` | `200` receipt | `404 NOT_FOUND` | `422 PREREQUISITE_BLOCKED` / `403 POLICY_DENIED` | same key `200` replay; changed body `409 IDEMPOTENCY_CONFLICT` |
| `POST /api/v1/agent-console/contexts` | `201` | `404` after ownership resolution | `403 POLICY_DENIED`, `409 CONTEXT_STALE`, `503` | changed body `409` |
| `POST /api/v1/agent-console/preflight` | `200` | `404` after ownership resolution | `403 POLICY_DENIED`, `409 CONTEXT_STALE`, `503` | changed body `409` |
| `POST /api/v1/agents/job-matching` | `202` | `404` after ownership resolution | `403 POLICY_DENIED`, `409 CONTEXT_STALE`, `503` | changed body `409` |
| `POST /api/v1/agents/resume-review` | `202` | `404` after ownership resolution | `403 POLICY_DENIED`, `409 CONTEXT_STALE`, `503` | changed body `409` |
| `POST /api/v1/agents/interview-preparation` | `202` | `404` after ownership resolution | `403 POLICY_DENIED`, `409 CONTEXT_STALE`, `503` | changed body `409` |
| `POST /api/v1/agents/runs/{run_id}/retry` | `202` | `404 NOT_FOUND` | `409 NOT_RETRYABLE` / `403 POLICY_DENIED` | same receipt; changed body `409` |
| `POST /api/v1/agents/runs/{run_id}/stop` | `202` | `404 NOT_FOUND` | `409 STOP_NOT_ALLOWED` / `403 POLICY_DENIED` | same receipt; changed body `409` |
| `POST /api/v1/agents/runs/{run_id}/review` | `200` | `404 NOT_FOUND` | `409 STALE` / `422 EVIDENCE_REQUIRED` | same receipt; changed body `409` |
| `POST /api/v1/crawl-plans/versions`, `POST /api/v1/crawl-plans/versions/{version_id}/activate`, `POST /api/v1/crawl-plans/pause`, `POST /api/v1/crawl-plans/resume`, `POST /api/v1/crawl-plans/run-now`, `POST /api/v1/crawl-runs/{run_id}/retry`, `POST /api/v1/crawl-runs/{run_id}/stop` | existing success code with receipt | non-enumerating `404` | `403 SOURCE_POLICY_DENIED` / `503` | same key receipt; changed body `409` |
| `POST /api/v1/smart-intake/previews`, `POST /api/v1/smart-intake/previews/{preview_id}/apply`, `POST /api/v1/applications/{application_id}/packages`, `POST /api/v1/applications/{application_id}/packages/{version_id}/approve`, `POST /api/v1/applications/{application_id}/packages/{version_id}/edits` | existing typed success with CSRF/confirmation | `404` | `409 STALE`/`422 EVIDENCE_REQUIRED`/`403 EXTERNAL_WRITE_DENIED` | same key receipt; changed body `409` |

Canonical route coverage (all paths include the `/api/v1` prefix):

| Exact route(s) | Candidate scope | State change | Required controls |
| --- | --- | --- | --- |
| `GET /api/v1/agent-console/actions`, `GET /api/v1/agent-console/contexts/{context_id}` | session candidate only | no | session, signed candidate cursor, `no-store` |
| `POST /api/v1/agent-console/actions/{action_key}/accept` | action belongs to session candidate | yes | Origin, CSRF, idempotency, audit, source/version check |
| `POST /api/v1/agent-console/actions/{action_key}/snooze` | action belongs to session candidate | yes | Origin, CSRF, idempotency, audit, source/version check |
| `POST /api/v1/agent-console/actions/{action_key}/dismiss` | action belongs to session candidate | yes | Origin, CSRF, idempotency, audit, source/version check |
| `POST /api/v1/agent-console/actions/{action_key}/complete` | action belongs to session candidate | yes | Origin, CSRF, idempotency, audit, source/version check |
| `POST /api/v1/agent-console/contexts` | context belongs to session candidate | yes | Origin, CSRF, idempotency, audit, source/version check |
| `POST /api/v1/agent-console/preflight` | context belongs to session candidate | yes | Origin, CSRF, idempotency, audit, source/version check |
| `GET /api/v1/capabilities/agent` | operation is an enum; no candidate content | no | session, redacted response |
| `GET /api/v1/agents/runs`, `GET /api/v1/agents/runs/{run_id}`, `GET /api/v1/agents/runs/{run_id}/stages` | run and every stage belong to session candidate | no | session, signed cursor, `no-store` |
| `GET /api/v1/agents/runs/{run_id}/reviews` | run belongs to session candidate | no | session, ownership, `no-store`, redacted actor |
| `POST /api/v1/agents/job-matching`, `POST /api/v1/agents/resume-review`, `POST /api/v1/agents/interview-preparation`, `POST /api/v1/agents/runs/{run_id}/retry`, `POST /api/v1/agents/runs/{run_id}/stop`, `POST /api/v1/agents/runs/{run_id}/review` | context/run belongs to session candidate | yes | Origin, CSRF, idempotency, preflight/lease/review checks, audit; URL slugs map one-to-one to the underscore operation enum |
| `GET /api/v1/capabilities/agent`, `GET /api/v1/smart-intake/capability`, `GET /api/v1/smart-intake/previews`, `GET /api/v1/smart-intake/previews/{preview_id}` | operation/preview belongs to session candidate where applicable | no | session, policy/capability check, signed cursor where paginated, `no-store` |
| `GET /api/v1/crawl-runs`, `GET /api/v1/crawl-runs/{run_id}`, `GET /api/v1/crawl-runs/{run_id}/stages` | plan/run/stage belongs to session candidate | no | session, signed cursor, `no-store` |
| `GET /api/v1/crawl-plans`, `GET /api/v1/crawl-plans/versions`, `GET /api/v1/crawl-plans/versions/{version_id}` | plan/version belongs to session candidate | no | session, ownership, `no-store` |
| `POST /api/v1/crawl-runs/{run_id}/retry`, `POST /api/v1/crawl-runs/{run_id}/stop`, `POST /api/v1/crawl-plans/versions`, `POST /api/v1/crawl-plans/versions/{version_id}/activate`, `POST /api/v1/crawl-plans/pause`, `POST /api/v1/crawl-plans/resume`, `POST /api/v1/crawl-plans/run-now` | plan/run belongs to session candidate | yes | Origin, CSRF, idempotency, source policy, audit |
| Internal audit/metrics read projections (no new candidate HTTP route) | own redacted event/aggregate projection only | no | candidate-scoped security-definer read; no raw provider/source data |
| `POST /api/v1/smart-intake/previews`, `POST /api/v1/smart-intake/previews/{preview_id}/apply`, `POST /api/v1/applications/{application_id}/packages`, `POST /api/v1/applications/{application_id}/packages/{version_id}/approve`, `POST /api/v1/applications/{application_id}/packages/{version_id}/edits` | package/preview belongs to session candidate | yes | Origin, CSRF, idempotency, evidence/context check; package confirmation nonce for approve |
| `GET /api/v1/applications/{application_id}/packages`, `GET /api/v1/applications/{application_id}/packages/latest` | application/package belongs to session candidate | no | session, ownership, `no-store` |
| Any `/api/v1/*` outbound send, OAuth, mail sync, `system-send`, or external submission route | deny in this change | never | `403 EXTERNAL_WRITE_DENIED` or existing separate capability gate; never an Agent action |

The brace notation in this table is documentation shorthand for the finite
literal alternatives shown inside it; the router and contract tests enumerate
each literal route. It is not a wildcard authorization rule. `audit` and
`metrics` are projections only: the API role cannot select a base audit table,
raw provider log, worker identity, or cross-candidate aggregate.

### Exact existing-authority route inventory

The following is the complete finite route inventory for the existing
resume/profile/evidence/matching/smart-intake authorities that this change
links into. A `{...}` segment is a typed UUID path parameter, not a wildcard;
each row is an individually tested method/path pair. Routes not listed here
are outside this change and cannot be reached through an Agent action.

| Method and exact path | Scope | Mutation controls / read controls |
| --- | --- | --- |
| `GET /api/v1/smart-intake/capability` | session candidate, redacted | session, `no-store` |
| `GET /api/v1/smart-intake/previews` | session candidate preview list | session, signed cursor, `no-store` |
| `POST /api/v1/smart-intake/previews` | session candidate | Origin, CSRF, idempotency, policy/preflight, audit, `no-store` |
| `GET /api/v1/smart-intake/previews/{preview_id}` | owning candidate preview | session, ownership, `no-store` |
| `POST /api/v1/smart-intake/previews/{preview_id}/apply` | owning candidate preview | Origin, CSRF, idempotency, context digest, decision hash, audit, `no-store` |
| `GET /api/v1/profile` | session candidate active version | session, ownership, `no-store` |
| `GET /api/v1/profile/versions` | session candidate versions | session, ownership, `no-store` |
| `GET /api/v1/profile/versions/{version_id}` | owning candidate version | session, ownership, `no-store` |
| `POST /api/v1/profile` | session candidate | Origin, CSRF, idempotency, validation, audit |
| `POST /api/v1/profile/versions/{version_id}/activate` | owning candidate version | Origin, CSRF, idempotency, validation, audit |
| `GET /api/v1/resumes` | session candidate versions | session, ownership, `no-store` |
| `GET /api/v1/resumes/eligible` | session candidate confirmed versions | session, ownership, `no-store` |
| `GET /api/v1/resumes/{version_id}` | owning candidate version | session, ownership, `no-store` |
| `POST /api/v1/resumes` | session candidate | Origin, CSRF, idempotency, size/type validation, audit |
| `POST /api/v1/resumes/{version_id}/confirm` | owning candidate version | Origin, CSRF, idempotency, parse-state check, audit |
| `GET /api/v1/resumes/{version_id}/evidence` | owning candidate resume | session, ownership, `no-store` |
| `GET /api/v1/evidence` | session candidate evidence | session, ownership, `no-store` |
| `GET /api/v1/evidence/{evidence_id}` | owning candidate evidence | session, ownership, `no-store` |
| `POST /api/v1/evidence/{evidence_id}/confirm` | owning candidate evidence | Origin, CSRF, idempotency, audit |
| `POST /api/v1/evidence/{evidence_id}/reject` | owning candidate evidence | Origin, CSRF, idempotency, audit |
| `GET /api/v1/candidates/{candidate_id}/evidence` | session candidate only; path must equal session | session, substitution check, `no-store` |
| `GET /api/v1/candidates` | session candidate only; no candidate query authority | session, bounded list, `no-store` |
| `POST /api/v1/evidence/import` | session candidate only; body candidate is non-authoritative | Origin, CSRF, idempotency, substitution check, audit |
| `GET /api/v1/jobs/{job_id}/remote-eligibility` | owning job only | session, ownership, `no-store` |
| `GET /api/v1/jobs/{job_id}/compensation` | owning job only | session, ownership, `no-store` |
| `GET /api/v1/matches` | session candidate matches | session, signed cursor, `no-store` |
| `POST /api/v1/matches/run` | session candidate + owned job | Origin, CSRF, idempotency, deterministic authority, audit |
| `GET /api/v1/applications/{application_id}/packages` | owning application | session, ownership, `no-store` |
| `GET /api/v1/applications/{application_id}/packages/latest` | owning application | session, ownership, `no-store` |
| `POST /api/v1/applications/{application_id}/packages` | owning application | Origin, CSRF, idempotency, evidence/context check, audit |
| `POST /api/v1/applications/{application_id}/packages/{version_id}/approve` | owning package version | Origin, CSRF, idempotency, ten-minute confirmation nonce, audit |
| `POST /api/v1/applications/{application_id}/packages/{version_id}/edits` | owning package version | Origin, CSRF, idempotency, evidence/context check, audit |
| `GET /api/v1/companies` | session candidate, bounded company read | session, `no-store` |
| `GET /api/v1/jobs` | session candidate, bounded job read | session, `no-store` |
| `GET /api/v1/jobs/{job_id}` | owning/visible job | session, ownership, `no-store` |
| `GET /api/v1/inbox` | session candidate inbox | session, signed cursor, `no-store` |
| `GET /api/v1/inbox/{job_id}` | owning/visible inbox job | session, ownership, `no-store` |
| `GET /api/v1/inbox/{job_id}/excluded-reasons` | owning/visible inbox job | session, ownership, `no-store` |
| `POST /api/v1/inbox/{job_id}/favorite` | owning/visible inbox job | Origin, CSRF, idempotency, audit |
| `POST /api/v1/inbox/{job_id}/ignore` | owning/visible inbox job | Origin, CSRF, idempotency, audit |
| `POST /api/v1/inbox/{job_id}/snooze` | owning/visible inbox job | Origin, CSRF, idempotency, audit |
| `GET /api/v1/applications` | session candidate applications | session, signed cursor, `no-store` |
| `POST /api/v1/applications` | session candidate + owned job | Origin, CSRF, idempotency, audit |
| `POST /api/v1/applications/{application_id}/transition` | owning application | Origin, CSRF, idempotency, audit |
| `GET /api/v1/applications/{application_id}/events` | owning application | session, ownership, `no-store` |
| `POST /api/v1/applications/{application_id}/prepare` | owning application | Origin, CSRF, idempotency, audit |
| `GET /api/v1/applications/{application_id}/channels` | owning application | session, ownership, `no-store` |
| `POST /api/v1/applications/{application_id}/channel` | owning application | Origin, CSRF, idempotency, audit |
| `POST /api/v1/applications/{application_id}/package` | owning application | Origin, CSRF, idempotency, package authority, audit |
| `GET /api/v1/applications/{application_id}/timeline` | owning application | session, ownership, `no-store` |
| `POST /api/v1/applications/{application_id}/state` | owning application | Origin, CSRF, idempotency, audit |
| `POST /api/v1/resume-versions`, `POST /api/v1/application-packages`, `POST /api/v1/follow-ups` | session candidate | Origin, CSRF, idempotency, validation, audit |
| `GET /api/v1/applications/{application_id}/email-draft` | owning application, local draft only | session, ownership, `no-store`; no outbound effect |
| `POST /api/v1/applications/{application_id}/send-email` | deny in this change | never; `403 EXTERNAL_WRITE_DENIED` before network |
| `POST /api/v1/applications/{application_id}/submit`, `POST /api/v1/applications/{application_id}/confirm-external-submission` | deny in this change | never; `403 EXTERNAL_WRITE_DENIED` before external side effect |
