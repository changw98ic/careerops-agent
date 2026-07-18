# ADR 0008: Normalize compensation without guessed exchange rates

- Status: Accepted
- Date: 2026-07-17

## Context

Job descriptions mix currencies, gross/net wording, hourly/monthly/annual periods, ranges and equity. No real-time FX provider is in MVP, so a single guessed converted salary would be irreproducible and misleading.

## Decision

Parsed compensation preserves source facts:

- original text and evidence span;
- ISO 4217 currency when explicit;
- minimum/maximum amount as decimals;
- period: hour, day, month, year or unknown;
- gross/net/unknown, base/total/unknown and full-time-equivalent assumptions;
- parser version and confidence.

Period normalization is allowed only with explicit, versioned assumptions from user configuration, such as work hours per week and paid weeks per year. The calculation stores every input and formula.

Cross-currency comparison uses only a user-supplied static FX snapshot containing base currency, quote currency, decimal rate, effective date, source note and content hash. The snapshot is immutable once referenced by a score. No network FX lookup or inferred parity occurs in MVP.

If currency, period or a required FX snapshot is missing/stale under the configured policy, the salary dimension is `unknown` and contributes neutral weight. It cannot cause a hard rejection or positive boost. Equity, bonus and benefits are displayed separately and are not converted into base salary.

## Consequences

- Salary ranking may be less complete but is reproducible and honest.
- Updating an FX snapshot creates new match results; old decisions retain their original snapshot reference.
- A future real-time FX adapter needs a new ADR and source/reliability tests.

## Verification

Fixtures cover ranges, one-sided amounts, hourly/monthly/annual conversions, gross/net ambiguity, multiple currencies, missing/stale FX, decimal rounding and replay with an old snapshot.
