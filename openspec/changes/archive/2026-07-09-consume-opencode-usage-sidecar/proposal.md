## Why

The OpenCode adapter now has a usage emitter plugin and the sidecar contract is defined, but `opsx-plan` still does not create a per-stage usage sidecar, pass the `OPSX_*` context to OpenCode, or read plugin-emitted usage into telemetry. As a result, direct OpenCode stage runs continue to fall back to worker JSON, recognized log metadata, or unavailable usage even when the plugin can observe accurate token events.

Consuming the sidecar completes the planned OpenCode plugin usage flow and gives stage telemetry a reliable fallback usage source before cost estimation runs.

## What Changes

- Extend `orchestrator/opsx-plan.py` to create a unique usage sidecar path for each direct OpenCode stage invocation.
- Pass `OPSX_USAGE_PATH`, `OPSX_PLAN_NAME`, `OPSX_RUN_ID`, `OPSX_CHANGE_ID`, `OPSX_STAGE`, and `OPSX_ROUND` to the OpenCode subprocess environment for that stage.
- Read the sidecar after stage completion or failure and select the best valid plugin usage record according to the archived sidecar contract.
- Populate telemetry usage and model identity from the sidecar only when worker JSON and recognized log metadata provide no higher-precedence values.
- Set `usage.usage_source = "opencode_plugin"` when sidecar usage wins precedence.
- Run existing cost estimation after sidecar usage and model identity are merged into the telemetry record.
- Add orchestrator tests for valid sidecar usage, missing sidecar, malformed sidecar, timeout with incremental usage, source precedence, and cost estimation from sidecar usage.

No OpenCode plugin changes, installer changes, historical telemetry backfill, provider API calls, hosted telemetry, report/dashboard changes, or model-selection policy changes are included.

## Capabilities

### Modified Capabilities

- `plan-run-observability`: Enables `opsx-plan` to consume OpenCode plugin usage sidecars as the third deterministic usage source and feed sidecar-derived usage into existing telemetry and cost estimation.

### New Capabilities

- None.

## Impact

- Affected specs: `openspec/specs/plan-run-observability/spec.md`.
- Affected code: `orchestrator/opsx-plan.py`.
- Affected tests: `tests/orchestrator/test_opsx_plan.py`.
- Runtime behavior: Direct OpenCode stage invocations receive per-stage `OPSX_*` usage context and may populate telemetry from plugin sidecars when stronger sources are unavailable.
- Deployment: After implementation, run `bash adapters/opencode/install.sh --global --verify` if any OpenCode adapter files are touched; run the orchestrator validation suite for orchestrator changes.
