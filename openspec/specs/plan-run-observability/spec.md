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

### Requirement: Direct stage telemetry records include deterministic cost estimation outcomes

When direct stage telemetry is written for a completed stage and the runtime supports `estimate-stage-token-costs`, the record SHALL attempt cost estimation from the record's normalized `usage` object, normalized `model` object, and the local pricing catalog.

The resulting `cost.status` SHALL be:

- `"estimated"` when the runtime has enough usage, model, pricing, and billing-mode-specific inputs to compute a reproducible estimate.
- `"unresolved"` when estimation was attempted but one or more required inputs were unavailable or invalid.

The runtime SHALL NOT fail the stage, alter the stage `status`, or skip telemetry writing solely because cost estimation is unresolved.

#### Scenario: Completed stage produces estimated cost

- **WHEN** a completed direct stage telemetry record has available usage, reliable `model.provider` and `model.model_id`, and resolvable pricing inputs
- **THEN** the record is written with `cost.status = "estimated"`, a non-null `cost.estimated_cost`, and a non-null `cost.price_snapshot`

#### Scenario: Cost estimation failure does not fail the stage

- **WHEN** cost estimation cannot be completed because a required pricing or usage input is unavailable
- **THEN** the stage record is still written, the stage `status` is unchanged, and `cost.status = "unresolved"`

### Requirement: Per-token cost estimation uses category-specific rates

For telemetry records whose resolved pricing entry has `billing_mode = "per_token"`, the estimated cost SHALL equal the sum of all priced token categories calculated as:

- `input_tokens / 1_000_000 * input_price_per_mtok` when `input_tokens` is non-null
- `output_tokens / 1_000_000 * output_price_per_mtok` when `output_tokens` is non-null
- `cached_input_tokens / 1_000_000 * cached_input_price_per_mtok` when `cached_input_tokens` is non-null
- `reasoning_tokens / 1_000_000 * reasoning_price_per_mtok` when `reasoning_tokens` is non-null

A token category with usage `0` SHALL contribute zero cost. A token category with `null` usage SHALL contribute no calculated amount. If a token category has a positive observed usage count but the matching catalog rate is `null`, cost estimation SHALL be unresolved.

#### Scenario: Input and output token rates produce an estimate

- **WHEN** a telemetry record has `usage.input_tokens = 200000`, `usage.output_tokens = 50000`, `usage.cached_input_tokens = null`, `usage.reasoning_tokens = null`, and the resolved catalog entry has `input_price_per_mtok = 2.0` and `output_price_per_mtok = 8.0`
- **THEN** `cost.status = "estimated"` and `cost.estimated_cost = 0.8`

#### Scenario: Cached tokens contribute when cached rate exists

- **WHEN** a telemetry record has non-null `usage.cached_input_tokens` and the resolved catalog entry has non-null `cached_input_price_per_mtok`
- **THEN** the cached token cost is included in `cost.estimated_cost`

#### Scenario: Missing rate for observed token category is unresolved

- **WHEN** a telemetry record has `usage.reasoning_tokens = 1000` and the resolved per-token catalog entry has `reasoning_price_per_mtok = null`
- **THEN** `cost.status = "unresolved"`, `cost.estimated_cost = null`, and `cost.unresolved_reason` describes the missing reasoning-token rate

### Requirement: Subscription cost estimation requires a configured usage denominator

For telemetry records whose resolved pricing entry has `billing_mode = "subscription"`, the runtime SHALL estimate effective stage cost only when a configured subscription usage denominator is available for the resolved model.

The denominator SHALL be a positive numeric count of normalized usage units for the relevant subscription period. The stage usage units SHALL come from `usage.total_tokens` when that field is non-null, otherwise from the sum of non-null normalized token categories available for the record.

The effective stage estimate SHALL equal:

`subscription_price * (stage_usage_units / subscription_usage_denominator)`

When the denominator is missing, non-positive, or stage usage units cannot be derived, cost estimation SHALL be unresolved.

#### Scenario: Subscription model with denominator produces estimate

- **WHEN** a telemetry record resolves to a subscription-priced model with `subscription_price = 10.0`, `usage.total_tokens = 100000`, and a configured `subscription_usage_denominator = 50000000`
- **THEN** `cost.status = "estimated"` and `cost.estimated_cost = 0.02`

#### Scenario: Subscription model without denominator is unresolved

- **WHEN** a telemetry record resolves to a subscription-priced model but no subscription usage denominator is configured
- **THEN** `cost.status = "unresolved"`, `cost.estimated_cost = null`, and `cost.unresolved_reason` describes the missing subscription denominator

### Requirement: Cost snapshots preserve the exact pricing inputs used

