# Email labeling guide v1

## Privacy and unit

One row is one authorized, redacted recruiting thread. Group by thread and recruiter/sender organization. Remove personal addresses, phone numbers, tracking/meeting tokens, signatures and irrelevant quoted history before annotation; raw mail never enters Git.

## Primary category

Choose the action-dominant category: `screen`, `interview`, `rejection`, `offer`, `document_request`, `salary`, `visa`, `assessment`, `follow_up`, `unknown` or `non_recruiting`. Multiple topics use the highest-risk actionable category and list all high-risk flags.

## High-risk flags

Offer, document, salary, visa, relocation, tax, background check, identity, bank and suspicious sender always require review. A euphemism or attachment does not reduce risk. Unknown sender/application linkage is review-required.

## Time labels

Record source text, exact UTC instant and source IANA timezone only when determinate. Preserve DST fold/gap and ambiguous abbreviations as abstain/review; never assume CST means China or US Central.

## Quality

All high-risk rows are double-labeled. Synthetic threads may exercise development taxonomy but final classification/high-risk metrics use real redacted threads with lawful source authorization.
