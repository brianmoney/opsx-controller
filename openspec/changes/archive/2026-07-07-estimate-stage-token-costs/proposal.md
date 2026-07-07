## Why

Direct stage telemetry now records durable usage and model metadata, and the repo now has a local pricing catalog. The remaining gap is converting those two inputs into reproducible stage-level cost estimates that can be compared across plans, changes, and model combinations.

Without this change, telemetry can show duration and token consumption but cannot answer the planning goal of normalized cost comparison. The schema already reserves `cost.*` fields for this purpose, but those fields remain unset after stage completion.

## What Changes

- Add stage-cost estimation logic that reads the telemetry `usage` and `model` objects, resolves model pricing through `PricingCatalog`, and fills the existing `cost` object in completed telemetry records.
- Support per-token list-price estimation for models with catalog token rates and subscription amortization for models with a configured observed usage denominator.
- Persist `cost.status`, `cost.pricing_catalog_version`, `cost.price_snapshot`, `cost.unresolved_reason`, and `cost.estimated_cost` so historical records remain reproducible after the pricing catalog changes.
- Keep cost estimation best-effort: unresolved pricing, missing usage, or missing subscription denominator data must not fail the stage or the plan run.
- Add unit tests covering per-token pricing, cached tokens, missing usage, unknown pricing, and subscription models without enough denominator data.

No billing reconciliation, provider invoice import, report rendering, or model-selection policy is included.

## Capabilities

### Modified Capabilities

- `plan-run-observability`: Adds deterministic stage-level cost estimation and unresolved-state reporting to telemetry records.

### New Capabilities

- None.

## Impact

- Affected specs: `openspec/specs/plan-run-observability/spec.md` (modified — defines cost estimation behavior, unresolved states, and price snapshot contents).
- Affected code: `orchestrator/opsx-plan.py` telemetry cost population, plus a small pricing helper module if needed.
- Affected tests: `tests/orchestrator/test_opsx_plan.py` and/or `tests/lib/pricing/` for cost-estimation coverage.
- Downstream metrics and report changes can rely on explicit `estimated` vs `unresolved` cost states without recalculating historical prices from the current catalog.
