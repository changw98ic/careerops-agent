# Discovery / Parser labeling guide v1

## Unit and grouping

One row is one frozen page/API response. Group by verified company + registrable domain so related Careers, ATS and posting pages cannot cross splits. Dynamic/login/CAPTCHA-only content is unsupported for MVP and must be declared before evaluation.

## Labels

- `official_careers_entry`: the company-controlled or explicitly linked official Careers entry, otherwise null.
- `adapter`: the narrowest adapter that correctly represents the source; never label a browser-rendered page as static HTML.
- `postings`: authoritative source ID/URL/company/title/location/description presence and active state.
- `terms_status`: `blocked` forbids collection, `unknown` permits only the plan's one low-frequency discovery request, `allowed` follows normal crawl policy.

## Rules

Use visible/source facts, not a model guess. Strip tracking parameters for canonical URL but retain the original source URL in the manifest. A missing source field is null/absent truth, not a parser error. A closed role remains a valid sample with `active=false`.

## Required slices

Greenhouse, Lever and Ashby each; JSON-LD, Sitemap and static HTML; duplicate listing; pagination; missing location; closed/reopened; 403/429; malformed structured data; unsupported dynamic page.

## Evidence and review

Record snapshot hash, capture time, source URL, robots/terms decision and annotator IDs. Disagreements on official ownership or active state require adjudication before sealing.
