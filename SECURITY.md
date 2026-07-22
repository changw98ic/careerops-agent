# Security Policy

## Supported code

Security fixes are applied to the current `main` branch. This repository is pre-release and
does not yet publish supported release branches.

## Reporting a vulnerability

Use GitHub's private vulnerability-reporting flow:

<https://github.com/changw98ic/careerops-agent/security/advisories/new>

Do not include credentials, tokens, personal data, private job-search material, D0 pilot rows,
or unsanitized logs in a public issue, pull request, discussion, or commit. Include only the
minimum redacted reproduction needed to identify the affected component and impact.

If a credential or personal-data artifact was committed or posted publicly, treat it as
compromised: revoke or rotate it at the provider, preserve the incident timestamp and affected
scope, and then use the private reporting channel. Rewriting Git history alone is not a
remediation.

## Safety boundary

The repository must remain fail closed for external side effects. A security report or test
result must never be used to enable Gmail sending, Calendar writes, public webhooks, model
providers, or Release Qualification without the separately required release evidence and human
authorization.
