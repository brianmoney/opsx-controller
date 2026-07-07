## Context

`record-direct-stage-telemetry` writes one JSONL record per direct stage invocation. `capture-worker-usage-metadata` populates nullable token counts and model identity. `add-model-pricing-catalog` provides a local `PricingCatalog` loader that resolves `(provider, model_id)` to either a token-priced or subscription-priced catalog entry.

This change connects those pieces. It computes a deterministic estimate after a stage finishes, stores the price inputs alongside the numeric result, and leaves an explicit unresolved state when any required input is missing.

## Goals / Non-Goals

**Goals:**

- Estimate per-stage cost for token-billed models using stored token counts and catalog per-million-token rates.
- Estimate per-stage effective cost for subscription-billed models when an observed usage denominator is configured.
- Persist enough pricing detail in telemetry to reproduce the stored estimate without re-reading a newer catalog version.
- Distinguish unresolved cost from zero cost and from not-yet-attempted cost estimation.
- Keep estimation non-fatal: telemetry and control-loop outcomes stay unchanged when pricing cannot be resolved.

**Non-Goals:**

- Do not reconcile estimates against provider invoices or subscription statements.
- Do not fetch pricing or usage denominators from remote services.
- Do not add aggregation, reporting, or dashboard rendering.
- Do not choose which model is best or alter stage retry behavior based on cost.

## Decisions

### 1. Estimate cost immediately when writing completed telemetry

The orchestrator should compute cost during direct-stage telemetry construction, using the same stage result, usage metadata, and model metadata already in memory.

If estimation succeeds, the record is written once with `cost.status = "estimated"`. If estimation cannot be completed, the record is still written once with `cost.status = "unresolved"` and an explicit `cost.unresolved_reason`.

**Rationale:** Keeps telemetry self-contained and avoids a second pass that would need to reopen and rewrite JSONL records.

### 2. Preserve `unavailable` only for records that were never eligible for estimation

The existing default `cost.status = "unavailable"` remains valid for code paths that do not attempt estimation, such as older telemetry records or non-updated writers. Once this change is implemented in the direct-stage path, completed direct stage records should end in either `"estimated"` or `"unresolved"`.

**Rationale:** Preserves backward compatibility for historical records while giving new records a stronger contract.

### 3. Per-token models sum category-specific costs

For `billing_mode = "per_token"`, the estimate is the sum of each available token category divided by 1,000,000 and multiplied by its corresponding catalog rate:

- `input_tokens` × `input_price_per_mtok`
- `output_tokens` × `output_price_per_mtok`
- `cached_input_tokens` × `cached_input_price_per_mtok`
- `reasoning_tokens` × `reasoning_price_per_mtok`

Token categories with `null` usage contribute nothing because they are unknown. A category with `0` usage contributes `0`. If a token category has a positive usage count but the matching catalog rate is missing, estimation becomes unresolved rather than silently undercounting cost.

**Rationale:** This keeps estimates conservative and reproducible. Missing rate data for observed usage is materially different from zero cost.

### 4. Subscription models require an explicit observed-usage denominator

For `billing_mode = "subscription"`, estimation requires a configured observed usage denominator representing the number of usage units over which the subscription price is amortized for the relevant subscription period.

The denominator should be expressed as normalized units aligned to the telemetry record's `usage.total_tokens` when available, or to the sum of known token categories otherwise. The effective estimate is:

`subscription_price * (stage_usage_units / configured_subscription_usage_denominator)`

When the denominator is absent, zero, negative, or incompatible with the available usage shape, estimation is unresolved.

**Rationale:** Subscription cost cannot be normalized per stage without an operator-supplied amortization basis.

### 5. Store a complete price snapshot in telemetry

The stored `cost.price_snapshot` should include:

- `provider`
- `model_id`
- `display_name`
- `billing_mode`
- `currency`
- `effective_date`
- `catalog_version`
- Rate or subscription fields copied from the resolved catalog entry
- Subscription denominator fields used for the estimate when `billing_mode = "subscription"`

This snapshot is copied from the resolved catalog entry and the local denominator configuration rather than referencing the current catalog by pointer.

**Rationale:** Historical telemetry stays reproducible even after catalog entries or denominator configuration change.

### 6. Use explicit unresolved reasons

Cost estimation should return structured unresolved reasons for at least these conditions:

- usage unavailable
- model identity unavailable
- pricing catalog failed to load
- unknown provider
- unknown model
- missing rate for observed token category
- missing subscription denominator
- invalid subscription denominator

These reasons should map directly to `cost.unresolved_reason`.

**Rationale:** Downstream reports need to separate different failure modes instead of collapsing them into a generic null.

## Price Snapshot Shape

For per-token models:

```json
{
  "provider": "openai",
  "model_id": "gpt-4o",
  "display_name": "GPT-4o",
  "billing_mode": "per_token",
  "currency": "USD",
  "effective_date": "2025-07-01",
  "catalog_version": "1.0.0",
  "input_price_per_mtok": 2.0,
  "output_price_per_mtok": 8.0,
  "cached_input_price_per_mtok": 1.0,
  "reasoning_price_per_mtok": null
}
```

For subscription models:

```json
{
  "provider": "github",
  "model_id": "copilot",
  "display_name": "GitHub Copilot",
  "billing_mode": "subscription",
  "currency": "USD",
  "effective_date": "2025-07-01",
  "catalog_version": "1.0.0",
  "subscription_period": "monthly",
  "subscription_price": 10.0,
  "usage_denominator_units": 50000000,
  "usage_denominator_source": "config"
}
```

## Risks / Trade-offs

- [Risk] Catalog entries may omit some optional token rates while telemetry reports those token categories. -> Mitigation: mark the estimate unresolved instead of silently dropping priced usage.
- [Risk] Subscription amortization can look more precise than it is. -> Mitigation: require explicit denominator configuration and persist it in the snapshot.
- [Risk] Cost estimation code could fail because of malformed pricing configuration. -> Mitigation: treat catalog-load or denominator errors as unresolved cost states, not stage failures.

## Migration Plan

No schema migration is required. Existing telemetry records remain valid with `cost.status = "unavailable"`.

New direct-stage telemetry records written after this change may store `cost.status = "estimated"` or `"unresolved"` and a populated `price_snapshot` when estimation succeeds.

## Open Questions

- The exact location and shape of subscription denominator configuration is not yet established elsewhere in the repo. This change should define one minimal local configuration path during implementation and document it in code and tests.
