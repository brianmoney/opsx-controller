## 1. Pricing Catalog Type Definitions

- [ ] 1.1 Define `CatalogEntry` dataclass with fields: `provider`, `model_id`, `display_name`, `billing_mode`, `currency`, `input_price_per_mtok`, `output_price_per_mtok`, `cached_input_price_per_mtok`, `reasoning_price_per_mtok`, `subscription_period`, `subscription_price`, `effective_date`, `notes`.
- [ ] 1.2 Define `CatalogMetadata` dataclass with `version` and `updated` fields.
- [ ] 1.3 Define `ResolvedPrice` dataclass with all fields needed by downstream cost estimation (`provider`, `model_id`, `display_name`, `billing_mode`, `currency`, per-mtok rates, subscription fields, `effective_date`, `notes`).
- [ ] 1.4 Define `UnresolvedPrice` dataclass with `provider`, `model_id`, and `reason` fields.

## 2. Pricing Catalog File

- [ ] 2.1 Create `lib/pricing/__init__.py` with package docstring.
- [ ] 2.2 Create `lib/pricing/catalog.toml` with `[catalog]` metadata and representative `[[entries]]` for common OpenCode models (e.g., GPT-4o, Claude Sonnet 4, Gemini 2.0 Flash, DeepSeek V3, etc.) covering both per-token and subscription billing modes.
- [ ] 2.3 Document in `catalog.toml` comments how operators add new entries, update rates, and handle model deprecation.

## 3. Pricing Catalog Loader

- [ ] 3.1 Implement `PricingCatalog` class in `lib/pricing/loader.py` that loads and parses `lib/pricing/catalog.toml`.
- [ ] 3.2 Implement load-time validation: verify required fields are present (`provider`, `model_id`, `billing_mode`, `currency`), `billing_mode` is `"per_token"` or `"subscription"`, `currency` is a valid ISO 4217 code, `input_price_per_mtok` is non-negative when present, subscription fields are present when `billing_mode` is `"subscription"`, `effective_date` is ISO-8601 date format.
- [ ] 3.3 Implement `CatalogLoadError` exception with entry index and field-specific error messages.
- [ ] 3.4 Implement `resolve(provider: str, model_id: str)` method that returns a `ResolvedPrice` when a matching entry exists (latest by `effective_date` when multiple entries match) or an `UnresolvedPrice` with an appropriate reason when no entry matches.
- [ ] 3.5 Implement `get_catalog_version()` method that returns the `version` string from catalog metadata.
- [ ] 3.6 Ensure the loader handles an empty catalog (no `[[entries]]`) gracefully, returning `UnresolvedPrice` for all lookups.

## 4. Unit Tests

- [ ] 4.1 Create `tests/lib/pricing/` directory and `__init__.py`.
- [ ] 4.2 Test that `resolve()` returns a `ResolvedPrice` for a known per-token model with all rate fields populated correctly.
- [ ] 4.3 Test that `resolve()` returns a `ResolvedPrice` for a known subscription model with subscription fields populated and token rate fields `None`.
- [ ] 4.4 Test that `resolve()` returns an `UnresolvedPrice` with `reason` containing "unknown model" when the model_id does not match any entry.
- [ ] 4.5 Test that `resolve()` returns an `UnresolvedPrice` with `reason` containing "unknown provider" when the provider does not match any entry.
- [ ] 4.6 Test that when multiple entries exist for the same `(provider, model_id)`, the one with the latest `effective_date` is returned.
- [ ] 4.7 Test that loading a catalog with a missing required field (`provider`, `model_id`, `billing_mode`, `currency`) raises a `CatalogLoadError` with the entry index and field name in the error message.
- [ ] 4.8 Test that loading a catalog with a negative `input_price_per_mtok` raises a `CatalogLoadError`.
- [ ] 4.9 Test that loading a catalog with an unknown `billing_mode` value raises a `CatalogLoadError`.
- [ ] 4.10 Test that loading a catalog with a subscription entry missing `subscription_price` raises a `CatalogLoadError`.
- [ ] 4.11 Test that loading an empty catalog (valid TOML with no entries) succeeds and all `resolve()` calls return `UnresolvedPrice`.
- [ ] 4.12 Test that `get_catalog_version()` returns the version string from catalog metadata.
- [ ] 4.13 Test that loading a catalog with a per-token model that omits optional rate fields (`output_price_per_mtok`, `cached_input_price_per_mtok`, `reasoning_price_per_mtok`) succeeds and those fields are `None` in the `ResolvedPrice`.
- [ ] 4.14 Test that `resolve()` handles provider and model_id strings that differ only by whitespace (no normalization — consumers must provide exact matches).

## 5. Verification

- [ ] 5.1 Run existing test suite and ensure no regressions.
- [ ] 5.2 Run `openspec validate add-model-pricing-catalog --strict` and ensure zero findings.
- [ ] 5.3 Verify that the catalog file contains representative entries covering at least 3 providers and both billing modes.