When `cost.status = "estimated"`, the telemetry record SHALL populate:

- `cost.pricing_catalog_version` with the version returned by `PricingCatalog.get_catalog_version()`
- `cost.price_snapshot` with the exact pricing inputs used for the estimate
- `cost.unresolved_reason` as `null`

The `cost.price_snapshot` SHALL include at minimum:

- `provider`
- `model_id`
- `display_name`
- `billing_mode`
- `currency`
- `effective_date`
- The applicable catalog rate or subscription fields used for the estimate

For subscription estimates, `cost.price_snapshot` SHALL also include the subscription denominator value used for amortization.

The stored snapshot SHALL be sufficient to reproduce `cost.estimated_cost` from the telemetry record without re-reading the current pricing catalog.

#### Scenario: Historical telemetry keeps old catalog values

- **WHEN** a telemetry record is written with `cost.status = "estimated"` using catalog version `"1.0.0"` and later the catalog is updated to version `"1.1.0"`
- **THEN** the stored telemetry record keeps `cost.pricing_catalog_version = "1.0.0"` and the original `cost.price_snapshot` values unchanged

### Requirement: Unresolved cost states are explicit and machine-readable

When cost estimation is attempted but cannot produce an estimate, the telemetry record SHALL set:

- `cost.status = "unresolved"`
- `cost.estimated_cost = null`
- `cost.price_snapshot = null`
- `cost.pricing_catalog_version` to the loaded catalog version when catalog loading succeeded, otherwise `null`
- `cost.unresolved_reason` to a stable human-readable reason

The runtime SHALL use explicit unresolved reasons for at least these cases:

- usage unavailable
- model identity unavailable
- pricing catalog failed to load
- unknown provider
- unknown model
- missing rate for observed token category
- missing subscription denominator
- invalid subscription denominator

#### Scenario: Missing usage becomes unresolved

- **WHEN** a completed telemetry record has `usage.usage_available = false`
- **THEN** `cost.status = "unresolved"`, `cost.price_snapshot = null`, and `cost.unresolved_reason` describes missing usage rather than reporting zero cost

#### Scenario: Unknown model pricing becomes unresolved

- **WHEN** a completed telemetry record has `model.provider` and `model.model_id` populated but `PricingCatalog.resolve()` returns `UnresolvedPrice` with reason `"unknown model"`
- **THEN** `cost.status = "unresolved"` and `cost.unresolved_reason` contains `"unknown model"`

### Requirement: Metrics aggregator reads telemetry and state without mutation

A metrics aggregation module SHALL read `.opsx-plan/telemetry/<plan_name>.jsonl` and `.opsx-plan/<plan_name>.state.json` as input and SHALL NOT write to either file. The aggregator SHALL return typed aggregation results (plan metrics, per-change metrics, stage aggregates, and model-combination leaderboard entries) without modifying telemetry records, state files, or the plan execution environment.

#### Scenario: Aggregator runs without side effects

- **WHEN** an operator runs the aggregator against a completed plan's telemetry and state files
- **THEN** the telemetry JSONL file and state JSON file are unchanged, and aggregation results are returned as typed objects

#### Scenario: Aggregator can re-run deterministically

- **WHEN** the aggregator is run twice against the same telemetry and state files
- **THEN** both runs produce identical aggregation results

### Requirement: Aggregator groups records by run_id

The aggregator SHALL group telemetry records by `run_id`. When no explicit `run_id` is requested, the aggregator SHALL default to the `run_id` with the latest `started_at` timestamp across all records. The aggregator SHALL support an explicit `run_id` parameter to aggregate a specific run.

#### Scenario: Default to latest run in multi-run file

- **WHEN** a telemetry JSONL file contains records from two runs with different `run_id` values
- **THEN** the aggregator selects the run with the most recent `started_at` and returns metrics for that run only, and the `run_id` field in the result matches the selected run

#### Scenario: Explicit run_id selects a specific run

- **WHEN** the aggregator is invoked with a specific `run_id` parameter matching an earlier run
- **THEN** the aggregator returns metrics for only that run, ignoring records from the later run

### Requirement: Plan-level metrics include completion and cost breakdown

Plan-level aggregation SHALL compute the following metrics from telemetry and state:

- `total_changes`: count of all changes in the plan manifest.
- `completed_changes`: changes where plan state marks them done.
- `failed_changes`: changes that failed (including max-rounds exceeded).
- `blocked_changes`: changes explicitly marked blocked in plan state.
- `incomplete_changes`: remaining changes not yet finished.
- `completion_rate`: `completed_changes / total_changes`, or `None` when `total_changes = 0`.
- `success_rate`: `completed_changes / (completed_changes + failed_changes)`, or `None` when the denominator is zero.
- `total_duration_ms`: sum of `duration_ms` for all completed stage records.
- `total_tokens`: sum of `usage.total_tokens` for records where `usage.total_tokens` is non-null.
- `total_estimated_cost`: sum of `cost.estimated_cost` for records where `cost.status = "estimated"`.
- Cost breakdown counts: `estimated_cost_changes`, `unresolved_cost_changes`, `unknown_cost_changes`.

