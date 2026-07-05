## Context

The `plan-run-observability` telemetry schema defines cost estimation slots (`cost.status`, `cost.price_snapshot`, `cost.estimated_cost`) but provides no pricing data source. Downstream changes (`estimate-stage-token-costs`) need to resolve `(provider, model_id)` pairs to billing mode, per-token rates, subscription prices, and currency so they can compute reproducible dollar estimates from telemetry token counts.

This change delivers a local, operator-maintainable pricing catalog and a loader API. The catalog is intentionally a static file (TOML) rather than a live API to keep cost estimation deterministic and offline-capable.

## Goals / Non-Goals

**Goals:**

- Provide a TOML catalog file with entries covering provider, model_id, billing_mode, currency, per-million-token rates (input, output, cached_input, reasoning), subscription pricing, effective_date, and notes.
- Provide a `PricingCatalog` loader that validates entries at load time and surfaces clear errors for malformed data.
- Provide a `resolve(provider, model_id)` method that returns a `ResolvedPrice` (with billing mode, rates, currency) or an `UnresolvedPrice` (with a reason) when no entry matches.
- Define typed dataclasses for catalog entries and resolution results so downstream code has a stable contract.
- Include representative catalog entries for common opencode models (no network access required for tests or operation).
- Document within the catalog file how operators add or update entries.

**Non-Goals:**

- Do not fetch live provider prices from APIs or websites.
- Do not enforce pricing freshness or warn about stale entries — operators own catalog maintenance.
- Do not change installed agent model configuration or map aliases to model_ids.
- Do not compute dollar costs — that is `estimate-stage-token-costs`'s responsibility.
- Do not handle model aliases in the catalog — aliases are resolved in the agent configuration layer before the lookup.

## Decisions

### 1. Use TOML as the catalog format

The catalog file is TOML with a `[catalog]` header containing `version` and `updated` metadata, followed by an array of `[[entries]]` tables.

```toml
[catalog]
version = "1.0.0"
updated = "2026-07-05"

[[entries]]
provider = "openai"
model_id = "gpt-4o"
display_name = "GPT-4o"
billing_mode = "per_token"
currency = "USD"
input_price_per_mtok = 2.50   # USD per million input tokens
output_price_per_mtok = 10.00 # USD per million output tokens
cached_input_price_per_mtok = 1.25  # USD per million cached input tokens
effective_date = "2025-01-01"
notes = "Standard pricing as of 2025"

[[entries]]
provider = "anthropic"
model_id = "claude-sonnet-4-20250514"
display_name = "Claude Sonnet 4"
billing_mode = "per_token"
currency = "USD"
input_price_per_mtok = 3.00
output_price_per_mtok = 15.00
cached_input_price_per_mtok = 0.30
effective_date = "2025-06-01"

[[entries]]
provider = "openai"
model_id = "gpt-4o"
display_name = "GPT-4o (updated)"
billing_mode = "per_token"
currency = "USD"
input_price_per_mtok = 2.00
output_price_per_mtok = 8.00
cached_input_price_per_mtok = 1.00
effective_date = "2025-07-01"
notes = "Price reduction"
```

**Rationale:** TOML is already used in the codebase (`plan.example.toml`), is human-writable, supports comments, and has a clear array-of-tables syntax for repeating entries. It is parseable without dependencies (Python 3.11+ includes `tomllib`; older versions can use `tomli`).

**Alternatives considered:** JSON (no comments, poor ergonomics for operator edits), YAML (whitespace-sensitive, less common in this codebase), CSV (no nested structure, poor for subscription pricing fields).

### 2. Deduplicate by effective_date — latest wins

When multiple catalog entries match the same `(provider, model_id)` pair, the entry with the latest `effective_date` is used. This allows operators to add updated pricing without deleting past entries, preserving a price history.

**Rationale:** Enables price history tracking in the catalog file. Downstream cost estimation stores the effective pricing in `price_snapshot`, so historical telemetry records remain reproducible even after the catalog is updated.

**Alternatives considered:** Strict uniqueness enforcement (forces deletion of old entries, loses history), first-match-wins (non-deterministic on file sort order).

### 3. Return typed result objects: ResolvedPrice and UnresolvedPrice

The `resolve()` method returns a `ResolvedPrice` dataclass when a matching entry is found or an `UnresolvedPrice` dataclass when no entry matches. This forces callers to handle the unresolved case explicitly.

```python
@dataclass
class ResolvedPrice:
    provider: str
    model_id: str
    display_name: str
    billing_mode: str  # "per_token" | "subscription"
    currency: str
    input_price_per_mtok: float | None
    output_price_per_mtok: float | None
    cached_input_price_per_mtok: float | None
    reasoning_price_per_mtok: float | None
    subscription_period: str | None  # "monthly" | "yearly"
    subscription_price: float | None
    effective_date: str
    notes: str | None

@dataclass
class UnresolvedPrice:
    provider: str | None
    model_id: str | None
    reason: str
```

