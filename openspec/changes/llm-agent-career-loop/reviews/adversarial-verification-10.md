# Independent adversarial verification — 2026-07-27

This report records ten separate read-only checks against the current repository and the `llm-agent-career-loop` planning artifacts. A passing check means the tested safety/contract property held; it does not mean the new feature is implemented. A failing integration check is an intentional finding that the new spec still has work to close.

The independent sub-agent attempt was not counted: the review thread exceeded two 180-second waits and was shut down without a result. The ten counted passes below are deterministic local checks with non-overlapping attack surfaces.

## Results

| Pass | Attack surface | Result | Evidence |
|---|---|---|---|
| V1 | OpenSpec artifact validity | PASS | `openspec validate llm-agent-career-loop --type change --strict --json`: 1 item passed, 0 issues |
| V2 | Model tool binding and unsafe capability defaults | PASS | No actual `tools` field in `anthropic_compat.py`; `Settings` defaults model provider/OAuth/external writes/auto-send to disabled; no unsafe `=true` flag found in `.env`/Compose |
| V3 | Model gateway schema/429/usage behavior | PASS | `tests/unit/test_model_gateway.py`, `test_model_gateway_schema.py`, `test_llm_rate_limit_429.py`, `test_llm_metrics.py`: 46 passed |
| V4 | Model egress and prompt-injection boundary | PASS | `tests/unit/test_section3_security.py`: 39 passed |
| V5 | Crawl HTTP SSRF/rate-limit/circuit boundary | PASS | `tests/unit/test_http_fetcher.py`, `test_circuit_breaker.py`: 56 passed |
| V6 | Crawl Temporal/activity contract | PASS | `tests/contract/test_section5_crawl_slice.py`, `tests/unit/test_s5_temporal_wiring.py`: 25 passed |
| V7 | Page run-now → Temporal → real crawl dispatch | PASS after repair | `tests/contract/test_section5_crawl_slice.py` and `tests/unit/test_s5_temporal_wiring.py`; run-now creates/reuses a pending run and starts the stable workflow ID |
| V8 | Ego in the formal runtime path | PASS after repair | `src/careerops/infrastructure/temporal/ego_browser_executor.py`, explicit `executor_mode=ego`, and `tests/unit/test_ego_browser_executor.py`: SSRF preflight, bounded capture, missing dependency, and no HTTP fallback |
| V9 | Page inbox LLM injection and interview-prep implementation | PASS after repair | `InboxProjectionService` receives the shared model client; `application/agent_services.py` and Agent routes provide schema-bound resume/interview execution; `tests/unit/test_agent_services.py` covers model, fallback, idempotency, and review |
| V10 | Resume-review LLM path and durable Agent-run UI/API | PASS after repair | `domain/agent_runs.py`, `agent_runs`/`agent_run_reviews` migrations, `StructuredModelRequest` services, candidate-scoped routes, and the Agent contract tests |

## Interpretation

- Safety and existing gateway/crawl contracts are green in the tested surfaces.
- The original V7–V10 gaps were repaired and re-run locally. The remaining unchecked items are pilot, UI breadth, stale/correction workflows, and independent/release qualification evidence.
- These passes are code-level/harness evidence only; they do not substitute for a real Mimo pilot, quality sample review, or release qualification.
- No key, raw model content, browser session material, or private dataset was emitted by these checks.
