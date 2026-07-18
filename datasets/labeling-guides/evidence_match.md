# Evidence Match labeling guide v1

## Unit and grouping

One row pairs one atomic job requirement with zero or one frozen Candidate Evidence reference. Group by job and evidence bundle so adjacent requirements or repository snippets cannot leak across splits.

## Labels

- `strong`: evidence directly demonstrates the required capability at the claimed level.
- `partial`: evidence covers a meaningful subset but not the full requirement/level.
- `transferable`: evidence demonstrates a relevant adjacent skill and the response must describe it as transferable.
- `unsupported`: no valid evidence supports a positive claim.
- `hard_fail`: explicit candidate fact contradicts a mandatory requirement.

## Rules

Evidence must point to repository, commit, path, optional symbol and snippet hash or an equivalently frozen user-approved artifact. Resume wording without underlying evidence cannot create `strong`. Tool-name overlap alone is insufficient; judge demonstrated behavior and scope.

## Review

Annotators see the requirement and frozen evidence, not a model match score. Any `strong` row without a valid evidence reference is invalid and must be corrected before sealing.
