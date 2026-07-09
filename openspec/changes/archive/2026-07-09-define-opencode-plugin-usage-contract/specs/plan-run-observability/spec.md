## ADDED Requirements

### Requirement: OpenCode plugin usage sidecars are scoped to one stage invocation

When `opsx-plan` later enables OpenCode plugin usage capture for a stage invocation, it SHALL provide a sidecar path and stage identity through environment variables before starting the OpenCode worker process.

The plugin execution context SHALL use these environment variables:

- `OPSX_USAGE_PATH`: Absolute or repository-relative path to the stage usage sidecar JSONL file.
- `OPSX_PLAN_NAME`: Plan manifest name.
- `OPSX_RUN_ID`: Stable telemetry run id for the current plan run.
- `OPSX_CHANGE_ID`: OpenSpec change id being processed by this stage.
- `OPSX_STAGE`: Stage name. SHALL be one of `"implement"`, `"review"`, or `"archive"`.
- `OPSX_ROUND`: One-indexed stage round as a base-10 integer string.

When `OPSX_USAGE_PATH` is absent or empty, the plugin SHALL be inert and SHALL NOT create usage files. When `OPSX_USAGE_PATH` is present, the sidecar SHALL be scoped to exactly one stage invocation and SHALL be append-only JSONL.

#### Scenario: Stage invocation provides plugin usage context

- **WHEN** an OpenCode direct stage invocation is configured for plugin usage capture
- **THEN** the OpenCode process environment contains `OPSX_USAGE_PATH`, `OPSX_PLAN_NAME`, `OPSX_RUN_ID`, `OPSX_CHANGE_ID`, `OPSX_STAGE`, and `OPSX_ROUND` for that exact stage invocation

#### Scenario: Plugin is inert outside opsx-plan stage runs

- **WHEN** OpenCode starts without `OPSX_USAGE_PATH`
- **THEN** the plugin does not write a sidecar file and does not alter OpenCode behavior

### Requirement: OpenCode plugin usage sidecar records have a normalized JSONL shape

Each OpenCode plugin usage sidecar record SHALL be a single JSON object on its own line and SHALL include:

- `schema_version` (integer): Sidecar schema version. Initial version is `1`.
- `emitted_at` (string): ISO-8601 UTC timestamp when the plugin emitted the record.
- `event_type` (string): Event classification. SHALL be `"incremental"` or `"final"`.
- `plan_name` (string): Value of `OPSX_PLAN_NAME`.
- `run_id` (string): Value of `OPSX_RUN_ID`.
- `change_id` (string): Value of `OPSX_CHANGE_ID`.
- `stage` (string): Value of `OPSX_STAGE`.
- `round` (integer): Parsed value of `OPSX_ROUND`.
- `provider` (string or null): API provider name when known.
- `model_id` (string or null): Canonical model identifier when known.
- `model_alias` (string or null): Operator-configured model alias when known.
- `input_tokens` (integer or null): Input or prompt tokens consumed.
- `output_tokens` (integer or null): Output or completion tokens consumed.
- `cached_input_tokens` (integer or null): Input tokens served from cache.
- `reasoning_tokens` (integer or null): Reasoning or thinking tokens consumed.
- `total_tokens` (integer or null): Total tokens reported by OpenCode or the provider.
- `request_count` (integer or null): Count of model requests represented by this record when known.
- `latency_ms` (integer or null): Model latency represented by this record when known.

Token count, request count, and latency fields SHALL be non-negative integers when present. Missing, malformed, negative, floating-point, non-numeric, or ambiguous numeric values SHALL be treated as `null` by consumers.

#### Scenario: Final sidecar record contains normalized usage

- **WHEN** the OpenCode plugin observes final usage for a stage invocation
- **THEN** it appends a JSONL record with `event_type = "final"`, matching stage identity fields, normalized model identity fields, and any available normalized token fields

#### Scenario: Malformed numeric fields are unavailable

- **WHEN** a sidecar record contains a negative, floating-point, non-numeric, or ambiguous value for a token count, request count, or latency field
- **THEN** consumers ignore that field and do not coerce it into an integer

