# Remote eligibility labeling guide v1

## Unit and candidate

One row is a JD/location case evaluated for a candidate in Chengdu, China (`Asia/Shanghai`). Group by company/domain/job family; paraphrases stay together.

## Labels

- `china_eligible`: explicit location/employment facts allow work from China.
- `china_ineligible`: explicit country, residency, timezone, payroll or authorization rule excludes China.
- `global_remote`: explicit worldwide remote with no contradictory restriction.
- `apac_ambiguous`: APAC/remote language does not prove China eligibility.
- `unknown`: evidence is missing or contradictory.

`hard_gate=fail` only follows explicit disqualifying evidence. `APAC`, `CST`, “remote” or a convenient timezone alone never proves eligibility. CST without a named region is ambiguous.

## Required evidence

Copy the minimal decisive span and source URL/hash. Do not infer employer-of-record availability, visa sponsorship, tax status or cross-border employment.

## Safety review

Every determinate hard-false pilot/holdout row is double-labeled. Disagreement resolves toward `unknown/review_required` until source evidence is adjudicated.
