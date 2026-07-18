# Dedup labeling guide v1

## Unit and grouping

One row is a pair of source postings. Group by company plus candidate-job cluster. At least 40% of the final set must be hard negatives: similar title/location but materially different team, level, requisition or description.

## Labels

- `same_job`: evidence shows both postings are the same requisition/logical opening.
- `different_job`: evidence shows distinct openings.
- `review_required`: source evidence is insufficient; this is not counted as a safe automatic merge.

## Decision order

Prefer shared source/requisition ID, then authoritative cross-links/canonical URL, then exact company/title/location plus materially matching description. Title similarity alone is never enough. Multiple headcount under one requisition is still one logical job; two requisitions with identical copy are different unless the source links them.

## Edge cases

Reposted role with new requisition, localization mirrors, staffing-agency copy, seniority-only difference, remote versus onsite variant, evergreen/talent-pool posting and one source closed while another is active.

## Evidence and review

Annotators cite the decisive fields. `same_job` disagreements are always adjudicated because false merge is harder to reverse than a missed merge.
