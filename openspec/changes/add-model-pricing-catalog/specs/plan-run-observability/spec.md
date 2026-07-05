## ADDED Requirements

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
