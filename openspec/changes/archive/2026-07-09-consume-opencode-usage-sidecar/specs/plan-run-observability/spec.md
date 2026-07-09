## ADDED Requirements

### Requirement: Direct OpenCode stages provide plugin usage sidecar context

For every direct OpenCode-backed implement, review, or archive stage invocation, `opsx-plan.py` SHALL create a unique usage sidecar path scoped to that stage invocation and SHALL create the parent directory before starting the OpenCode subprocess.

The OpenCode subprocess environment SHALL include `OPSX_USAGE_PATH`, `OPSX_PLAN_NAME`, `OPSX_RUN_ID`, `OPSX_CHANGE_ID`, `OPSX_STAGE`, and `OPSX_ROUND` values matching the stage identity that will be written to telemetry. `OPSX_ROUND` SHALL be the one-indexed stage round encoded as a base-10 integer string.

The sidecar path SHOULD be under `.opsx-plan/usage/` and MUST NOT point at the canonical telemetry JSONL file or stage log file.

#### Scenario: OpenCode stage receives exact usage context

- **WHEN** `opsx-plan.py` starts a direct OpenCode implement, review, or archive stage
- **THEN** the subprocess environment contains a unique `OPSX_USAGE_PATH` and matching `OPSX_PLAN_NAME`, `OPSX_RUN_ID`, `OPSX_CHANGE_ID`, `OPSX_STAGE`, and `OPSX_ROUND` values for that exact stage invocation

#### Scenario: Sidecar path is separate from telemetry and logs

- **WHEN** `opsx-plan.py` prepares an OpenCode usage sidecar path
- **THEN** the path is not the plan telemetry JSONL path and is not the stage log path

### Requirement: Direct stage telemetry consumes valid OpenCode plugin sidecar records

After a direct OpenCode stage invocation ends or is interrupted, `opsx-plan.py` SHALL read the configured usage sidecar as best-effort input to telemetry construction.

The sidecar consumer SHALL consider only records that meet all of these criteria:

- `schema_version` is `1`.
- `plan_name`, `run_id`, `change_id`, `stage`, and `round` match the current stage invocation.
- `event_type` is either `"final"` or `"incremental"`.
- `emitted_at` is a usable timestamp for ordering records of the same event type.
- At least one normalized token count or model identity field is usable after conservative normalization.

Token count, request count, and latency fields SHALL be used only when they are non-negative integers. Missing, malformed, negative, floating-point, non-numeric, or ambiguous numeric values SHALL be treated as unavailable and SHALL NOT be coerced to zero or another integer.

#### Scenario: Valid final sidecar record is selected

- **WHEN** a sidecar contains a valid final record matching the current stage invocation
- **THEN** `opsx-plan.py` selects the latest valid final record by `emitted_at` for sidecar-derived usage and model identity

#### Scenario: Invalid records are ignored independently

- **WHEN** a sidecar contains malformed JSONL, unsupported schema versions, unknown event types, identity mismatches, and one valid matching record
- **THEN** `opsx-plan.py` ignores the invalid records and may use the valid matching record

### Requirement: Sidecar incremental usage is used only for interrupted stages

When no valid final sidecar record exists, `opsx-plan.py` SHALL limit incremental sidecar usage to interrupted or otherwise non-normal stage outcomes. For those non-normal outcomes, it MAY select the latest valid incremental sidecar record as best-effort partial usage.

For normally completed stages, sidecar usage SHALL remain unavailable when the sidecar contains only incremental records.

#### Scenario: Timeout uses latest incremental usage

- **WHEN** a direct OpenCode stage times out and the sidecar contains no valid final record but does contain valid incremental records with token counts
- **THEN** telemetry may populate usage from the latest valid incremental record and set `usage.usage_source = "opencode_plugin"` when no higher-precedence usage source exists

#### Scenario: Completed stage ignores incremental-only sidecar usage

- **WHEN** a direct OpenCode stage completes normally and its sidecar contains only incremental records
- **THEN** telemetry does not populate usage from the sidecar and keeps usage unavailable unless a higher-precedence source provides usage

### Requirement: OpenCode sidecar usage is merged with deterministic source precedence

Direct stage telemetry SHALL preserve usage and model metadata precedence in this order:

- Parsed worker JSON usage and model metadata.
- Recognized stage log metadata usage and model metadata.
- OpenCode plugin sidecar usage and model metadata.
- Unavailable usage and unknown model identity.

When selected sidecar token usage is the winning source, telemetry SHALL set `usage.usage_available = true` and `usage.usage_source = "opencode_plugin"`. The sidecar SHALL NOT override token usage or model identity obtained from higher-precedence worker JSON or recognized log metadata.

Cost estimation SHALL run after sidecar-derived usage and model identity have been merged into the telemetry record, using the same pricing catalog and unresolved-cost behavior as other usage sources.

#### Scenario: Sidecar fills usage and enables cost estimation

- **WHEN** worker JSON and recognized log metadata contain no usable usage, the sidecar contains selected token usage and model identity, and the pricing catalog has a matching entry
- **THEN** telemetry uses `usage.usage_source = "opencode_plugin"` and cost estimation may write `cost.status = "estimated"`

#### Scenario: Worker JSON keeps precedence over sidecar

- **WHEN** parsed worker JSON contains valid token usage and the OpenCode plugin sidecar contains different valid token usage
- **THEN** telemetry uses the worker JSON token usage and sets `usage.usage_source = "worker_json"`

#### Scenario: Log metadata keeps precedence over sidecar

- **WHEN** worker JSON contains no usable usage, recognized stage log metadata contains valid token usage, and the OpenCode plugin sidecar also contains valid token usage
- **THEN** telemetry uses the log metadata token usage and sets `usage.usage_source = "log_metadata"`

### Requirement: Sidecar handling remains non-fatal to plan execution

Missing sidecar files, empty sidecar files, unreadable sidecar files, malformed JSONL lines, unsupported records, identity mismatches, records without usable token counts, and sidecar reader errors SHALL NOT fail a stage invocation, alter the stage `status`, prevent telemetry from being written, or change retry and control-loop behavior.

If no valid sidecar record remains and no higher-precedence usage source provides usage, telemetry SHALL preserve unavailable usage and existing unresolved-cost behavior.

#### Scenario: Missing sidecar preserves unavailable usage

- **WHEN** a direct OpenCode stage finishes and the configured sidecar file is missing or empty
- **THEN** telemetry is still written and usage remains unavailable unless worker JSON or recognized log metadata provides usage

#### Scenario: Sidecar reader failure does not change stage outcome

- **WHEN** reading or parsing the sidecar raises an error
- **THEN** the stage outcome is unchanged, telemetry is still written, and retry behavior follows the stage outcome rather than the sidecar read failure
