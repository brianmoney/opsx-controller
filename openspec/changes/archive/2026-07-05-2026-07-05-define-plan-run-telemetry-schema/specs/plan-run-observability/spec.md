## ADDED Requirements

### Requirement: Telemetry records have a stable identity shape

Every telemetry record SHALL be a single JSON object on its own line (JSONL) and SHALL include the following required fields:

- `schema_version` (integer): The schema version for this record. Initial version is `1`.
- `plan_name` (string): The plan name from the plan manifest.
- `run_id` (string): A unique identifier for the plan run, stable across resumptions of the same run.
- `change_id` (string): The OpenSpec change id being processed by this stage.
- `stage` (string): The phase worker being invoked. SHALL be one of `"implement"`, `"review"`, or `"archive"`.
- `round` (integer): The one-indexed attempt round for this change's implement-review cycle. Archive stages SHALL record the round of the preceding review.
- `status` (string): The outcome of the stage invocation. SHALL be one of `"started"`, `"completed"`, `"failed"`, `"timeout"`, `"spawn_error"`, or `"invalid_output"`.
- `started_at` (string): ISO-8601 UTC timestamp when the stage worker invocation began.
- `ended_at` (string or null): ISO-8601 UTC timestamp when the stage worker invocation ended. SHALL be `null` when `status` is `"started"`.
- `duration_ms` (integer or null): Elapsed wall-clock milliseconds between `started_at` and `ended_at`. SHALL be `null` when `status` is `"started"`.

#### Scenario: Complete record with all identifier fields
- **WHEN** a telemetry record is written for a successfully completed stage
- **THEN** the record contains `schema_version`, `plan_name`, `run_id`, `change_id`, `stage`, `round`, `status`, `started_at`, `ended_at`, and `duration_ms`

#### Scenario: Started-but-not-finished record
- **WHEN** a telemetry record is written at the start of a stage invocation before the worker exits
- **THEN** `status` is `"started"`, `ended_at` is `null`, and `duration_ms` is `null`

### Requirement: Telemetry records capture invocation context

Telemetry records SHALL include an `invocation` object with the following fields describing how the stage worker was invoked:

- `adapter` (string): The orchestration adapter used for this invocation (e.g. `"opencode"`).
- `worker_command` (string): The primary worker command or subagent type dispatched.
- `args_sample` (array of strings or null): A sanitized sample of arguments passed to the worker, truncated to prevent unbounded record growth. SHALL be `null` when argument capture is unavailable.
- `timeout_seconds` (integer or null): The configured invocation timeout in seconds. SHALL be `null` when not configured.
- `retry_attempt` (integer): The zero-indexed retry attempt for this stage within the current round. SHALL be `0` for first attempts.

#### Scenario: Direct worker invocation with arguments
- **WHEN** `opsx-plan` dispatches an implement stage with a specific worker command and arguments
- **THEN** the telemetry record includes `invocation.adapter`, `invocation.worker_command`, `invocation.args_sample`, `invocation.timeout_seconds`, and `invocation.retry_attempt`

### Requirement: Telemetry records capture model identity

Telemetry records SHALL include a `model` object with the following fields:

- `provider` (string or null): The API provider name (e.g. `"openai"`, `"anthropic"`, `"google"`). SHALL be `null` when the provider cannot be determined.
- `model_id` (string or null): The canonical model identifier used in API calls (e.g. `"gpt-4o"`). SHALL be `null` when the model cannot be determined.
- `model_alias` (string or null): The operator-configured alias for this model, if the agent configuration uses an alias instead of a raw model id. SHALL be `null` when no alias is configured or when the actual model is unknown.

All three fields SHALL be `null` when model identity cannot be extracted from the worker invocation or output.

#### Scenario: Model identity extracted from worker output
- **WHEN** the worker output or configuration reveals the provider and model
- **THEN** the telemetry record populates `model.provider` and `model.model_id`

#### Scenario: Model identity unavailable
- **WHEN** the worker invocation provides no model identity information
- **THEN** `model.provider`, `model.model_id`, and `model_alias` are all `null`

### Requirement: Telemetry records capture parsed worker result summary

Telemetry records SHALL include a `result` object with parsed worker output summary fields:

