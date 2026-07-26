# Architecture decision records

ADRs are immutable decision snapshots. Superseding a decision requires a new ADR that links the old record; accepted records are not silently rewritten after implementation depends on them.

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-mvp-scope.md) | MVP scope and release boundaries | Accepted (scope widened by [0011](0011-application-centered-scope.md)) |
| [0002](0002-canonical-job-model.md) | Canonical job and posting model | Accepted |
| [0003](0003-side-effect-authority.md) | External side-effect authority chain | Refined by [0011](0011-application-centered-scope.md) |
| [0004](0004-google-oauth-scopes.md) | Google OAuth, Gmail and Calendar scopes | Refined by [0011](0011-application-centered-scope.md) |
| [0005](0005-storage-and-retention.md) | Content storage, retention and recovery | Accepted |
| [0006](0006-model-privacy.md) | Model isolation, privacy and qualification | Accepted |
| [0007](0007-console-auth.md) | Single-user console authentication | Accepted |
| [0008](0008-compensation-normalization.md) | Compensation normalization and FX | Accepted |
| [0009](0009-job-assignment-projection.md) | Current job assignment projection | Accepted |
| [0010](0010-content-blob-lifecycle.md) | Physical blob and logical content lifecycle | Accepted |
| [0011](0011-application-centered-scope.md) | Application-centered workspace with system-managed delivery | Accepted |

The authoritative delivery order and exit gates remain in [the MVP plan](../../.omx/plans/careerops-agent-mvp-plan.md). Risk decisions are narrowed by [the risk closure record](../../.omx/plans/careerops-agent-risk-closure.md).
