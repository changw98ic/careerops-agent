# Calendar labeling guide v1

## Unit

One row is a scheduling request, policy configuration, all-calendar busy intervals and optional injected fault. Group related timezone/request templates together.

## Time truth

Use IANA zones and exact UTC instants. Include DST gap/fold, UTC offsets, half-hour/45-minute zones, date rollover and ambiguous CST. Ambiguous text is an explicit abstain, not a guessed slot.

## Policy truth

Apply minimum notice, buffers, available windows, daily/weekly limits and proposal expiry at execution time. A conflict visible before the final FreeBusy result makes `create_allowed=false` and provider effect count zero.

For a conflict injected after final FreeBusy but before insert, label the physical event outcome honestly. The required safety result is conflict detection, manual queue and zero confirmation email—not a false assertion that Google offered atomic reservation.

Timeout-with-success, after-call-before-commit and duplicate click may create at most one event. Every write is human-triggered, targets the dedicated CareerOps Interviews calendar and has no external attendees.

## Quality

All conflict/race outcomes are double-labeled. Structured/time exact agreement must be at least 0.95 before scale-up.
