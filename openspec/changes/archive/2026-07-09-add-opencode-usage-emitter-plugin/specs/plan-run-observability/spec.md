## ADDED Requirements

### Requirement: OpenCode adapter installs a usage emitter plugin

The OpenCode adapter SHALL include an OpenCode plugin that can emit usage sidecar records matching the OpenCode plugin usage sidecar contract.

The OpenCode adapter installer SHALL deploy the plugin into the installed OpenCode configuration used by runtime OpenCode sessions. Installer verification SHALL confirm that the deployed plugin file is present.

The plugin SHALL be safe to install globally because it SHALL NOT write sidecar files or alter OpenCode behavior when `OPSX_USAGE_PATH` is absent or empty.

#### Scenario: Installer deploys plugin

- **WHEN** an operator runs the OpenCode adapter installer with verification enabled
- **THEN** the installer deploys the usage emitter plugin and verification confirms the deployed plugin file exists

#### Scenario: Plugin is inert without usage path

- **WHEN** OpenCode starts without `OPSX_USAGE_PATH` in the environment
- **THEN** the usage emitter plugin does not create usage files and does not alter OpenCode behavior

### Requirement: Usage emitter writes only scoped sidecar records

When `OPSX_USAGE_PATH` is present and non-empty, the usage emitter plugin SHALL append records only after validating the stage identity environment variables required by the sidecar contract: `OPSX_PLAN_NAME`, `OPSX_RUN_ID`, `OPSX_CHANGE_ID`, `OPSX_STAGE`, and `OPSX_ROUND`.

`OPSX_STAGE` SHALL be one of `"implement"`, `"review"`, or `"archive"`. `OPSX_ROUND` SHALL parse as a positive base-10 integer. If any required identity value is missing or invalid, the plugin SHALL NOT append a sidecar record for that event.

Each appended record SHALL be a single JSON object on one line in the file at `OPSX_USAGE_PATH` and SHALL include the normalized fields defined by the OpenCode plugin usage sidecar contract.

#### Scenario: Valid stage identity produces scoped record

- **WHEN** `OPSX_USAGE_PATH` and all required stage identity variables are present and valid, and OpenCode emits a token-bearing usage event
- **THEN** the plugin appends one JSONL record whose plan name, run id, change id, stage, and round match the environment values

#### Scenario: Invalid stage identity suppresses writes

- **WHEN** `OPSX_USAGE_PATH` is set but `OPSX_STAGE` is missing, unsupported, or `OPSX_ROUND` is not a positive integer
- **THEN** the plugin does not append a sidecar record for the event

### Requirement: Usage emitter classifies incremental and final usage events

The usage emitter plugin SHALL classify token-bearing OpenCode progress or message update events as `event_type = "incremental"` records. The plugin SHALL classify final turn or session completion events as `event_type = "final"` records when those events expose usable final usage.

The plugin SHALL include `emitted_at` as an ISO-8601 UTC timestamp for every record and SHALL set `schema_version = 1`.

The plugin SHALL NOT emit a final record solely because a session event occurred; a final record requires usable usage or model metadata from the event payload.

#### Scenario: Message update emits incremental usage

- **WHEN** OpenCode emits a token-bearing message update event during a scoped stage invocation
- **THEN** the plugin appends a sidecar record with `event_type = "incremental"`, `schema_version = 1`, and an `emitted_at` timestamp

#### Scenario: Session completion emits final usage

- **WHEN** OpenCode emits a final session or turn event with usable final usage during a scoped stage invocation
- **THEN** the plugin appends a sidecar record with `event_type = "final"`, `schema_version = 1`, and the final usage fields available from the event

### Requirement: Usage emitter normalizes model and usage fields conservatively

The usage emitter plugin SHALL normalize available OpenCode event payload fields into:

- `provider`
- `model_id`
- `model_alias`
- `input_tokens`
- `output_tokens`
- `cached_input_tokens`
- `reasoning_tokens`
- `total_tokens`
- `request_count`
- `latency_ms`

Token count, request count, and latency fields SHALL be written only when the event payload provides non-negative integer values. Missing, malformed, negative, floating-point, non-numeric, or ambiguous values SHALL be written as `null` or omitted before serialization in a way that produces `null` in the sidecar record.

The plugin SHALL NOT infer usage from freeform text, synthesize token totals from partial values unless OpenCode reports a reliable total, or coerce unavailable values into zero.

#### Scenario: Complete usage payload is normalized

- **WHEN** an OpenCode usage event contains provider, model id, input tokens, output tokens, cached input tokens, reasoning tokens, total tokens, request count, and latency as valid values
- **THEN** the plugin appends a sidecar record with those normalized fields populated as non-negative integers or strings

#### Scenario: Malformed numeric values remain unavailable

- **WHEN** an OpenCode usage event contains negative, floating-point, non-numeric, or ambiguous token values
- **THEN** the plugin does not coerce those values and writes the affected normalized fields as `null`

#### Scenario: Zero usage is preserved

- **WHEN** an OpenCode usage event reports a token field as integer `0`
- **THEN** the plugin writes `0` for that field rather than `null`

### Requirement: Usage emitter failures do not alter OpenCode behavior

The usage emitter plugin SHALL treat usage emission as best-effort observability. Unsupported event shapes, missing usage fields, failed directory creation, failed file append operations, serialization errors, or other plugin emission errors SHALL NOT fail OpenCode startup, SHALL NOT fail a model request, SHALL NOT alter tool execution, and SHALL NOT change permission behavior.

When the plugin cannot emit a valid record for an event, it SHALL skip that event without fabricating usage data.

#### Scenario: Sidecar append failure is non-fatal

- **WHEN** `OPSX_USAGE_PATH` points to a location that cannot be written
- **THEN** OpenCode behavior continues unchanged and the plugin does not report fabricated usage

#### Scenario: Unsupported event shape is ignored

- **WHEN** OpenCode emits an event shape that the plugin does not recognize as usage-bearing
- **THEN** the plugin skips the event without writing a malformed sidecar record