### Requirement: OpenCode plugin usage consumers select final records before incremental records

Consumers of an OpenCode plugin usage sidecar SHALL consider only valid records whose `schema_version`, `plan_name`, `run_id`, `change_id`, `stage`, and `round` match the current stage invocation.

When at least one valid `"final"` record exists, consumers SHALL select the latest valid final record by `emitted_at` and ignore incremental records for token usage. When no valid final record exists and the stage did not complete normally, consumers MAY select the latest valid `"incremental"` record by `emitted_at` as best-effort partial usage. When no valid final record exists for a normally completed stage, consumers SHALL treat sidecar usage as unavailable rather than relying on incremental usage.

If the selected record has at least one non-null normalized token count, telemetry SHALL set `usage.usage_available = true` and `usage.usage_source = "opencode_plugin"` when the sidecar wins source precedence. If no normalized token count is available, usage SHALL remain unavailable.

#### Scenario: Latest final record wins

- **WHEN** a sidecar contains multiple valid incremental records and at least one valid final record for the current stage invocation
- **THEN** the consumer uses the latest valid final record for token usage and ignores incremental token counts

#### Scenario: Timeout can use latest incremental record

- **WHEN** a stage times out before a final sidecar record is emitted and the sidecar contains a valid incremental record with normalized token counts
- **THEN** the consumer may populate usage from the latest valid incremental record with `usage.usage_source = "opencode_plugin"`

#### Scenario: Completed stage without final usage remains unavailable

- **WHEN** a stage completes normally and the sidecar contains only incremental records
- **THEN** the consumer treats sidecar usage as unavailable rather than using partial incremental counts

### Requirement: OpenCode plugin usage sidecar handling is conservative and non-fatal

Missing sidecar files, unreadable sidecar files, empty sidecar files, malformed JSONL lines, unsupported sidecar schema versions, identity mismatches, unknown event types, and records with no usable token counts SHALL NOT fail a stage invocation, alter the stage `status`, prevent telemetry writing, or fabricate token usage.

Consumers SHALL ignore invalid sidecar records independently and MAY use other valid records from the same sidecar. If no valid record remains, the telemetry record SHALL preserve default-unavailable usage unless a higher-precedence source provides usage.

#### Scenario: Malformed sidecar does not fail telemetry

- **WHEN** a sidecar file contains malformed JSONL or unsupported records
- **THEN** the consumer ignores invalid records, keeps the stage outcome unchanged, and writes telemetry with unavailable usage unless another valid usage source exists

#### Scenario: Mismatched identity is ignored

- **WHEN** a sidecar record has a different `plan_name`, `run_id`, `change_id`, `stage`, or `round` than the current stage invocation
- **THEN** the consumer ignores that record and does not use it for telemetry usage or model identity

### Requirement: OpenCode plugin usage has deterministic source precedence

OpenCode plugin sidecar usage SHALL be a fallback source after existing direct-stage usage extraction sources. Consumers SHALL apply source precedence in this order:

- Parsed worker JSON usage and model metadata.
- Recognized stage log metadata usage and model metadata.
- OpenCode plugin sidecar usage and model metadata.
- Unavailable usage and unknown model identity.

When the sidecar is the winning usage source, telemetry SHALL set `usage.usage_source = "opencode_plugin"`. The sidecar SHALL NOT override token usage or model identity obtained from higher-precedence worker JSON or recognized log metadata.

#### Scenario: Worker JSON remains highest precedence

- **WHEN** parsed worker JSON contains valid token usage and the OpenCode plugin sidecar contains different valid token usage
- **THEN** telemetry uses the worker JSON usage and sets `usage.usage_source = "worker_json"`

#### Scenario: Sidecar fills usage after existing sources have none

- **WHEN** parsed worker JSON and recognized log metadata contain no usable token counts and the OpenCode plugin sidecar contains a valid selected record with normalized token counts
- **THEN** telemetry populates usage from the sidecar and sets `usage.usage_source = "opencode_plugin"`
