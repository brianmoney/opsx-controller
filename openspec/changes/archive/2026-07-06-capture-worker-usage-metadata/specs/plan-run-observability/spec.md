## ADDED Requirements

### Requirement: Direct stage telemetry extracts worker usage metadata conservatively

Direct stage telemetry records SHALL populate the existing `usage` object when a completed worker invocation exposes recognized token usage metadata in parsed worker JSON or stage log metadata.

The extractor SHALL support the following normalized token fields:

- `input_tokens`: Input or prompt tokens consumed.
- `output_tokens`: Output or completion tokens consumed.
- `cached_input_tokens`: Input tokens served from cache.
- `reasoning_tokens`: Reasoning or thinking tokens consumed.
- `total_tokens`: Total tokens reported by the provider or client.

Each token field SHALL be populated only from a non-negative integer value. Missing, malformed, negative, floating-point, or ambiguous values SHALL leave that field as `null`.

If at least one normalized token field is populated, `usage.usage_available` SHALL be `true` and `usage.usage_source` SHALL identify the winning extraction source. If no token field is populated, `usage.usage_available` SHALL be `false`, `usage.usage_source` SHALL be `null`, and all token fields SHALL remain `null`.

#### Scenario: Structured worker JSON provides full usage

- **WHEN** a direct stage worker completes and its parsed JSON contains recognized non-negative integer values for input, output, cached input, reasoning, and total tokens
- **THEN** the telemetry record populates all corresponding `usage` token fields, sets `usage.usage_available` to `true`, and sets `usage.usage_source` to `"worker_json"`

#### Scenario: Structured worker JSON provides partial usage

- **WHEN** a direct stage worker completes and its parsed JSON contains recognized non-negative integer values for input and output tokens only
- **THEN** the telemetry record populates `usage.input_tokens` and `usage.output_tokens`, leaves cache, reasoning, and total token fields as `null`, sets `usage.usage_available` to `true`, and sets `usage.usage_source` to `"worker_json"`

#### Scenario: Provider reports zero usage

- **WHEN** recognized usage metadata reports a token field as `0`
- **THEN** the telemetry record stores that field as `0` rather than `null` and treats usage as available when at least one token field is known

#### Scenario: Malformed usage does not fabricate counts

- **WHEN** worker JSON or log metadata contains negative, floating-point, non-numeric, or ambiguous token values
- **THEN** those values are ignored, no token count is coerced, and usage remains unavailable unless another recognized token field contains a valid non-negative integer

### Requirement: Direct stage telemetry extracts model identity from usage metadata

Direct stage telemetry records SHALL populate the existing `model` object when parsed worker JSON or recognized stage log metadata exposes reliable model identity fields.

The extractor SHALL populate:

- `model.provider` from a recognized provider field.
- `model.model_id` from a recognized model identifier field.
- `model.model_alias` from a recognized model alias field, when available.

The extractor SHALL NOT infer model identity from stage names, worker names, agent names, freeform prose, or pricing catalog entries. Missing or ambiguous identity fields SHALL remain `null`.

#### Scenario: Structured worker JSON provides model identity

- **WHEN** a completed worker result contains recognized provider and model id metadata
- **THEN** the telemetry record populates `model.provider` and `model.model_id` from those fields and leaves `model.model_alias` as `null` unless an alias field is also present

#### Scenario: Model identity unavailable

- **WHEN** worker JSON and recognized log metadata do not expose reliable model identity
- **THEN** `model.provider`, `model.model_id`, and `model.model_alias` remain `null`

#### Scenario: Ambiguous model identity is not guessed

- **WHEN** the only available model hint is a stage name, worker command, agent name, or freeform log prose
- **THEN** the telemetry record leaves all `model` fields as `null`

### Requirement: Usage extraction source precedence is deterministic

When both parsed worker JSON and stage log metadata are available, direct stage telemetry SHALL prefer recognized usage and model metadata from parsed worker JSON. Stage log metadata SHALL be used as a fallback only when parsed worker JSON contains no usable token counts or model identity fields for the corresponding object.

When stage log metadata is used for token usage, `usage.usage_source` SHALL be `"log_metadata"`. When parsed worker JSON is used for token usage, `usage.usage_source` SHALL be `"worker_json"`.

#### Scenario: Worker JSON wins over conflicting log metadata

- **WHEN** parsed worker JSON contains valid token usage and the stage log contains different recognized token usage
- **THEN** the telemetry record uses the worker JSON values and sets `usage.usage_source` to `"worker_json"`

#### Scenario: Log metadata fills usage when worker JSON has none

- **WHEN** parsed worker JSON contains no usable token usage and the stage log contains recognized token usage metadata
- **THEN** the telemetry record populates usage from the log metadata and sets `usage.usage_source` to `"log_metadata"`

### Requirement: Usage extraction does not alter stage completion semantics

Usage and model metadata extraction SHALL be best-effort. Extraction failures, malformed metadata, unknown client formats, unreadable log metadata, or missing usage SHALL NOT change the stage `status`, SHALL NOT cause a stage invocation to fail, and SHALL NOT prevent telemetry from being written with default-unavailable usage values.

Telemetry records for timeouts, spawn errors, and invalid worker output SHALL keep `usage.usage_available` as `false`, `usage.usage_source` as `null`, all token fields as `null`, and model fields as `null` unless reliable metadata was already captured before the failure.

#### Scenario: Unknown output format remains successful

- **WHEN** a direct stage worker completes successfully but exposes no recognized usage or model metadata
- **THEN** the stage remains completed, telemetry is written, usage is marked unavailable, and model fields remain `null`

#### Scenario: Timeout keeps default-unavailable usage

- **WHEN** a direct stage worker times out
- **THEN** the telemetry record keeps usage unavailable and model fields `null`, and the stage outcome remains `"timeout"`

#### Scenario: Invalid worker output keeps default-unavailable usage

- **WHEN** a direct stage worker returns output that cannot be parsed as valid stage JSON
- **THEN** the telemetry record keeps usage unavailable and model fields `null`, and the stage outcome remains `"invalid_output"`
