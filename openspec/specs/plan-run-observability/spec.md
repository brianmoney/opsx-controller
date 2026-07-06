# plan-run-observability Specification

## Purpose
TBD - created by archiving change 2026-07-05-define-plan-run-telemetry-schema. Update Purpose after archive.
## Requirements
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

### Requirement: Pricing catalog stores versioned model pricing entries

The pricing catalog SHALL be a TOML file at `lib/pricing/catalog.toml` containing a `[catalog]` metadata section with `version` (string) and `updated` (ISO-8601 date string) fields, and an array of `[[entries]]` where each entry describes a model's pricing.

Each `[[entries]]` SHALL include the following fields:

- `provider` (string, required): The API provider name (e.g., `"openai"`, `"anthropic"`, `"google"`).
- `model_id` (string, required): The canonical model identifier (e.g., `"gpt-4o"`).
- `display_name` (string, required): A human-readable model name for reports.
- `billing_mode` (string, required): SHALL be `"per_token"` or `"subscription"`.
- `currency` (string, required): An ISO 4217 currency code (e.g., `"USD"`).
- `effective_date` (string, required): ISO-8601 date when this pricing entry became or becomes effective.
- `input_price_per_mtok` (float, optional): USD per million input tokens for per-token models.
- `output_price_per_mtok` (float, optional): USD per million output tokens for per-token models.
- `cached_input_price_per_mtok` (float, optional): USD per million cached input tokens for per-token models.
- `reasoning_price_per_mtok` (float, optional): USD per million reasoning tokens for per-token models.
- `subscription_period` (string, optional): Billing period for subscription models (`"monthly"` or `"yearly"`).
- `subscription_price` (float, optional): Subscription period price in the specified currency.
- `notes` (string, optional): Freeform operator notes about this pricing entry.

Per-token entries (where `billing_mode` is `"per_token"`) SHALL have at minimum `input_price_per_mtok` populated. Other per-mtok rate fields are optional. Subscription entries (where `billing_mode` is `"subscription"`) SHALL have `subscription_period` and `subscription_price` populated.

Multiple entries for the same `(provider, model_id)` pair MAY exist. When they do, the entry with the latest `effective_date` SHALL be considered the current pricing entry.

#### Scenario: Valid per-token entry in catalog

- **WHEN** the catalog contains an entry with `billing_mode = "per_token"`, `provider = "openai"`, `model_id = "gpt-4o"`, and `input_price_per_mtok = 2.50`
- **THEN** the loader parses the entry successfully and `resolve("openai", "gpt-4o")` returns a `ResolvedPrice` with `billing_mode = "per_token"`, `input_price_per_mtok = 2.50`, and `currency = "USD"`

#### Scenario: Valid subscription entry in catalog

- **WHEN** the catalog contains an entry with `billing_mode = "subscription"`, `provider = "github"`, `model_id = "copilot"`, `subscription_period = "monthly"`, and `subscription_price = 10.00`
- **THEN** the loader parses the entry successfully and `resolve("github", "copilot")` returns a `ResolvedPrice` with `billing_mode = "subscription"`, `subscription_period = "monthly"`, `subscription_price = 10.0`, and all per-mtok rate fields `None`

#### Scenario: Multiple entries for same model — latest effective_date wins

- **WHEN** the catalog has two entries for `provider = "openai"`, `model_id = "gpt-4o"` with `effective_date` values of `"2025-01-01"` and `"2025-07-01"` and different `input_price_per_mtok` values
- **THEN** `resolve("openai", "gpt-4o")` returns the entry with `effective_date = "2025-07-01"` and its rates

### Requirement: Pricing catalog loader validates entries at load time

The `PricingCatalog` constructor in `lib/pricing/loader.py` SHALL parse the TOML catalog file and validate every `[[entries]]` entry at load time. Validation SHALL fail with a `CatalogLoadError` for any of the following conditions:

- A required field is missing or empty (`provider`, `model_id`, `display_name`, `billing_mode`, `currency`, `effective_date`).
- `billing_mode` is not one of `"per_token"` or `"subscription"`.
- `currency` is not a valid ISO 4217 three-letter code.
- `billing_mode` is `"per_token"` and `input_price_per_mtok` is missing, negative, or zero.
- Any present per-mtok rate field (`input_price_per_mtok`, `output_price_per_mtok`, `cached_input_price_per_mtok`, `reasoning_price_per_mtok`) contains a negative value.
- `billing_mode` is `"subscription"` and either `subscription_period` or `subscription_price` is missing.
- `subscription_period` is not one of `"monthly"` or `"yearly"` when `billing_mode` is `"subscription"`.
- `effective_date` is not a valid ISO-8601 date string.

The `CatalogLoadError` SHALL include the index of the failing entry and a message identifying which validation rule was violated.

#### Scenario: Missing provider field raises CatalogLoadError

- **WHEN** the catalog contains an `[[entries]]` with `model_id = "gpt-4o"` but no `provider` field
- **THEN** `PricingCatalog` construction raises `CatalogLoadError` with a message referencing the entry index and the missing `provider` field

#### Scenario: Negative input_price_per_mtok raises CatalogLoadError

- **WHEN** the catalog contains a per-token entry with `input_price_per_mtok = -1.0`
- **THEN** `PricingCatalog` construction raises `CatalogLoadError` with a message indicating the negative rate