**Rationale:** Typed result objects give downstream code a stable contract and prevent the "return None" anti-pattern where a None value's meaning must be inferred from context. The `UnresolvedPrice.reason` field carries the specific resolution failure (e.g., "unknown provider", "unknown model") directly to telemetry's `cost.unresolved_reason`.

**Alternatives considered:** Returning `Optional[dict]` (loses type information, forces downstream to inspect dict keys), raising exceptions for unresolved lookups (forces try/except at every call site, loses the reason text unless embedded in the exception).

### 4. Validate at load time, not at lookup time

The catalog loader validates all entries at construction time (missing required fields, negative rates, invalid billing_mode enum values, invalid currency codes). Any malformed entry causes the loader to raise a `CatalogLoadError` with the entry index and specific field error.

**Rationale:** Fail-fast behavior ensures operators catch malformed entries immediately rather than discovering them mid-run when `estimate-stage-token-costs` encounters a null price snapshot at the end of a long plan execution.

**Alternatives considered:** Skipping malformed entries and logging warnings (silently produces incorrect cost estimates for those models), validating at lookup time (duplicates validation logic across every lookup call).

### 5. Use per-million-token rates (not per-token)

All per-token pricing rates are expressed in currency units per million tokens (e.g., `input_price_per_mtok = 2.50` means $2.50 USD per 1,000,000 input tokens). This matches the industry convention used by OpenAI, Anthropic, and Google, and avoids tiny floating-point values prone to precision issues.

The downstream cost estimator divides token counts by 1,000,000 and multiplies by the per-mtok rate.

**Rationale:** Industry standard, operator-readable, avoids floating-point precision issues with sub-cent per-token rates.

### 6. Store per-million-token rates as optional floats

For per-token models, `input_price_per_mtok`, `output_price_per_mtok`, `cached_input_price_per_mtok`, and `reasoning_price_per_mtok` are populated as applicable. For subscription models, these fields are `None` and `subscription_period` / `subscription_price` are populated instead. The `billing_mode` field determines which rate fields are relevant.

A per-token entry MUST have at minimum `input_price_per_mtok` populated. Other token rate fields are optional and default to `None` when not applicable (e.g., a model that does not report cache hits separately).

**Rationale:** Not all models have cache or reasoning token breakdowns. Making these fields optional avoids requiring operators to fabricate zero rates for features their models don't support.

## Catalog File Location

The catalog file is stored at `lib/pricing/catalog.toml`, not in `.opsx-plan/`. This is configuration data, not runtime telemetry. It belongs with the code so it is version-controlled alongside the loader and tests.

The loader is in `lib/pricing/loader.py` with a `@dataclass` types module either inline or in `lib/pricing/types.py`.

## Risks / Trade-offs

- [Risk] Catalog becomes stale if operators do not update pricing. → Mitigation: The `effective_date` field and catalog `[[entries]]` array support historical tracking; stale data produces incorrect cost estimates but does not break the telemetry pipeline.
- [Risk] Per-token rates for cache and reasoning tokens may not be consistently available across providers. → Mitigation: These fields are optional; `estimate-stage-token-costs` handles `None` rate fields by skipping that token category in cost calculation.
- [Risk] Operator edits a malformed catalog, breaking plan runs. → Mitigation: Load-time validation with clear error messages; `CatalogLoadError` includes the entry index and field name. Downstream changes (`estimate-stage-token-costs`) handle load failures gracefully without aborting the plan run.
- [Risk] Subscription-billed models (like GitHub Copilot, Cursor) lack per-token rates, making direct cost comparison with per-token models difficult. → Mitigation: The subscription billing mode in the catalog and the `cost.price_snapshot.shape` handles this case (`billing_mode: "subscription"`). The downstream aggregation change will compute effective amortized cost when a usage denominator is configured.

## Migration Plan

No migration is required. This change adds new files only. No existing code paths are modified.

When `estimate-stage-token-costs` is implemented, it will import and instantiate `PricingCatalog` from `lib/pricing/loader.py`.

## Open Questions

- Should the catalog include a `deprecated` or `superseded_by` field for discontinued models rather than just relying on `effective_date`? Deferring for now — the `notes` field can carry this information in prose.
- Should provider aliases (e.g., `"deepseek"` → `"deepseek"`) be normalized in the catalog or in the caller? Deferring to `capture-worker-usage-metadata` — it normalizes model identity in telemetry records before cost estimation looks up pricing.