- `stage_status` (string or null): The machine-readable status returned by the worker. SHALL be `null` when `status` is not `"completed"`.
- `verdict` (string or null): The review verdict when the stage is `"review"`. SHALL be `"pass"` or `"fail"` when available. SHALL be `null` for non-review stages or when the verdict is absent.
- `critical_count` (integer or null): Number of critical findings returned by the reviewer. SHALL be `null` for non-review stages or when count is unavailable.
- `warning_count` (integer or null): Number of warning findings returned by the reviewer. SHALL be `null` for non-review stages or when count is unavailable.
- `note_count` (integer or null): Number of note findings returned by the reviewer. SHALL be `null` for non-review stages or when count is unavailable.
- `error_message` (string or null): A sanitized error message when the worker returned an error. SHALL be `null` when no error occurred.
- `log_path` (string or null): Relative path to the stage-specific log file under `.opsx-plan/logs/`. SHALL be `null` when no log was written.

#### Scenario: Successful review with findings
- **WHEN** the review worker returns `verdict=fail` with 2 critical findings, 3 warnings, and 1 note
- **THEN** the telemetry record has `result.verdict` set to `"fail"`, `result.critical_count` set to `2`, `result.warning_count` set to `3`, and `result.note_count` set to `1`

#### Scenario: Failed stage with error
- **WHEN** the stage worker process exits with a spawn error
- **THEN** `status` is `"spawn_error"` and `result.error_message` contains the sanitized error description

### Requirement: Token usage fields distinguish missing from zero usage

Telemetry records SHALL include a `usage` object with the following fields:

- `usage_available` (boolean): `true` when at least one token field has a non-null value, `false` when all token fields are `null`.
- `usage_source` (string or null): Describes how usage was obtained (e.g. `"worker_json"`, `"log_metadata"`, `"provider_api"`). SHALL be `null` when `usage_available` is `false`.
- `input_tokens` (integer or null): Input/prompt tokens consumed. `null` means unavailable; `0` means the provider reported zero input tokens.
- `output_tokens` (integer or null): Output/completion tokens consumed. `null` means unavailable; `0` means zero output tokens.
- `cached_input_tokens` (integer or null): Input tokens served from cache. `null` means unavailable or not applicable; `0` means zero cache hits.
- `reasoning_tokens` (integer or null): Reasoning/thinking tokens consumed by models that report them separately. `null` means unavailable or not applicable; `0` means zero reasoning tokens.
- `total_tokens` (integer or null): Total tokens consumed as reported by the provider. `null` means unavailable; when present it SHOULD equal the sum of populated token sub-fields, but the schema does not enforce this.

All token count fields SHALL use `null` to represent unavailable values and `0` to represent a known zero value. Downstream consumers SHALL treat `null` as "unknown" and `0` as "confirmed zero" in reports and cost calculations.

#### Scenario: Usage fully populated from worker output
- **WHEN** the worker returns structured token counts including cache and reasoning breakdown
- **THEN** `usage.usage_available` is `true`, all token count fields contain non-null integers, and `usage.usage_source` identifies the extraction method

#### Scenario: Usage completely unavailable
- **WHEN** the worker output provides no token usage information
- **THEN** `usage.usage_available` is `false`, all token count fields are `null`, and `usage.usage_source` is `null`

#### Scenario: Zero usage reported
- **WHEN** the provider reports zero tokens for a stage invocation
- **THEN** token count fields contain `0` (not `null`) and `usage.usage_available` is `true`

#### Scenario: Partial usage with some fields missing
- **WHEN** the worker reports input and output tokens but no cache or reasoning breakdown
- **THEN** `usage.input_tokens` and `usage.output_tokens` contain integers, `usage.cached_input_tokens` and `usage.reasoning_tokens` are `null`, `usage.usage_available` is `true`

### Requirement: Cost estimation placeholders reserve billing mode slots

Telemetry records SHALL include a `cost` object with the following fields:

- `status` (string): The cost estimation status. SHALL be one of:
  - `"unavailable"`: Cost estimation has not been attempted (default before Phase 2).
  - `"unresolved"`: Estimation was attempted but pricing or usage data is insufficient.
  - `"estimated"`: A cost estimate was calculated and stored in `price_snapshot`.
- `pricing_catalog_version` (string or null): The version string of the pricing catalog used for estimation. SHALL be `null` when `status` is `"unavailable"`.
- `price_snapshot` (object or null): Contains the pricing rates and billing mode used for the estimate. The shape of this object is defined by `estimate-stage-token-costs` and is intentionally loose in this schema. SHALL include at minimum `billing_mode` (one of `"per_token"` or `"subscription"`), `currency` (ISO 4217 code), and model-specific rate fields. SHALL be `null` when `status` is `"unavailable"` or `"unresolved"`.
- `unresolved_reason` (string or null): Human-readable explanation when estimation could not be completed. SHALL be `null` when `status` is `"estimated"` or `"unavailable"`.
- `estimated_cost` (number or null): The estimated cost in the currency specified by `price_snapshot.currency`. SHALL be `null` when `status` is `"unavailable"` or `"unresolved"`.