#### Scenario: Plan metrics for a completed 2-change run

- **WHEN** a plan has 2 changes, both with telemetry showing completed implement, review (pass), and archive stages with estimated costs
- **THEN** `total_changes = 2`, `completed_changes = 2`, `failed_changes = 0`, `blocked_changes = 0`, `incomplete_changes = 0`, `completion_rate = 1.0`, `success_rate = 1.0`, `total_estimated_cost` equals the sum of stage costs, `estimated_cost_changes = 2`, `unresolved_cost_changes = 0`

#### Scenario: Plan metrics for a partially complete run

- **WHEN** a plan has 3 changes, 1 completed with estimated cost, 1 failed (max rounds), 1 still in progress
- **THEN** `total_changes = 3`, `completed_changes = 1`, `failed_changes = 1`, `incomplete_changes = 1`, `completion_rate ≈ 0.333`, `success_rate = 0.5`, `estimated_cost_changes = 1`

### Requirement: Change-level metrics derive status from state and telemetry together

Per-change aggregation SHALL derive change status from plan state (authoritative for "is this change done?") combined with telemetry (authoritative for stage outcomes). Change status SHALL be one of:

- `"completed"`: plan state marks the change done.
- `"failed"`: plan state marks it done and telemetry shows non-passing outcomes.
- `"blocked"`: plan state marks it blocked.
- `"incomplete"`: plan state does not mark it done.

Per-change metrics SHALL include: `change_id`, `status`, `total_rounds`, `duration_ms`, `tokens`, `estimated_cost`, `cost_status`, `first_pass_review`, `review_failures`, `no_progress`, `max_rounds_exceeded`, `archive_failed`, `fast_check_failed`.

#### Scenario: Completed change with first-pass review

- **WHEN** a change has one implement stage (status completed), one review stage (verdict "pass"), and one archive stage (status completed), all with model identity and estimated cost
- **THEN** `status = "completed"`, `total_rounds = 1`, `first_pass_review = true`, `review_failures = 0`, `no_progress = false`, `max_rounds_exceeded = false`, `archive_failed = false`

#### Scenario: Failed change with max rounds

- **WHEN** a change cycled through implement-review 3 times (configured max rounds = 3) and the last review still returned "fail"
- **THEN** `status = "failed"`, `total_rounds = 3`, `first_pass_review = false`, `review_failures = 3`, `no_progress = false`, `max_rounds_exceeded = true`

#### Scenario: Blocked change with no recent telemetry

- **WHEN** plan state marks a change blocked and no telemetry records exist for the most recent round
- **THEN** `status = "blocked"`, `total_rounds` equals the last recorded round from state, telemetry-derived fields are `null` where unavailable

### Requirement: Cost aggregates separate estimated from unresolved and unknown costs

Every cost aggregate SHALL separate records with `cost.status = "estimated"` from those with `"unresolved"` or `"unavailable"`. The aggregator SHALL also separate records where model identity is unknown (`model.provider` is `null` or `model.model_id` is `null`) from records where model identity is known but pricing is unresolved.

Zero estimated cost (`estimated_cost = 0.0`) is distinct from absent cost. Zero-cost records SHALL contribute to cost sums and averages. Records with unresolved or unavailable cost status SHALL NOT contribute to cost sums or averages.

Averages SHALL use only the count of records with estimated cost as the denominator. When no records have estimated cost, the average SHALL be `None`.

#### Scenario: Mixed estimated and unresolved costs

- **WHEN** a plan has 3 completed changes: change A has estimated cost $1.00, change B has estimated cost $2.00, change C has all costs unresolved
- **THEN** `estimated_cost_changes = 2`, `unresolved_cost_changes = 1`, `total_estimated_cost = 3.0`, `average_cost_per_change = 1.5` (2/2, not 3/3)

#### Scenario: All costs unresolved

- **WHEN** a completed run has telemetry but no records have `cost.status = "estimated"`
- **THEN** `total_estimated_cost = None`, `average_cost_per_change = None`, `estimated_cost_changes = 0`

#### Scenario: Zero estimated cost is included

- **WHEN** a completed change has a stage with `cost.status = "estimated"` and `cost.estimated_cost = 0.0`
- **THEN** that stage contributes `0.0` to `total_estimated_cost` and the change counts toward `estimated_cost_changes`

