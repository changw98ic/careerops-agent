# Contact labeling guide v1

## Unit and grouping

One row is one candidate contact found on an official job/Careers/ATS/recruiting page or an already established recruiting thread. Group by company/domain and source page/thread.

## Labels

- `recruiting_contact`: publicly listed for recruiting/job communication or already participating in the thread.
- `ordinary_employee`: named employee without public recruiting purpose.
- `generic_non_recruiting`: support, sales, privacy or unrelated mailbox.
- `invalid`: malformed, obsolete or non-company address.
- `review_required`: evidence cannot safely distinguish the role.

## Hard rules

Never construct, infer, probe or verify an address from a name pattern. Domain match is necessary but not sufficient. A public recruiting address retains source text, URL/thread ID, capture time and hash. Ordinary employees have no send permission.

For `source.kind=established_thread`, store the provider's opaque thread ID in `url_or_thread_ref` using only ASCII letters, digits, `.`, `_`, or `-` (maximum 128 characters). Other source kinds require an absolute source URL. Never put a relative path or URL-shaped value into the thread-ID namespace.

## Review

Guessed contacts and ordinary-employee false positives are zero-tolerance errors. Low-confidence or domain-mismatch cases may only be displayed for review.