The `price_snapshot` object SHALL be sufficient for downstream consumers to reproduce cost estimates from stored telemetry without re-querying the pricing catalog.

#### Scenario: Telemetry record before cost estimation
- **WHEN** a telemetry record is written by `record-direct-stage-telemetry` before cost estimation runs
- **THEN** `cost.status` is `"unavailable"`, `cost.pricing_catalog_version` is `null`, `cost.price_snapshot` is `null`, `cost.unresolved_reason` is `null`, and `cost.estimated_cost` is `null`

#### Scenario: Cost estimation unresolved due to missing usage
- **WHEN** cost estimation runs but `usage.usage_available` is `false`
- **THEN** `cost.status` is `"unresolved"`, `cost.unresolved_reason` describes the missing usage, and `cost.estimated_cost` is `null`

#### Scenario: Cost estimated for per-token model
- **WHEN** cost estimation runs with available usage and a matching per-token pricing entry
- **THEN** `cost.status` is `"estimated"`, `cost.price_snapshot.billing_mode` is `"per_token"`, `cost.price_snapshot` contains the rates used, `cost.estimated_cost` is the calculated value, and `cost.pricing_catalog_version` identifies the catalog version

#### Scenario: Cost estimated for subscription model
- **WHEN** cost estimation runs for a subscription-billed model with a configured usage denominator
- **THEN** `cost.status` is `"estimated"`, `cost.price_snapshot.billing_mode` is `"subscription"`, and `cost.price_snapshot` contains the amortized effective rate and subscription price used

### Requirement: Telemetry records are stored under `.opsx-plan/telemetry/`

Telemetry records SHALL be written as append-only JSONL files under `.opsx-plan/telemetry/`. Each plan's telemetry SHALL be stored in a file named `<plan_name>.jsonl` within that directory.

The storage root `.opsx-plan/telemetry/` SHALL be separate from the plan state file (`.opsx-plan/<plan_name>.state.json`) and the logs directory (`.opsx-plan/logs/`) so that telemetry can be archived, deleted, or processed independently of plan execution state.

Records within a plan-scoped file SHALL NOT be assumed to be ordered by timestamp across resumptions, but each record SHALL carry its own `started_at` timestamp for ordering by consumers.

#### Scenario: Telemetry file created alongside plan state
- **WHEN** `record-direct-stage-telemetry` writes the first telemetry record for a plan run
- **THEN** the record is appended to `.opsx-plan/telemetry/<plan_name>.jsonl` and the directory is created if it does not exist

#### Scenario: Telemetry survives plan state deletion
- **WHEN** an operator deletes `.opsx-plan/<plan_name>.state.json` to reset plan execution
- **THEN** `.opsx-plan/telemetry/<plan_name>.jsonl` remains intact and unchanged

### Requirement: Schema versioning preserves historical readability

Every telemetry record SHALL include a `schema_version` integer field. When the schema is extended with new fields or changed semantics, the `schema_version` SHALL be incremented. Readers SHALL use `schema_version` to branch on field presence, type expectations, and semantics.

Records written with different `schema_version` values MAY coexist in the same plan-scoped JSONL file. Consumers SHALL handle unknown `schema_version` values gracefully and SHALL NOT fail to read older records when new optional fields are added to the current schema.

The initial `schema_version` SHALL be `1`.

#### Scenario: Reader processes file with mixed schema versions
- **WHEN** a telemetry file contains records with `schema_version` values of `1` and `2`
- **THEN** a reader SHALL parse both record shapes correctly by branching on `schema_version`

#### Scenario: Complete record with all fields populated
- **WHEN** a telemetry record is written for a successfully completed review stage with full invocation context, model identity, parsed findings, detailed token usage, and a resolved per-token cost estimate
- **THEN** the record includes all identifier fields (`schema_version`, `plan_name`, `run_id`, `change_id`, `stage`, `round`, `status`, `started_at`, `ended_at`, `duration_ms`), a complete `invocation` object, a complete `model` object with `provider` and `model_id`, a complete `result` object with `verdict` and finding counts, a complete `usage` object with all token counts populated and `usage_available` set to `true`, and a `cost` object with `status` set to `"estimated"`, `pricing_catalog_version` identifying the catalog version, `price_snapshot` containing `billing_mode` and rates, and `estimated_cost` holding the calculated value

#### Scenario: Future field additions do not break readers
- **WHEN** a future schema version adds optional fields
- **THEN** readers targeting the current schema version SHALL ignore unknown fields and still extract all fields defined in the current version
