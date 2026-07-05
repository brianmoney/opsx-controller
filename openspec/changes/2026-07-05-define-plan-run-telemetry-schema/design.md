## Context

`opsx-plan` drives implement, review, and archive workers per change but does not persist structured telemetry records. Operators comparing model choices currently inspect ad-hoc log files. The plan calls for a durable telemetry foundation before any runtime writes, pricing catalogs, or dashboards are built.

This change defines the schema contract only. No runtime code is modified.

## Goals / Non-Goals

**Goals:**

- Define a JSONL telemetry record format with required identifiers, optional usage fields, and cost-estimation placeholders.
- Distinguish missing usage (`null`) from zero usage (`0`) so reports can separate "provider did not report" from "free invocation."
- Provide slots for per-token and subscription billing mode cost fields without requiring cost calculation logic.
- Specify `.opsx-plan/telemetry/` as the storage root and document how schema versioning preserves historical readability.
- Produce OpenSpec requirements that downstream changes can implement against.

**Non-Goals:**

- Do not implement runtime telemetry writes, log parsing, or cost calculation.
- Do not add pricing tables, dashboards, or provider-specific usage extraction.
- Do not modify the `opsx-plan` control loop or state schema.
- Do not prescribe which fields are populated by which change in the plan.

## Decisions

1. **Use JSONL as the storage format.** Each telemetry record is a single JSON object on its own line. JSONL is append-only, human-readable, trivial to concatenate and grep, and avoids the complexity of streaming JSON arrays or binary formats.
   Rationale: JSONL is the simplest append-only format that supports incremental writes from concurrent processes and allows operators to `tail -f` telemetry files during runs.
   Alternatives considered: JSON arrays (require final bracketing), CSV (poorly handles nested objects like invocation args or token breakdowns), SQLite (adds a binary dependency when operators only need grep-like access).

2. **Store telemetry under `.opsx-plan/telemetry/` with plan-scoped filenames.** Telemetry files are named `<plan_name>.jsonl` and live under `.opsx-plan/telemetry/`. This separates telemetry from plan state (`<plan_name>.state.json`) and logs (`.opsx-plan/logs/`), and allows operators to archive or delete telemetry independently of plan execution state.
   Rationale: Plan-scoped files naturally scope queries to a single plan run without requiring a database index.
   Alternatives considered: One JSONL file per change (fragments telemetry, complicates cross-change aggregation), per-run directories (adds directory management overhead for operators who only care about one plan).

3. **Version the schema with a `schema_version` field on every record.** Each record includes a `schema_version` integer. Readers can branch on this field to handle format changes. The initial version is `1`.
   Rationale: Schema must evolve as new providers, model fields, or billing modes appear. Versioning at the record level allows historical files to remain readable even when the current schema has grown.
   Alternatives considered: Versioning at the file level (one version per file; harder to detect mixed-version files from interrupted runs), no versioning (unmaintainable as the contract grows).

4. **Use `null` for unavailable usage, `0` for zero usage.** Token fields (`input_tokens`, `output_tokens`, `cached_input_tokens`, `reasoning_tokens`, `total_tokens`) are nullable integers. A `null` value means the provider did not report usage (or extraction failed). A `0` value means the provider reported zero usage for that category.
   Rationale: Reports and dashboards must distinguish "unknown cost" from "zero cost" to avoid misleading conclusions. Null-vs-zero is the clearest way to encode this in JSON without sentinel values.
   Alternatives considered: A separate `usage_available` boolean flag (redundant with null checking and error-prone if a subset of token fields are populated), negative sentinel values (violates JSON type expectations).

5. **Cost fields use a `cost` sub-object with `status`, `pricing_catalog_version`, and `price_snapshot`.** The `cost` object records cost estimation results without requiring actual cost calculation. `status` is an enum: `"unavailable"` (before estimation runs), `"unresolved"` (estimation attempted but pricing or usage missing), `"estimated"` (cost calculated). `price_snapshot` stores the rates and billing mode used so reports can reproduce estimates from stored telemetry alone.
   Rationale: Cost calculation is a separate concern (Phase 2). The schema reserves well-named slots so Phase 2 changes have a target contract without modifying this spec.
   Alternatives considered: Flat cost fields like `estimated_cost_usd` (loses provenance and reproducibility), omitting cost fields entirely in Phase 1 (forces a schema version bump in Phase 2).

6. **Model identity uses provider, model_id, and model_alias fields.** `provider` names the API provider (e.g. `"openai"`, `"anthropic"`, `"google"`). `model_id` is the canonical model string provided by the runtime (e.g. `"gpt-4o"`). `model_alias` is the user-configured alias if the agent configuration uses an alias instead of a raw model id (nullable).
   Rationale: Pricing catalogs join on `provider` and `model_id`. The `model_alias` field preserves the operator's local naming without requiring the pricing catalog to understand aliases.
   Alternatives considered: A single `model` field (loses provider context, complicates pricing lookups).

## Risks / Trade-offs

- [Risk] JSONL files grow unbounded over many plan runs. -> Mitigation: JSONL is trivially compressible with `gzip`; operators control retention externally; a future change could add rotation or archival.
- [Risk] Schema version bumps require all readers to handle multiple versions. -> Mitigation: version is an integer at the record level; readers need only branch on version number; this is a well-understood pattern in event-sourced systems.
- [Risk] The `cost` placeholder may need restructuring when Phase 2 pricing logic is designed. -> Mitigation: the `cost` sub-object is intentionally minimal (status + version + snapshot); Phase 2 changes own adding fields within that sub-object and can bump `schema_version` if the shape changes materially.

## Migration Plan

No migration is required. This change adds only specification and documentation artifacts under `openspec/specs/plan-run-observability/`. No runtime code paths are modified.

When `record-direct-stage-telemetry` is implemented, it will create `.opsx-plan/telemetry/` and write records conforming to this schema.

## Open Questions

- Should telemetry records include a UUID or rely on `(plan_name, run_id, change_id, stage, round)` for uniqueness? (Defer to `record-direct-stage-telemetry`; the schema reserves space for both approaches.)
- Should the `cost.price_snapshot` store full pricing catalog entries or just the rates actually used? (Defer to `estimate-stage-token-costs`; the schema stores a snapshot blob that Phase 2 defines.)
