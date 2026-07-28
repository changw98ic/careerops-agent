# Agent console authority map

This annex resolves overlap with `llm-agent-career-loop` and `ai-assisted-smart-forms`.

| Capability | Existing authority | Agent-console adapter | Forbidden shortcut |
| --- | --- | --- | --- |
| Crawl | crawl-plan service, `CrawlRunWorkflow`, canonical jobs, inbox projection | readiness/provenance/action/context views | new crawl table, API-handler browser call, silent source fallback |
| Matching | `/api/v1/matches/run`, `MatchOrchestrator`, deterministic `filter_decisions` | review-only explanation in `agent_runs` | model changes hard verdict, evidence, or new semantic-ranking authority |
| Smart intake | `/api/v1/smart-intake/previews*`, existing short-lived store/decision | preflight/capability presentation | second generic preview store |
| Resume | `/api/v1/resume-versions`, `/api/v1/resumes`, `resume_versions` | review diff and explicit adapter call | direct mutation of base version or evidence |
| Interview preparation | `agent_runs.result` + `agent_run_reviews`; smart-intake for user context | context continuity and editable review draft | new unreviewed facts/table or external communication |
| Application package | `/api/v1/applications/{id}/packages*`, `application_package_versions` | field/evidence adapter and review route | dual write to legacy/new package authorities or auto-send |
| Mail/reply | existing mail/reply routes and policy gates | explicitly excluded from new actions | `sendApplicationEmail`, `sendReplyDraft`, `confirmSystemSend`, `syncMailNow`, `confirmExternalSubmission` |
| Audit | `careerops.append_audit_event` | bounded event taxonomy/read function | API-role direct INSERT/select or separate audit transaction |
| Browser | dedicated digest-pinned sidecar contract | readiness and bounded activity adapter | implicit PATH `ego-browser`, reused tab/session, unbounded egress |

The delta is stricter where it adds unknown-state denial, field-level egress, safe rendering, candidate-scoped read authorization, or append-audit atomicity. Earlier capabilities remain the source of truth; they are not silently replaced.

## Outbound deny matcher

The Agent action router calls `careerops.agent_target_policy` before it emits
any navigation, adapter request, or action receipt. Its input is the
server-generated `{action_key, operation, method, canonical_path}`; client
supplied URLs, methods, and action names are not authority inputs. The matcher
canonicalizes once (rejects encoded slash/backslash, credentials, fragments,
dot segments, and non-HTTP schemes) and applies this finite deny table:

| Method | Canonical path pattern | Denial |
| --- | --- | --- |
| `POST` | `/api/v1/reply/drafts/{draft_id}/send` | `EXTERNAL_WRITE_DENIED` |
| `POST` | `/api/v1/mail/sync-now` | `EXTERNAL_WRITE_DENIED` |
| `POST` | `/api/v1/applications/{application_id}/system-send` | `EXTERNAL_WRITE_DENIED` |
| `POST` | `/api/v1/applications/{application_id}/confirm-external-submission` | `EXTERNAL_WRITE_DENIED` |
| `POST` | `/api/v1/mail/account/revoke` or any OAuth connect/callback path | `OAUTH_DENIED` |
| `POST` | any path not in the server action route registry | `TARGET_NOT_ALLOWLISTED` |

The registry contains only `/profile`, `/resumes`, `/evidence`,
`/crawl-plans`, `/crawl-runs`, `/jobs`, `/inbox`, `/ai-workbench`, and
`/applications/{id}` navigation/read targets plus the explicitly listed
review-only adapter operations. The matcher also rejects operation/action
keys `sendApplicationEmail`, `sendReplyDraft`, `confirmSystemSend`,
`syncMailNow`, and `confirmExternalSubmission`, including case/encoding
variants after canonicalization. An unknown adapter/service name, HTTP method,
or path is denied. This check runs again at the server dispatcher boundary;
passing a UI route check cannot authorize a network write.
