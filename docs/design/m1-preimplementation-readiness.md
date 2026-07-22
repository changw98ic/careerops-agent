# M1 pre-implementation readiness handoff

- Status: pre-implementation handoff only
- Date: 2026-07-18
- Scope: M1 read-only job discovery design boundary before writing crawl code

## Purpose

Use this handoff after D0 evidence collection reaches the full gate. It defines
the minimum safety invariants, module boundaries, test-first milestones, and
forbidden claims for starting M1 implementation. It does not authorize M1 work
while the D0 full gate is failing.

Authoritative inputs remain the durable execution brief, the M1 section of the
MVP plan, the D0 intake runbook, the metric contracts, and the M-1 verifier.

## Current gate state

As of 2026-07-18, M1 implementation must not start.

Verified commands:

```bash
uv run careerops-d0 --root . validate --contracts-only --json
make verify-m1-full
```

Observed state:

| Check | Result | Meaning |
| --- | --- | --- |
| Contracts-only validation | Passes | Static ADR, schema, guide, and metric contracts exist. |
| Full D0 gate | Fails closed | Required real pilot evidence is incomplete. |
| Pilot rows | `0/286` | No real D0 pilot rows currently satisfy the gate. |
| Required roles | Missing reviewer and adjudicator | Role independence is not established. |
| Dataset manifests | Missing all 9 | No dataset can satisfy M1 evidence prerequisites. |
| Release qualification | `false` | M-1 and D0 cannot authorize release or Auto-send. |

## D0 boundary

M1 implementation can begin only after all of the following are true:

1. The M-1 discovery/parser/dedup labeling contracts are frozen and unchanged.
2. `uv run careerops-d0 --root . validate --json` exits zero, or
   `make verify-m1-full` exits zero.
3. The frozen 10% D0 pilot has lawful, real, de-identified evidence for all
   required datasets.
4. Independent reviewer and adjudicator roles are assigned and distinct from the
   data curator.
5. PII/secret scan evidence passes for every referenced artifact.
6. Holdout custody, source lawfulness, consent, retention, and de-identification
   evidence are complete outside the repository-local hash checks.

Passing the D0 handoff permits only M1 implementation work. It does not permit
recurring crawling, release qualification, provider writes, Gmail send,
Calendar writes, or Auto-send.

## Safety invariants

M1 code must preserve these invariants from the first implementation commit:

- Read-only only: discovery may fetch public job-source documents, but it must
  not submit applications, send messages, create calendar events, or mutate
  external providers.
- Safe HTTP before adapters: no adapter may perform network I/O until the safe
  client and crawl policy are implemented and tested.
- SSRF closed by default: allow only approved schemes; deny private, loopback,
  link-local, and metadata networks; re-check every redirect and DNS result.
- Crawl policy closed by default: enforce robots handling, per-domain rate
  limits, concurrency limit `2`, size/time limits, and stop rules for 403, 429,
  `Retry-After`, login walls, and CAPTCHA.
- Terms status gates recurrence: `terms_status=unknown` permits at most one
  low-frequency discovery request and forbids periodic crawling until user
  review; explicit `blocked` forbids crawling.
- Official-source preference: adapters should prioritize company-owned Careers
  pages and official ATS sources before generic parsing.
- Evidence is append-only and reproducible: each fetched document records source
  URL, fetched timestamp, raw response hash, parser version, and adapter version.
- Dedup must be reversible: semantic similarity can propose merges, but cannot
  be the sole reason for automatic canonical merge.
- Raw document retention is bounded: purge full raw bodies by TTL only after
  durable evidence snippets, URL, timestamp, hash, and version references are
  preserved.

## Module boundaries

