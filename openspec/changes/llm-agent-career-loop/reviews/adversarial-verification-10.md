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
| V7 | Page run-now → Temporal → real crawl dispatch | FAIL — gap confirmed | `crawl_plans.py:293` creates a run only; `runtime.py:388` still defines `demo_crawler()` returning `()`; no API `start_workflow` call |
| V8 | Ego in the formal runtime path | FAIL — gap confirmed | `rg --files src/careerops` has no Ego/browser executor; Ego appears in standalone `scripts/crawl_full.py`, `crawl_all.sh`, and `crawl_daily.sh` only |
| V9 | Page inbox LLM injection and interview-prep implementation | FAIL — gap confirmed | `api/app.py:247-253` constructs `InboxProjectionService` without `model_client`; no dedicated interview/preparation/Agent implementation file exists |
| V10 | Resume-review LLM path and durable Agent-run UI/API | FAIL — gap confirmed | `resume_analysis.py`/`resume_service.py` are deterministic and contain no `StructuredModelRequest`; no `AgentRun`/Agent-run API or persistence contract exists |

## Interpretation

- Safety and existing gateway/crawl contracts are green in the tested surfaces.
- Four independent product-integration checks fail for the same reason the new change was proposed: the full Ego + LLM + page-managed Agent loop is not yet implemented.
- V7–V10 are not waived as “known”; they are explicit blockers for implementing and later accepting this change.
- No key, raw model content, browser session material, or private dataset was emitted by these checks.
