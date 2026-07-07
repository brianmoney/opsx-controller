## ADDED Requirements

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
