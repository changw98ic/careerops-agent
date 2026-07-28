# Agent console budget contract

Counters are server-owned and charged atomically before work. Redis/limiter unavailability is `BUDGET_DECISION_UNAVAILABLE` and denies the operation.

| Counter key | Window/limit | Charge |
| --- | --- | --- |
| `candidate:{id}:provider_calls:all:hour:{UTC}` | 10 calls/hour **across all providers** | one reservation per provider connection attempt, including retry; this is a candidate aggregate, never a per-provider allowance |
| `candidate:{id}:input_tokens:day:{UTC}` | 100,000/day | reserved input-token budget before provider call; unused reservation is released only if no connection occurs |
| `candidate:{id}:output_tokens:day:{UTC}` | 20,000/day | actual/declared bounded output reservation; never negative |
| `run:{run_id}:model_attempts` | 5/run | every model activity attempt, including timeout/429/repair |
| `candidate:{id}:browser_tasks` | 2 concurrent | acquired before sidecar task, released only after task cleanup |
| `source:{source_id}:requests:{UTC minute}` | 60/minute | every admitted HTTP/browser subrequest |
| `run:{run_id}:response_bytes` | 2 MiB/source response | bytes admitted before normalization |
| `candidate:{id}:exports:{UTC day}` | 10/day | one confirmed manual export/copy receipt |

Reservation order is provider/source policy → candidate counters → run counter → work. Any failed reservation rolls back all reservations in the same limiter transaction. Once a provider connection, browser subrequest, or export starts, its attempt is charged even if it fails or is cancelled; retries consume a new attempt. A budget denial is audited with counter key class, limit, observed value, and retry time, never raw content. Counter keys are TTL-bound to the end of their window plus 24 hours and cannot be selected by a client.

The provider name is recorded only as bounded attribution in the audit/metrics
projection. It does not create another quota bucket and cannot raise the
candidate aggregate of 10 calls/hour. Per-provider policy may impose a lower
limit, but an unknown provider limit denies rather than widening the aggregate.

The plan's acceptance test must run concurrent reservations, Redis outage, retry exhaustion, cancellation, and window rollover. It must prove no request, model call, browser task, or export occurs after a denied reservation.
