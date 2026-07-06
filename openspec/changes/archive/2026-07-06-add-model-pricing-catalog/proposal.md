## Why

The `plan-run-observability` specification reserves cost estimation slots in telemetry records but provides no mechanism to look up model pricing. Downstream cost estimation (`estimate-stage-token-costs`) requires a local, operator-maintained pricing catalog to translate token usage into normalized dollar costs. Without a pricing catalog, cost estimation cannot produce reproducible estimates and reports cannot separate "unknown model" from "zero cost."

This change provides the catalog file, loader, and lookup API so that all following Phase 2 and Phase 3 changes have a deterministic pricing source.

## What Changes

- Add a versioned, operator-maintainable pricing catalog file (TOML) at `lib/pricing/catalog.toml` covering provider, model id, billing mode, currency, per-million token rates, subscription pricing, effective date, and notes.
- Add a `PricingCatalog` loader class in `lib/pricing/loader.py` that parses the catalog, validates entries, and exposes a `resolve(provider, model_id)` lookup returning a `ResolvedPrice` with billing mode, rates, and metadata or an `UnresolvedPrice` with a reason when no entry matches.
- Define dataclass types for catalog entries and resolved results.
- Add validation at load time for malformed entries (missing required fields, negative rates, invalid billing modes, non-ISO-4217 currency codes in catalog entries).
- Document in the catalog file how operators update rates and add new models.
- Add unit tests covering: valid per-token entry resolution, valid subscription entry resolution, missing model, unknown provider, malformed entries (missing fields, negative rates, unknown billing mode), and multiple entries for the same model (latest by effective_date wins).

No live price fetching, enforcing pricing freshness, or changes to installed agent model configuration are included.

## Capabilities

### Modified Capabilities

- `plan-run-observability`: Adds a versioned pricing catalog and loader that `estimate-stage-token-costs` will use to populate `cost.price_snapshot` and `cost.estimated_cost` fields in telemetry records.

### New Capabilities

- None.

## Impact

- Affected specs: `openspec/specs/plan-run-observability/spec.md` (modified — adds pricing catalog format, loader behavior, and lookup contract requirements).
- New files: `lib/pricing/catalog.toml` (versioned pricing data), `lib/pricing/loader.py` (parser and lookup API), `lib/pricing/__init__.py` (package init).
- New test files: `tests/lib/pricing/` directory with unit tests for catalog loading and resolution.
- Downstream change `estimate-stage-token-costs` will depend on the `PricingCatalog` loader and `ResolvedPrice` types defined here.
- No runtime telemetry writes or cost calculations are performed by this change.