| Boundary | Owns | Must not own |
| --- | --- | --- |
| `domain/companies.py` | Company identity, official source declarations, terms status | HTTP requests, HTML parsing |
| `domain/jobs.py` | Canonical job, posting version, closed/reopened lifecycle | Crawl scheduling, network policy |
| `domain/crawl.py` | Crawl intent, source policy state, crawl result metadata | Provider writes, application lifecycle |
| `infrastructure/http/safe_client.py` | Scheme/DNS/IP/redirect/body/time safety | Source-specific parsing or dedup |
| Crawl-policy infrastructure | Robots cache, rate limits, concurrency, stop rules | Adapter-specific extraction |
| Source adapters | `detect`, `list_jobs`, `fetch_job` for one source family | Raw socket/network bypasses |
| Normalization/dedup | Canonical URLs, fingerprints, merge proposals, rollback | Fetching remote documents |
| Workflows | Activity orchestration and retry boundaries | Direct I/O outside activities |
| REST/UI | Thin company and jobs inbox surfaces | Release qualification or Auto-send controls |
| Evaluators/fixtures | Frozen adapter, parser, dedup, lifecycle evidence | Rewriting labels or manifests |

## Test-first milestones

Implement M1 in this order. Each milestone starts with failing tests that encode
the gate before production behavior is added.

1. Safe HTTP test matrix:
   - scheme allowlist;
   - private, loopback, link-local, and metadata IP denial;
   - redirect-to-private denial;
   - DNS rebinding revalidation;
   - response size and timeout limits.
2. Crawl policy test matrix:
   - robots cache behavior;
   - per-domain rate limiting;
   - concurrency limit `2`;
   - 403, 429, `Retry-After`, login wall, and CAPTCHA stop rules;
   - `terms_status=unknown` one-shot behavior;
   - `terms_status=blocked` denial.
3. Adapter contract tests:
   - Careers link discovery;
   - Greenhouse;
   - Lever;
   - Ashby;
   - Sitemap;
   - JSON-LD;
   - static HTML.
4. Posting/version tests:
   - same posting refetch is idempotent;
   - content change creates one new version;
   - every posting records source URL, timestamp, content hash, parser version.
5. Canonical job and dedup tests:
   - source ID, canonical URL, company/title/location, fingerprint layers;
   - semantic candidates create merge proposals only;
   - manual merge, split, rollback, and replay.
6. Lifecycle tests:
   - one source closed and another active keeps canonical job active;
   - closed, reopened, and expired transitions are reproducible.
7. Workflow tests:
   - all network and persistence I/O happens in Activities;
   - retries do not duplicate postings;
   - crash/retry keeps evidence references consistent.
8. Retention tests:
   - raw document purge preserves long-term snippet, URL, timestamp, hash, and
     version references;
   - no dangling posting-version reference remains after purge.
9. Product metric tests:
   - Careers entry recall target `>= 0.90`;
   - ATS required-field micro accuracy `>= 0.95`;
   - dedup pairwise F1 `>= 0.98` and precision `>= 0.99`.

## Forbidden claims

Do not claim any of the following from this handoff or from a contracts-only
validation pass:

- M1 implementation is authorized while `make verify-m1-full` fails.
- D0 status output proves lawful source, consent, privacy clearance, identity,
  role independence, or holdout custody.
- Repository-local hashes prove real-world authenticity or legal approval.
- Synthetic, paraphrased, placeholder, or local-only rows satisfy real D0 pilot
  evidence.
- Safe HTTP tests alone authorize recurring crawling.
- Read-only crawling implies release qualification.
- M1 enables Auto-send, Gmail send, provider writes, or Calendar writes.
- Unknown terms status authorizes periodic crawl.
- Semantic similarity alone authorizes automatic merge.

## Start condition checklist

Before opening the first M1 implementation branch, capture the following in the
work item:

- zero-exit output from `uv run careerops-d0 --root . validate --json` or
  `make verify-m1-full`;
- confirmation that the M-1 discovery/parser/dedup labeling contracts remain
  frozen;
- D0 dataset manifest versions for discovery, parser, and dedup evidence;
- named reviewer and adjudicator evidence;
- PII/secret scan pass evidence;
- source lawfulness, consent, retention, de-identification, and holdout custody
  evidence location;
- first test milestone selected from this handoff;
- explicit statement that recurring crawl remains disabled until M1 crawl-safety
  exit criteria pass.
