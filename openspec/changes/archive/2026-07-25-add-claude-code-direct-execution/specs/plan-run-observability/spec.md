## ADDED Requirements

### Requirement: Claude Code result envelopes are a recognized usage source

Direct stage telemetry SHALL recognize the Claude Code result envelope as a usage and model metadata source.

When the envelope is the winning usage source, telemetry SHALL set `usage.usage_source` to `"claude_result_json"`.

Envelope extraction SHALL read the selected envelope object only, SHALL NOT aggregate values across intermediate streamed messages, and SHALL apply the same conservative validation used for other sources: only non-negative integer token counts and non-empty string model identity fields are accepted.

#### Scenario: Envelope supplies usage for a Claude Code stage

- **WHEN** a direct stage completes under the `claude-code` adapter and the selected result envelope carries valid token counts
- **THEN** the telemetry record populates usage from the envelope and sets `usage.usage_source` to `"claude_result_json"`

#### Scenario: Streamed intermediate usage does not displace envelope totals

- **WHEN** a direct stage log contains intermediate streamed messages carrying partial token counts followed by a final result envelope
- **THEN** the telemetry record uses the final envelope totals rather than any intermediate partial values

#### Scenario: Envelope model identity is recorded

- **WHEN** the selected result envelope carries model identity fields and parsed worker JSON carries none
- **THEN** the telemetry record populates model identity from the envelope

### Requirement: Cache-creation input tokens are a recognized token field

Token field normalization SHALL map `cache_creation_input_tokens` to the normalized cached input token field, alongside the already-recognized `cache_read_input_tokens`.

#### Scenario: Cache-creation tokens populate cached input usage

- **WHEN** a recognized usage source reports `cache_creation_input_tokens` as a non-negative integer and no other cached input field
- **THEN** the telemetry record populates the normalized cached input token field from that value

## MODIFIED Requirements

### Requirement: Usage extraction source precedence is deterministic

When multiple usage sources are available, direct stage telemetry SHALL prefer recognized usage and model metadata from parsed worker JSON. The Claude Code result envelope SHALL be used when parsed worker JSON contains no usable token counts or model identity fields for the corresponding object. Stage log metadata SHALL be used as a fallback only when neither parsed worker JSON nor a result envelope provides usable values for the corresponding object.

Consumers SHALL apply source precedence in this order:

- Parsed worker JSON usage and model metadata.
- Claude Code result envelope usage and model metadata.
- Recognized stage log metadata usage and model metadata.
- OpenCode plugin sidecar usage and model metadata.
- Unavailable usage and unknown model identity.

When stage log metadata is used for token usage, `usage.usage_source` SHALL be `"log_metadata"`. When parsed worker JSON is used for token usage, `usage.usage_source` SHALL be `"worker_json"`. When the result envelope is used for token usage, `usage.usage_source` SHALL be `"claude_result_json"`.

#### Scenario: Worker JSON wins over conflicting log metadata

- **WHEN** parsed worker JSON contains valid token usage and the stage log contains different recognized token usage
- **THEN** the telemetry record uses the worker JSON values and sets `usage.usage_source` to `"worker_json"`

#### Scenario: Log metadata fills usage when worker JSON has none

- **WHEN** parsed worker JSON contains no usable token usage and the stage log contains recognized token usage metadata
- **THEN** the telemetry record populates usage from the log metadata and sets `usage.usage_source` to `"log_metadata"`

#### Scenario: Worker JSON wins over a conflicting result envelope

- **WHEN** parsed worker JSON contains valid token usage and the selected result envelope contains different valid token usage
- **THEN** the telemetry record uses the worker JSON values and sets `usage.usage_source` to `"worker_json"`

#### Scenario: Result envelope outranks log metadata

- **WHEN** parsed worker JSON contains no usable token usage and both a result envelope and other recognized log metadata carry token counts
- **THEN** the telemetry record uses the envelope values and sets `usage.usage_source` to `"claude_result_json"`

### Requirement: OpenCode plugin usage has deterministic source precedence

OpenCode plugin sidecar usage SHALL be a fallback source after existing direct-stage usage extraction sources. Consumers SHALL apply source precedence in this order:

- Parsed worker JSON usage and model metadata.
- Claude Code result envelope usage and model metadata.
- Recognized stage log metadata usage and model metadata.
- OpenCode plugin sidecar usage and model metadata.
- Unavailable usage and unknown model identity.

When the sidecar is the winning usage source, telemetry SHALL set `usage.usage_source = "opencode_plugin"`. The sidecar SHALL NOT override token usage or model identity obtained from higher-precedence worker JSON, result envelope, or recognized log metadata.

#### Scenario: Worker JSON remains highest precedence

- **WHEN** parsed worker JSON contains valid token usage and the OpenCode plugin sidecar contains different valid token usage
- **THEN** telemetry uses the worker JSON usage and sets `usage.usage_source = "worker_json"`

#### Scenario: Sidecar fills usage after existing sources have none

- **WHEN** parsed worker JSON and recognized log metadata contain no usable token counts and the OpenCode plugin sidecar contains a valid selected record with normalized token counts
- **THEN** telemetry populates usage from the sidecar and sets `usage.usage_source = "opencode_plugin"`
