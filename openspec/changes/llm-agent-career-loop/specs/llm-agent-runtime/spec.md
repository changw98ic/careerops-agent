## ADDED Requirements

### Requirement: All career Agents use one guarded model runtime
Resume review, job matching, and interview preparation SHALL use `StructuredModelClient` through capability-specific request builders. No Agent SHALL call a provider SDK directly, bind tools, read provider credentials, or write application state or external providers.

#### Scenario: Agent capability is enabled
- **WHEN** the user configures a supported model endpoint, key, model name, and explicitly releases the requested capability
- **THEN** the Agent uses the shared model runtime with the capability's prompt version, schema, timeout, token budget, and trace identity

#### Scenario: Agent attempts a tool call
- **WHEN** a model response or prompt requests a tool, policy change, recipient change, credential, or external write
- **THEN** the runtime rejects or ignores the request and returns only review-only structured data

### Requirement: Model egress is minimized and untrusted content is isolated
Each model request SHALL contain only the minimum selected candidate evidence and job/application context required by its capability. Job pages, browser output, email excerpts, and other external content SHALL be fenced as untrusted data. Raw resume bytes, credentials, unrelated private material, and raw browser/session content MUST NOT leave the local boundary.

#### Scenario: Resume review is requested
- **WHEN** the user selects a resume version, target job, and evidence set
- **THEN** the request contains only bounded extracted text, selected evidence references, and the minimum target-job content needed for review

#### Scenario: A request contains forbidden material
- **WHEN** the request builder detects credentials, raw attachment bytes, unrelated mailbox content, or unselected private evidence
- **THEN** it removes or rejects the material before provider egress and records a non-sensitive denied-egress outcome

#### Scenario: External content contains prompt injection
- **WHEN** untrusted job or browser content contains model-directed instructions
- **THEN** the runtime labels it as data, prevents it from changing the system instruction or tools, and preserves the review-only boundary

### Requirement: Structured output is validated and may abstain
Every enabled Agent request SHALL identify a versioned JSON Schema. The runtime SHALL validate the provider result, allow at most one bounded repair attempt, and return an explicit invalid/unavailable/abstained outcome when validation or the budget fails.

#### Scenario: Provider returns valid output
- **WHEN** the response parses and satisfies the capability schema
- **THEN** the runtime returns typed structured data with model ID, schema/prompt versions, confidence if supplied, and `review_only=true`

#### Scenario: Provider returns invalid output twice
- **WHEN** the initial response and one repair attempt fail parsing or schema validation
- **THEN** the runtime returns a safe failure/abstention and does not pass arbitrary text to downstream policy or state transitions

### Requirement: Disabled or unavailable models preserve deterministic behavior
The default model provider SHALL remain disabled. When the model is disabled, unavailable, rate-limited, or invalid, deterministic parsing, hard filtering, evidence matching, package validation, and manual preparation SHALL remain usable and the UI SHALL distinguish unavailable from a low score.

#### Scenario: Default configuration is used
- **WHEN** no model provider is explicitly configured and released
- **THEN** no external model request is made and the deterministic path returns an explicit model-disabled status where semantic output is unavailable

#### Scenario: Provider becomes unavailable after a crawl
- **WHEN** a model call fails after jobs or resume inputs are already persisted
- **THEN** the system preserves the inputs and deterministic results, records an abstained Agent result, and allows a later bounded retry

### Requirement: Model usage and Agent traces are bounded and non-content-bearing
The runtime SHALL record aggregate input/output token counts, latency, outcome, model/schema/prompt versions, safe trace ID, and Agent run identity. It SHALL NOT record raw prompts, raw responses, secrets, or user content in metrics or default diagnostic traces.

#### Scenario: Agent call succeeds
- **WHEN** a provider call returns a valid result
- **THEN** `/metrics` exposes bounded aggregate usage and outcome counters and the result is correlated to the request trace without content labels

#### Scenario: Agent call fails
- **WHEN** a provider call times out, is rate-limited, or returns invalid output
- **THEN** the same bounded telemetry records the failure class and latency without exposing provider payloads or credentials
