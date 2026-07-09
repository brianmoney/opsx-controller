## Context

`opsx-plan` runs each change through direct implement, review, and archive stages. Existing telemetry records already include stable identity fields, normalized `usage`, model identity, and deterministic cost-estimation behavior. Usage currently comes from parsed worker JSON first and recognized log metadata second.

The OpenCode adapter needs a stronger usage source. A plugin is the appropriate boundary because it can observe model events inside the OpenCode runtime while `opsx-plan` provides the stage identity through environment variables.

## Goals / Non-Goals

**Goals:**

- Define a sidecar file contract that joins OpenCode usage events to exactly one `opsx-plan` stage invocation.
- Normalize plugin-emitted usage into the same token fields already stored in telemetry.
- Distinguish final usage records from incremental observations so the consumer can choose the most authoritative event.
- Keep sidecar handling best-effort and non-fatal.
- Reserve `usage.usage_source = "opencode_plugin"` for sidecar-derived telemetry.

**Non-Goals:**

- Do not implement the OpenCode plugin.
- Do not update `opsx-plan` to create, pass, read, or consume the sidecar.
- Do not change report, dashboard, pricing, or aggregation behavior.
- Do not infer usage from text length, aggregate stats, or OpenCode database internals.

## Decisions

### 1. Use one append-only JSONL sidecar per stage invocation

The orchestrator will provide `OPSX_USAGE_PATH` for a single stage invocation. The plugin appends one JSON object per observed event to that path.

**Rationale:** JSONL tolerates incremental writes and partially completed runs. A per-stage file avoids cross-stage correlation ambiguity.

### 2. Make stage identity explicit in every record

Every sidecar record includes `plan_name`, `run_id`, `change_id`, `stage`, and `round`, matching the environment variables passed to the plugin.

**Rationale:** This lets a consumer reject stale, copied, or cross-stage records even if the wrong path is read.

### 3. Prefer final records over incremental records

The sidecar supports `event_type = "incremental"` for model-update observations and `event_type = "final"` for complete turn/session usage. Consumers use the latest valid final record when present, otherwise the latest valid incremental record only when the stage did not complete normally and no stronger source exists.

**Rationale:** Final records are less likely to double-count partial updates. Incremental records still preserve useful information when a timeout interrupts a stage.

### 4. Keep existing sources authoritative

Worker JSON and recognized log metadata remain higher precedence than the plugin sidecar. The sidecar is a fallback before unavailable usage.

**Rationale:** Existing structured outputs are direct worker results and should not be overridden by an auxiliary observer until the ecosystem proves plugin data is more authoritative.

### 5. Reject ambiguous data instead of repairing it

Malformed JSONL lines, negative or non-integer token counts, mismatched stage identity, unknown event types, and missing final records are handled conservatively.

**Rationale:** Telemetry should never fabricate usage or convert ambiguous data into apparently precise cost estimates.
