## 1. Telemetry Schema Specification

- [x] 1.1 Define the telemetry record JSON schema with required identifier fields (plan_name, run_id, change_id, stage, round, status, timestamps, duration_ms).
- [x] 1.2 Define the invocation command shape fields (adapter, worker_command, args_sample, timeout_seconds, retry_attempt).
- [x] 1.3 Define the model identity fields (provider, model_id, model_alias) with null-vs-absent semantics.
- [x] 1.4 Define the parsed worker result summary fields (stage_status, verdict, critical_count, warning_count, note_count, error_message, log_path).
- [x] 1.5 Define the token usage fields (input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, total_tokens) with `null` for unavailable and `0` for zero usage, plus `usage_source` and `usage_available` fields.
- [x] 1.6 Define the cost estimation placeholder fields (cost.status, cost.pricing_catalog_version, cost.price_snapshot, cost.unresolved_reason) with enum values for status.
- [x] 1.7 Define the schema_version field and document backward-compatibility expectations for readers.
- [x] 1.8 Document the storage root `.opsx-plan/telemetry/` and the plan-scoped `<plan_name>.jsonl` naming convention.

## 2. OpenSpec Capability Requirements

- [x] 2.1 Write `specs/plan-run-observability/spec.md` with formal requirements covering record identity, field semantics, null-vs-zero usage, schema versioning, storage location, and cost placeholder shape.
- [x] 2.2 Include scenarios for: a complete record with all fields populated, a record with missing usage, a record with zero usage, a record before cost estimation, a record with unresolved cost, and a record with an estimated cost.

## 3. Verification

- [x] 3.1 Run `openspec validate define-plan-run-telemetry-schema --strict` and ensure zero findings.
- [x] 3.2 Verify the schema names all required and optional fields and matches the scope described in the plan.
- [x] 3.3 Confirm the schema distinguishes missing usage from zero usage and documents subscription/per-token billing mode slots.
