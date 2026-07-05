## Why

OPSX plan runs currently lack durable telemetry for comparing implementer, reviewer, and archiver model choices. Without a shared record contract, every downstream efficiency tool (cost estimation, metrics aggregation, dashboards) must re-parse ad-hoc log output. Defining the telemetry schema first ensures all later runtime writes, pricing catalogs, and reports share a stable, well-documented contract.

## What Changes

- Define the durable JSONL telemetry record format for plan runs, including plan name, run id, change id, stage, round, status, timestamps, duration, invocation command shape, model identity fields, parsed worker result summary, token usage fields, and cost-estimation placeholders.
- Document where telemetry records are stored under `.opsx-plan/telemetry/` and how historical records remain readable as the schema evolves.
- Specify required vs. optional fields, null-vs-zero semantics for missing usage, and how subscription and per-token billing modes will be represented without requiring actual cost calculation.
- Add OpenSpec requirements for the telemetry schema as a new capability.

## Capabilities

### New Capabilities

- `plan-run-observability`: Defines the durable JSONL telemetry contract for plan, change, and stage invocations, including model identity, token usage, cost-estimation slots, and schema versioning that preserves historical readability.

### Modified Capabilities

- None.

## Impact

- Affected specs: new `openspec/specs/plan-run-observability/spec.md` (proposed capability).
- No runtime code changes in this change; this is a specification and documentation baseline.
- Downstream changes (`record-direct-stage-telemetry`, `capture-worker-usage-metadata`, `add-model-pricing-catalog`, `estimate-stage-token-costs`, `aggregate-plan-efficiency-metrics`, `add-opsx-plan-report-command`, `export-plan-efficiency-dashboard`) will implement against this schema.
- Storage location `.opsx-plan/telemetry/` is new but no writes occur until `record-direct-stage-telemetry`.
