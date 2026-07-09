## Context

`define-opencode-plugin-usage-contract` established the per-stage sidecar schema, stage identity environment variables, final-vs-incremental selection rules, and source precedence. `add-opencode-usage-emitter-plugin` added and installed the OpenCode plugin that appends those records when `OPSX_USAGE_PATH` is set.

This change is the orchestrator consumer. It should keep OpenCode-specific handling at the adapter boundary while preserving the existing direct-stage telemetry shape and cost-estimation behavior.

## Goals / Non-Goals

**Goals:**

- Create a unique sidecar path for every direct OpenCode stage invocation.
- Pass the sidecar path and exact stage identity through `OPSX_*` environment variables to the OpenCode subprocess.
- Read the sidecar after the stage ends, including timeout and invalid-output outcomes.
- Validate sidecar records against schema version, identity, event type, timestamp, and conservative numeric rules.
- Prefer the latest valid final record; use the latest valid incremental record only for non-normal stage outcomes when no final record exists.
- Preserve source precedence: worker JSON first, recognized log metadata second, OpenCode plugin sidecar third, unavailable usage last.
- Populate `usage.usage_source = "opencode_plugin"` only when selected sidecar usage wins precedence.
- Run existing cost estimation after sidecar usage and model identity are available.
- Keep sidecar read failures non-fatal to plan execution and telemetry writing.

**Non-Goals:**

- Do not modify the OpenCode usage emitter plugin or adapter installer.
- Do not scrape `opencode stats`, OpenCode databases, freeform logs, or provider APIs.
- Do not backfill historical telemetry.
- Do not change dashboard/report aggregation semantics beyond existing telemetry records naturally containing better usage.
- Do not enable sidecar capture for non-OpenCode adapters unless they explicitly implement the same contract later.

## Decisions

### 1. Store sidecars under `.opsx-plan/usage/`

`opsx-plan` will create sidecar paths under `.opsx-plan/usage/` using stage identity and a collision-resistant suffix such as the telemetry UID or another per-invocation unique value.

**Rationale:** Sidecars are runtime artifacts distinct from durable telemetry JSONL and logs. Keeping them in a dedicated directory avoids mixing auxiliary plugin events with canonical telemetry records.

### 2. Pass environment only to direct OpenCode stage processes

The orchestrator will add `OPSX_USAGE_PATH`, `OPSX_PLAN_NAME`, `OPSX_RUN_ID`, `OPSX_CHANGE_ID`, `OPSX_STAGE`, and `OPSX_ROUND` only when invoking an OpenCode-backed direct stage.

**Rationale:** The plugin is OpenCode-specific. Other adapters should not receive unused environment variables until they have a compatible emitter and consumer story.

### 3. Read sidecars after process termination

The consumer will read the sidecar after the worker exits, times out, fails to spawn after environment setup, or returns invalid output. Missing and unreadable files are treated the same as no usable sidecar data.

**Rationale:** JSONL appends may continue until process termination. Reading after termination provides a stable snapshot while preserving timeout partials when OpenCode emitted incremental records before termination.

### 4. Keep final records authoritative

The consumer selects the latest valid `event_type = "final"` record by `emitted_at`. If no final record exists, it may select the latest valid `event_type = "incremental"` record only when the stage status is not normal completion.

**Rationale:** Incremental observations may be partial or cumulative in event-specific ways. They are useful for interrupted runs but should not replace missing final accounting for successful runs.

### 5. Merge usage and model identity by object precedence

Worker JSON and recognized log metadata remain higher precedence than the sidecar. The sidecar can fill usage and model identity only when the corresponding higher-precedence object has no usable values.

**Rationale:** This matches the existing contract while allowing plugin data to unlock cost estimation when previous sources are unavailable.

## Risks / Trade-offs

- [Risk] Sidecar files could accumulate over time. -> Mitigation: keep them under `.opsx-plan/usage/` so operators can clean auxiliary artifacts without deleting telemetry.
- [Risk] Incremental records could overcount on timeouts. -> Mitigation: only use incremental records for non-normal outcomes and never for successful completion without a final record.
- [Risk] Malformed or mismatched sidecars could pollute telemetry. -> Mitigation: validate identity, schema, event type, timestamps, and numeric fields independently per record.
- [Risk] Cost estimation might appear inconsistent if model identity is missing. -> Mitigation: reuse existing unresolved-cost behavior when sidecar usage lacks model identity or pricing.

## Migration Plan

No data migration is required. Existing telemetry remains readable. New direct OpenCode stage runs may create `.opsx-plan/usage/` sidecar files and may write telemetry with `usage.usage_source = "opencode_plugin"` when the sidecar wins precedence.

After implementation, run `python3 -m unittest tests/orchestrator/test_opsx_plan.py` and `openspec validate consume-opencode-usage-sidecar --strict`.