#### Scenario: Unknown billing_mode raises CatalogLoadError

- **WHEN** the catalog contains an entry with `billing_mode = "flat_rate"`
- **THEN** `PricingCatalog` construction raises `CatalogLoadError` with a message indicating the invalid billing mode

#### Scenario: Subscription entry missing subscription_price raises CatalogLoadError

- **WHEN** the catalog contains an entry with `billing_mode = "subscription"` but no `subscription_price`
- **THEN** `PricingCatalog` construction raises `CatalogLoadError` with a message indicating the missing subscription price

### Requirement: Pricing catalog resolver returns typed results for known and unknown models

The `PricingCatalog.resolve(provider: str, model_id: str)` method SHALL return one of:

- A `ResolvedPrice` dataclass when a matching entry exists for the given `(provider, model_id)` pair. When multiple entries match, the entry with the latest `effective_date` SHALL be returned.
- An `UnresolvedPrice` dataclass when no entry matches. The `reason` field SHALL describe why resolution failed: "unknown provider" when the provider has no entries, or "unknown model" when the provider has entries but none match the given `model_id`.

The `ResolvedPrice` SHALL include all fields from the matching catalog entry (`provider`, `model_id`, `display_name`, `billing_mode`, `currency`, per-mtok rates or `None`, subscription fields or `None`, `effective_date`, `notes`).

The `UnresolvedPrice` SHALL include `provider`, `model_id` (as provided to the call), and `reason`.

#### Scenario: Resolve known per-token model

- **WHEN** `resolve("openai", "gpt-4o")` is called on a catalog containing a matching per-token entry
- **THEN** the method returns a `ResolvedPrice` with `provider = "openai"`, `model_id = "gpt-4o"`, `billing_mode = "per_token"`, populated per-mtok rates, and `subscription_period`/`subscription_price` set to `None`

#### Scenario: Resolve unknown model for known provider

- **WHEN** `resolve("openai", "gpt-99")` is called on a catalog that has OpenAI entries but none with `model_id = "gpt-99"`
- **THEN** the method returns an `UnresolvedPrice` with `reason` containing "unknown model"

#### Scenario: Resolve unknown provider

- **WHEN** `resolve("nonexistent", "model")` is called on a catalog with no entries for provider `"nonexistent"`
- **THEN** the method returns an `UnresolvedPrice` with `reason` containing "unknown provider"

### Requirement: Catalog loader exposes version metadata

The `PricingCatalog` class SHALL expose a `get_catalog_version()` method that returns the `version` string from the `[catalog]` metadata section. This version SHALL be used by downstream cost estimation to populate `cost.pricing_catalog_version` in telemetry records.

#### Scenario: get_catalog_version returns the version string

- **WHEN** the catalog metadata has `version = "1.0.0"`
- **THEN** `get_catalog_version()` returns `"1.0.0"`

### Requirement: Empty catalog handles all lookups as unresolved

When a valid TOML catalog file contains `[catalog]` metadata but no `[[entries]]`, the `PricingCatalog` SHALL load successfully. All `resolve()` calls SHALL return `UnresolvedPrice` with `reason = "empty catalog"`.

#### Scenario: Empty catalog loads without error

- **WHEN** the catalog TOML file contains valid metadata but no entries
- **THEN** `PricingCatalog` construction succeeds and `resolve("any", "model")` returns `UnresolvedPrice` with `reason = "empty catalog"`

### Requirement: Direct stage invocations produce telemetry records

Every direct implement, review, and archive stage invocation in `opsx-plan.py` SHALL produce exactly one JSONL telemetry record written to `.opsx-plan/telemetry/<plan_name>.jsonl`. The record SHALL be written after the stage worker invocation completes or fails.

The record SHALL conform to the `plan-run-observability` schema version `1` and SHALL include:

- All required identity fields (`schema_version`, `plan_name`, `run_id`, `change_id`, `stage`, `round`, `status`, `started_at`, `ended_at`, `duration_ms`).
- A unique `uid` field (UUID string) for per-record identification and state linking.
- The `invocation` object with `adapter`, `worker_command`, `args_sample`, `timeout_seconds`, and `retry_attempt`.
- The `model` object with `provider`, `model_id`, and `model_alias` populated from recognized worker usage metadata when available, or `null` when unavailable.
- The `result` object with `log_path` set to the relative path of the stage log, `stage_status` set from the parsed worker output when available, and `error_message` populated for error outcomes.
- The `usage` object with token count fields populated from recognized worker usage metadata when available, `usage_available` reflecting whether any token field is non-null, and `usage_source` identifying the winning extraction source (`"worker_json"` or `"log_metadata"`).
- The `cost` object with `status` set to `"unavailable"`, all cost fields set to `null`.

#### Scenario: Successful implement stage produces a complete record

- **WHEN** `opsx-plan.py` dispatches a direct implement stage and the worker returns a valid status
- **THEN** a telemetry record is appended to `.opsx-plan/telemetry/<plan_name>.jsonl` with `status` equal to `"completed"`, `ended_at` and `duration_ms` non-null, `stage` equal to `"implement"`, `result.stage_status` set from the parsed worker output, `result.log_path` pointing to the stage log file, `usage` populated from recognized worker usage metadata when available, and `cost.status` is `"unavailable"`

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

