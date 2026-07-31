# Dependency baseline

- Snapshot date: 2026-07-18
- Resolver: uv lockfile
- Python: 3.12
- Audit scope: all runtime and development groups, excluding the local editable project

## Direct dependencies

| Package | Locked | License | Purpose and boundary |
| --- | ---: | --- | --- |
| Alembic | 1.18.5 | MIT | PostgreSQL schema migration; shipped because deployment must migrate before startup |
| FastAPI | 0.139.2 | MIT | Versioned local API only |
| Prometheus Client | 0.25.0 | Apache-2.0 AND BSD-2-Clause | `/metrics` exposition through a custom registry; default process, Python and GC collectors are not registered |
| Pydantic Settings | 2.14.2 | MIT | Validated, redacted process configuration |
| Psycopg / Psycopg Binary | 3.3.4 | LGPL-3.0-only | PostgreSQL driver; no libpq/build-tool prerequisite in the current self-hosted baseline |
| SQLAlchemy | 2.0.51 | MIT | Explicit Core metadata and connection lifecycle; domain objects do not depend on ORM models |
| Temporalio | 1.30.0 | MIT | Deterministic workflow/Activity boundary plus replay and worker-restart verification |
| Redis | 6.4.0 | MIT | Readiness, bounded shared coordination, and cross-process authentication throttling; never the business state authority |
| Argon2-cffi | 25.1.0 | MIT | Argon2id password hashing for the single-user console |
| Python Multipart | 0.0.32 | Apache-2.0 | Bounded console form parsing; the M0 API does not accept file uploads |
| Uvicorn | 0.51.0 | BSD-3-Clause | Loopback ASGI server without optional native extras |
| Bandit | 1.9.4 | Apache-2.0 | Development-only Python security linting |
| HTTPX2 | 2.7.0 | BSD-3-Clause | Development-only FastAPI/Starlette contract client |
| Pyright | 1.1.411 | MIT | Strict production-source type checking |
| pytest | 9.1.1 | MIT | Unit, contract and opt-in PostgreSQL integration tests |
| pytest-asyncio | 1.4.0 | Apache-2.0 | Temporal and async lifecycle test support |
| pytest-cov | 7.1.0 | MIT | Branch-aware local coverage reporting |
| Ruff | 0.15.22 | MIT | Formatting and static linting |

The project constrains stable minor/major lines in `pyproject.toml`; `uv.lock` records exact
direct and transitive versions. Database and workflow dependencies now have executable M0
owners. Google, model and browser dependencies remain absent until their later capability
gates are satisfied.

## Audited container image baseline

M0 Compose uses audited, digest-pinned external images. Local application images still use
local repository names, but their base images are pinned in the Dockerfiles.

| Surface | Image reference | Purpose and boundary |
| --- | --- | --- |
| App build helper | `ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d` | Supplies `uv` in the application image build only |
| App runtime | `python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7` | Runtime base for `careerops:0.1.0` |
| PostgreSQL | `postgres:17.5-alpine3.22@sha256:6567bca8d7bc8c82c5922425a0baee57be8402df92bae5eacad5f01ae9544daa` | Base for the local PostgreSQL image with M0 role bootstrap files |
| Redis server | `redis:7.2.14-alpine3.21@sha256:dfa18828cbc07b3ae6a95ec7343f6c214fdee2d836197b4be8e9904420762cd8` | Auth-required, append-only local Redis server; Redis 7.2 BSD-licensed line; availability dependency only |
| Temporal server | `temporalio/auto-setup:1.29.6@sha256:1263120feed69d82e4ca23b8ca6f1d702c3029fe70714e382966d0192318eab6` | Local deterministic workflow test topology |
| Temporal UI | `temporalio/ui:2.34.0@sha256:cb17ea423d76a8a19a269d0bcd81fc12eee1f6365acd2a56b590dafb35696a95` | Loopback-only local Temporal inspection UI |

Digest pinning is necessary but not sufficient for release qualification. M7 still needs
current image advisory review, SBOM capture, provenance/signature policy and deployment
evidence for the exact release artifacts.

## Binary-driver decision

Psycopg documents `psycopg[binary]` as the recommended installation for most users because
it bundles its client libraries and needs no compiler. Its documentation separately prefers
a local `psycopg[c]` build for production sites so operating-system library updates propagate.
The binary choice is accepted for the current single-node development baseline. Before a
release image is qualified, M7 must either:

1. retain the binary wheel and record its bundled libpq/OpenSSL inventory in the SBOM, or
2. switch the production dependency group/image to `psycopg[c]` and prove reproducible builds.

This decision does not permit copying or modifying Psycopg source into CareerOps; any
distribution must preserve the dependency's LGPL notices and replacement rights.

## Audit evidence

The lock graph contains 57 package records: the local editable project plus 56 third-party
distributions across all platform-marker branches. The following command audited the current
lock graph and returned `No known vulnerabilities found` against PyPI advisories:

```bash
make audit
```

An advisory scan is time-sensitive evidence, not a permanent claim. Re-run it after every
lockfile change and before a release. The project-local editable package is excluded because
it has no PyPI advisory identity and is covered by repository security checks instead.

`prometheus-client` is a runtime dependency used only for standards-compatible exposition and
metric primitives. Version 0.25.0 reports the SPDX expression
`Apache-2.0 AND BSD-2-Clause` in its installed distribution metadata. CareerOps supplies an
application-local `CollectorRegistry`, so adding it does not implicitly publish host process,
Python runtime or garbage-collector series. The dependency does not authorize secrets, raw
URLs, email addresses, request IDs or payloads as metric labels.
