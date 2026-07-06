## ADDED Requirements

### Requirement: Direct stage invocations produce telemetry records

Every direct implement, review, and archive stage invocation in `opsx-plan.py` SHALL produce exactly one JSONL telemetry record written to `.opsx-plan/telemetry/<plan_name>.jsonl`. The record SHALL be written after the stage worker invocation completes or fails.

The record SHALL conform to the `plan-run-observability` schema version `1` and SHALL include:

- All required identity fields (`schema_version`, `plan_name`, `run_id`, `change_id`, `stage`, `round`, `status`, `started_at`, `ended_at`, `duration_ms`).
- A unique `uid` field (UUID string) for per-record identification and state linking.
- The `invocation` object with `adapter`, `worker_command`, `args_sample`, `timeout_seconds`, and `retry_attempt`.
- The `model` object with all fields set to `null` (model extraction is deferred to `capture-worker-usage-metadata`).
- The `result` object with `log_path` set to the relative path of the stage log, `stage_status` set from the parsed worker output when available, and `error_message` populated for error outcomes.
- The `usage` object with `usage_available` set to `false`, all token count fields set to `null`, and `usage_source` set to `null`.
- The `cost` object with `status` set to `"unavailable"`, all cost fields set to `null`.

#### Scenario: Successful implement stage produces a complete record

- **WHEN** `opsx-plan.py` dispatches a direct implement stage and the worker returns a valid status
- **THEN** a telemetry record is appended to `.opsx-plan/telemetry/<plan_name>.jsonl` with `status` equal to `"completed"`, `ended_at` and `duration_ms` non-null, `stage` equal to `"implement"`, `result.stage_status` set from the parsed worker output, `result.log_path` pointing to the stage log file, `usage.usage_available` is `false`, and `cost.status` is `"unavailable"`

#### Scenario: Successful review stage populates verdict and findings

- **WHEN** `opsx-plan.py` dispatches a direct review stage and the worker returns a verdict and finding counts
- **THEN** the telemetry record has `stage` equal to `"review"`, `result.verdict` is `"pass"` or `"fail"`, `result.critical_count`, `result.warning_count`, and `result.note_count` are populated from the worker output, and `result.stage_status` is set from the parsed worker output

#### Scenario: Stringent reviewer verdict defaults findings

- **WHEN** the review worker returns a verdict but omit finding counts entirely
- **THEN** `result.critical_count`, `result.warning_count`, and `result.note_count` are `null` rather than fabricated values

#### Scenario: Timeout produces a telemetry record

- **WHEN** a stage worker exceeds its configured timeout and is terminated
- **THEN** a telemetry record is written with `status` equal to `"timeout"`, `ended_at` and `duration_ms` non-null, `result.stage_status` is `null`, and `result.error_message` describes the timeout

#### Scenario: Spawn error produces a telemetry record

- **WHEN** a stage worker command cannot be spawned (e.g., binary not found)
- **THEN** a telemetry record is written with `status` equal to `"spawn_error"`, `result.stage_status` is `null`, and `result.error_message` contains the spawn error description

#### Scenario: Invalid worker JSON produces a telemetry record

- **WHEN** a stage worker exits but produces output that cannot be parsed as valid stage JSON
- **THEN** a telemetry record is written with `status` equal to `"invalid_output"`, `result.stage_status` is `null`, and `result.error_message` describes the parse failure

#### Scenario: Telemetry directory created on first write

- **WHEN** the first telemetry record for a plan is written and `.opsx-plan/telemetry/` does not exist
- **THEN** the directory is created and the record is appended to a new `.opsx-plan/telemetry/<plan_name>.jsonl` file

### Requirement: Telemetry records are linked from plan state

The per-change worker state record SHALL include a `telemetry` object with a `latest_telemetry` field set to the `uid` of the most recently written telemetry record for that change and stage combination. This SHALL be updated after every stage invocation so that aggregators can discover the latest telemetry entry without scanning the full JSONL file.

The `telemetry` field in worker state SHALL be optional. Existing state files created before this change that lack the `telemetry` key SHALL be handled gracefully by consumers.

#### Scenario: State links to latest telemetry after stage completion

- **WHEN** a direct implement stage completes and a telemetry record is written
- **THEN** the per-change worker state record contains `telemetry.latest_telemetry` with the UID of the written record

#### Scenario: State lacks telemetry key for pre-change runs

- **WHEN** a per-change worker state file was created before `record-direct-stage-telemetry` was implemented
- **THEN** the `telemetry` key is absent and consumers treat it as "no telemetry linked"

#### Scenario: State linking updated on each round

- **WHEN** a change cycles through implement → review → implement rounds
- **THEN** each stage invocation updates `telemetry.latest_telemetry` with the UID of the most recent record

### Requirement: run_id is stable across plan resumptions

The `run_id` field in every telemetry record for a plan run SHALL remain stable when the plan is paused and resumed. The orchestrator SHALL derive `run_id` from the plan state's `started_at` timestamp on first run, falling back to a generated UUID if no timestamp exists.

#### Scenario: run_id persists across pause and resume

- **WHEN** a plan run writes telemetry for stage invocations, is paused, and later resumed
- **THEN** telemetry records from both before and after the pause share the same `run_id`

#### Scenario: First plan run generates a stable run_id

- **WHEN** a plan is executed for the first time with no prior state
- **THEN** a `run_id` is derived from the plan's `started_at` timestamp and persisted in the plan state

### Requirement: Telemetry writing does not alter control loop behavior

The implement-review-archive control loop, resume mechanics, round tracking, progress detection, and error recovery SHALL behave identically with and without telemetry writing enabled. A failure to write or flush a telemetry record SHALL NOT cause a stage invocation to be treated as failed, and SHALL NOT prevent the control loop from advancing to the next phase.

#### Scenario: Telemetry write failure does not block stage advancement

- **WHEN** a stage completes successfully but writing the telemetry record fails (e.g., disk full)
- **THEN** the stage outcome is still applied, the control loop advances to the next phase, and the failure is logged as a warning

#### Scenario: Stage failure with successful telemetry write

- **WHEN** a stage invocation fails (timeout, spawn error, or invalid output)
- **THEN** a telemetry record with the corresponding failure status is written, and the control loop applies the failure according to its existing retry/round logic without any change in behavior
