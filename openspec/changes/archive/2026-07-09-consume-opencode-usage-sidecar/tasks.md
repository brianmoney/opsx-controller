## 1. Sidecar Context

- [x] 1.1 Create a unique per-stage OpenCode usage sidecar path under `.opsx-plan/usage/` for direct OpenCode stage invocations.
- [x] 1.2 Ensure the sidecar directory is created before the OpenCode worker starts.
- [x] 1.3 Pass `OPSX_USAGE_PATH`, `OPSX_PLAN_NAME`, `OPSX_RUN_ID`, `OPSX_CHANGE_ID`, `OPSX_STAGE`, and `OPSX_ROUND` to the OpenCode subprocess environment.
- [x] 1.4 Avoid passing sidecar context to non-OpenCode adapters unless they explicitly support the same contract.

## 2. Sidecar Consumption

- [x] 2.1 Add a sidecar reader that handles missing, empty, unreadable, and malformed JSONL files without failing the stage.
- [x] 2.2 Validate sidecar records for supported schema version, matching stage identity, supported event type, usable timestamp, and conservative numeric fields.
- [x] 2.3 Select the latest valid final record when present.
- [x] 2.4 Select the latest valid incremental record only for non-normal stage outcomes when no valid final record exists.
- [x] 2.5 Treat normally completed stages with only incremental records as unavailable sidecar usage.
- [x] 2.6 Normalize selected sidecar usage and model identity into the existing telemetry `usage` and `model` objects.

## 3. Telemetry Precedence and Cost

- [x] 3.1 Preserve source precedence: worker JSON usage first, recognized log metadata second, OpenCode plugin sidecar third, unavailable usage last.
- [x] 3.2 Set `usage.usage_source = "opencode_plugin"` only when selected sidecar token usage wins precedence.
- [x] 3.3 Ensure sidecar model identity does not override higher-precedence model identity.
- [x] 3.4 Run existing cost estimation after sidecar-derived usage and model identity are merged into the telemetry record.
- [x] 3.5 Ensure sidecar extraction failures do not alter stage status, result parsing, retry behavior, or telemetry writing.

## 4. Tests

- [x] 4.1 Add an orchestrator test for a valid final sidecar populating usage, model identity, `usage_source`, and estimated cost when pricing is available.
- [x] 4.2 Add an orchestrator test proving a missing sidecar preserves unavailable usage and unresolved cost.
- [x] 4.3 Add an orchestrator test proving malformed JSONL, invalid numeric values, unsupported schema versions, unknown event types, and identity mismatches are ignored independently.
- [x] 4.4 Add an orchestrator test proving timeout can use the latest valid incremental record when no final record exists.
- [x] 4.5 Add an orchestrator test proving a normally completed stage with only incremental sidecar records does not use partial usage.
- [x] 4.6 Add an orchestrator test proving worker JSON and recognized log metadata keep precedence over sidecar records.

## 5. Verification

- [x] 5.1 Run `python3 -m unittest tests/orchestrator/test_opsx_plan.py`.
- [x] 5.2 Run `openspec validate consume-opencode-usage-sidecar --strict`.
- [x] 5.3 If implementation touches `adapters/opencode/`, run `bash adapters/opencode/install.sh --global --verify`.