### Requirement: Model-combination leaderboard groups by stage role

The aggregator SHALL produce model-combination leaderboard entries grouped by:

- Implementer model: `model.provider` + `model.model_id` from records where `stage = "implement"`.
- Reviewer model: `model.provider` + `model.model_id` from records where `stage = "review"`.
- Archiver model: `model.provider` + `model.model_id` from records where `stage = "archive"`.
- Full combination: the triple `(implementer_model, reviewer_model, archiver_model)` derived from each change's latest implement, review, and archive records.

Each leaderboard entry SHALL include: the model identifier(s), `change_count`, `success_rate`, `first_pass_rate`, `average_rounds`, `average_duration_ms`, `average_tokens`, `average_cost`.

For the full-combination leaderboard, only completed changes with all three model identities known SHALL be included. Changes with unknown model identity for any role SHALL be excluded from the full-combination leaderboard but included in per-role leaderboards where the specific role's identity is known.

#### Scenario: Full-combination leaderboard entry

- **WHEN** 3 completed changes all used `openai:gpt-4o` for implement, `anthropic:claude-sonnet` for review, `openai:gpt-4o-mini` for archive, with estimated costs
- **THEN** the full-combination leaderboard contains one entry with `implementer_model = "openai:gpt-4o"`, `reviewer_model = "anthropic:claude-sonnet"`, `archiver_model = "openai:gpt-4o-mini"`, `change_count = 3`, and all rate/average fields computed from those 3 changes

#### Scenario: Unknown model excluded from full combination

- **WHEN** a completed change has known implementer and reviewer models but unknown archiver model
- **THEN** the change is excluded from the full-combination leaderboard but included in the per-role implementer and reviewer leaderboards

#### Scenario: Per-role leaderboard with partial model identity

- **WHEN** a completed change has `openai:gpt-4o` for implement but unknown reviewer and archiver models
- **THEN** the change contributes to the implementer leaderboard entry for `openai:gpt-4o` but not to the reviewer or archiver leaderboards

### Requirement: Aggregator handles missing or incomplete input gracefully

When telemetry or state files are missing, empty, or incomplete, the aggregator SHALL return a result with available metrics populated and a `warnings` list describing what was missing. The aggregator SHALL NOT raise an unhandled exception for missing files or empty telemetry.

#### Scenario: Missing telemetry file

- **WHEN** `.opsx-plan/telemetry/<plan_name>.jsonl` does not exist
- **THEN** the aggregator returns an `AggregationResult` with empty change metrics, no model leaderboard entries, and a warning indicating the telemetry file was not found

#### Scenario: Missing state file

- **WHEN** `.opsx-plan/<plan_name>.state.json` does not exist but telemetry is present
- **THEN** the aggregator derives change identity from telemetry records, returns available metrics, and includes a warning about the missing state file

#### Scenario: Empty telemetry file

- **WHEN** the telemetry JSONL file exists but contains zero records
- **THEN** the aggregator returns `total_changes` from the plan state (if available), all change statuses as `"incomplete"`, and a warning about empty telemetry

### Requirement: Aggregator emits warnings for data quality issues

The aggregator SHALL include a `warnings` list in the `AggregationResult` for the following conditions:

- Telemetry file not found.
- Plan state file not found.
- Telemetry file contains records with unknown `schema_version`.
- A change has telemetry records but no corresponding entry in plan state.
- A change in plan state has no telemetry records.
- Conflicting status between telemetry and plan state for a change.
- Records with `cost.status = "unresolved"` (counted but flagged).

#### Scenario: Warnings emitted for discrepancies

- **WHEN** telemetry shows a completed archive stage for a change but plan state does not mark it done
- **THEN** the `warnings` list includes a warning about the status conflict for that `change_id`

### Requirement: Stage-level aggregates compute descriptive statistics

Stage-level aggregation SHALL compute:

- `average_rounds` and `median_rounds` across completed changes.
- `average_duration_implement`, `average_duration_review`, `average_duration_archive` from completed stage records.
- `review_failure_rate` as `total_review_failures / total_review_stages`.
- `average_tokens_per_change` and `average_cost_per_change` using only completed changes with at least one estimated cost record.

All averages SHALL be `None` when the denominator is zero.

#### Scenario: Stage aggregates for a multi-change run

- **WHEN** 2 completed changes have round counts of 1 and 3, review failures of 0 and 2, and implement durations of 30000ms and 45000ms
- **THEN** `average_rounds = 2.0`, `median_rounds = 2.0`, `average_duration_implement = 37500.0`, `review_failure_rate = 2 / (total review stages across both changes)`

