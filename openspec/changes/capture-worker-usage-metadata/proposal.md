## Why

Direct stage telemetry now writes durable records for every implement, review, and archive worker invocation, but `usage` remains unavailable and `model` identity remains `null`. That makes the telemetry useful for reliability and duration analysis, but not yet useful for comparing model efficiency by token use or connecting later pricing estimates to a concrete provider/model.

Worker outputs and logs may expose token counts and provider metadata, but the orchestrator must treat those formats as best-effort evidence rather than a hard dependency. Unknown or changing client formats must not fail plan execution or fabricate usage.

## What Changes

- Add a conservative usage extraction layer for direct stage telemetry in `orchestrator/opsx-plan.py`.
- Populate `usage.input_tokens`, `usage.output_tokens`, `usage.cached_input_tokens`, `usage.reasoning_tokens`, and `usage.total_tokens` when structured worker output or recognizable log metadata exposes non-negative integer values.
- Populate `usage.usage_available` and `usage.usage_source` based on the actual extraction source, while preserving `null` for unavailable token fields.
- Populate `model.provider`, `model.model_id`, and `model.model_alias` when the same extracted metadata exposes reliable model identity.
- Add tests covering known structured usage examples, recognizable log metadata, partial usage, zero usage, malformed usage, and unknown formats.

No provider API calls, token estimation from text, dollar-cost calculation, or dashboard/reporting work is included.

## Capabilities

### Modified Capabilities

- `plan-run-observability`: Adds runtime extraction requirements for token usage and model/provider metadata in direct stage telemetry records.

### New Capabilities

- None.

## Impact

- Affected specs: `openspec/specs/plan-run-observability/spec.md` (modified — adds usage/model metadata extraction behavior for direct stage telemetry).
- Affected code: `orchestrator/opsx-plan.py` usage/model extraction and telemetry record construction.
- Affected tests: `tests/orchestrator/test_opsx_plan.py` direct stage telemetry tests.
- Downstream changes (`estimate-stage-token-costs`, aggregation, reporting, dashboard export) can rely on `usage.usage_available`, nullable token fields, and populated model identity when extraction succeeds.
