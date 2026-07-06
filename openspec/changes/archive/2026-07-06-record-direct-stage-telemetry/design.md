## Context

`opsx-plan.py`'s `run_direct_change()` dispatches implement, review, and archive workers via `invoke_direct_stage()` → `run_logged_command()`. Each invocation produces a log file under `.opsx-plan/logs/` and updates the per-change worker state, but no structured telemetry records are persisted.

The `plan-run-observability` spec defines the JSONL record format and storage location (`.opsx-plan/telemetry/<plan_name>.jsonl`). This change implements the runtime writes that produce those records for every direct stage attempt.

## Goals / Non-Goals

**Goals:**

- Write exactly one JSONL telemetry record per direct stage invocation, after the invocation completes or fails.
- Populate all required identity fields (`schema_version`, `plan_name`, `run_id`, `change_id`, `stage`, `round`, `status`, `started_at`, `ended_at`, `duration_ms`), the `invocation` context, best-effort `model` identity, and a `result` summary (including `log_path`, `stage_status` when parsed, and `error_message` on failure).
- Populate default/unavailable values for `usage` (all token fields `null`, `usage_available` false) and `cost` (status `"unavailable"`) so records are valid immediately for downstream consumers.
- Link each completed telemetry record from the per-change worker state so aggregators can find the latest entry without scanning the full JSONL file.
- Handle all invocation outcomes: successful completion, timeout, spawn error (command not found), and invalid worker JSON output.
- Preserve existing resume behavior — pauses and resumes must not duplicate or orphan telemetry entries.

**Non-Goals:**

- Do not extract or populate token usage fields — that is `capture-worker-usage-metadata`'s responsibility.
- Do not attempt cost estimation — that is `estimate-stage-token-costs`'s responsibility.
- Do not modify the implement-review-archive control loop semantics.
- Do not write telemetry for legacy drive-mode dispatches — only direct mode is in scope.
- Do not change the worker state schema beyond adding a telemetry reference field.

## Decisions

### 1. Write one record per invocation, after the invocation completes

Each stage invocation produces exactly one JSONL record written after the subprocess exits. The record captures both start and end timestamps and the final outcome status. This avoids two-record-per-attempt complexity (started + completed) while still recording enough information for crash-recovery detection (a stage that never produces a record either never started or the orchestrator died before the subprocess exited).

The `status` field in the schema includes `"started"` for future use (e.g., writing a started record before invocation for crash detection), but this change only writes records with final statuses: `"completed"`, `"failed"`, `"timeout"`, `"spawn_error"`, or `"invalid_output"`.

**Rationale:** Matches the plan's "exactly one telemetry entry" success criteria. Keeps the implementation simple and avoids JSONL deduplication concerns.

**Alternatives considered:** Write a started record and a completed record (two lines per attempt, requires consumers to deduplicate by identity tuple; adds complexity without sufficient benefit at this phase).

### 2. Generate a per-invocation telemetry UID for state linking

Each telemetry record gets a `uid` field — a UUID generated at write time. The per-change worker state stores `latest_telemetry: <uid>` under a `telemetry` key so aggregators can quickly find the most recent entry for each change-stage combination without reading the entire JSONL file.

The `uid` is not required by the schema identity requirement (which uses the `(plan_name, run_id, change_id, stage, round)` tuple), but it provides a stable pointer from state to telemetry that survives log rotation or telemetry file reorganization.

**Rationale:** The identity tuple alone is sufficient for uniqueness, but a UID simplifies state-to-telemetry joining, especially when multiple rounds produce entries with the same change-stage identity.

**Alternatives considered:** Storing the JSONL byte offset (fragile across appends and file rotations), referencing telemetry by the identity tuple (requires multi-field lookup per consumer), omitting state linking entirely (forces every aggregator to scan all entries).

### 3. Derive run_id from plan state, stable across resumptions

`run_id` is read from the plan state record's `started_at` field (ISO-8601 UTC timestamp with seconds precision). If no `started_at` exists in state (first run), a UUID is generated and persisted. The `run_id` is the same for all telemetry records written during a plan run, including after a pause-and-resume.

**Rationale:** Timestamp-based `run_id` is human-readable and converges to the same value across distributed orchestrator instances. UUID fallback handles edge cases.

### 4. Best-effort model identity from adapter configuration

Model identity fields (`provider`, `model_id`, `model_alias`) are populated on a best-effort basis from the plan's adapter configuration. For `adapter: "opencode"`, the configuration may specify per-stage agent names (e.g., `opsx-implementer`). The actual model behind an agent is resolved at invocation time by the OpenCode runtime and is not available to the orchestrator without parsing agent config files — which is out of scope.

Therefore, `model.provider`, `model.model_id`, and `model.model_alias` are all `null` in records written by this change. This is explicitly valid per the schema ("All three fields SHALL be null when model identity cannot be extracted").

Downstream change `capture-worker-usage-metadata` may populate these from worker output or log metadata.

**Rationale:** Avoids coupling the orchestrator to agent configuration file formats that may change independently. The schema was designed with nullable model fields for this reason.

### 5. Record invocation context from the dispatch parameters

The `invocation` object is populated from the actual dispatch parameters:
- `adapter`: from `cfg["adapter"]` (e.g., `"opencode"`)
- `worker_command`: the stage-specific invoke command prefix (e.g., `"opencode run --agent opsx-implementer"`)
- `args_sample`: `null` (the input block is intentionally not captured to avoid storing large ephemeral prompts)
- `timeout_seconds`: from `cfg["changes"][cid]["timeout_minutes"] * 60`
- `retry_attempt`: `0` for the first attempt of a stage within a round (multi-attempt retry is a future feature)

### 6. Atomic JSONL appends with line-buffered writes

Telemetry records are appended to the JSONL file using an atomic write pattern: serialize the record, append a newline, and flush. The file is opened in append mode (`a`) with line buffering disabled to ensure each record is fully written before the next operation.

**Rationale:** Minimizes risk of partial writes corrupting the JSONL stream. JSONL is robust to partial last lines by design, but flushed appends provide stronger guarantees.

## Risks / Trade-offs

- [Risk] Telemetry file grows unbounded over many plan runs. → Mitigation: Same as the schema — operators control retention; JSONL compresses well with `gzip`. A future change can add rotation.
- [Risk] If the orchestrator crashes after the subprocess exits but before the telemetry record is flushed, a stage invocation exists in logs and state but has no telemetry entry. → Mitigation: This gap is indistinguishable from a pre-telemetry run. A post-hoc reconciliation tool could backfill from state and logs; that is out of scope for this change.
- [Risk] JSONL appends from concurrent orchestrator instances could interleave if two `opsx-plan` processes target the same plan simultaneously. → Mitigation: The plan's `require_clean_tracked` gate prevents concurrent runs against the same repo; this risk is already addressed by the existing orchestration model.

## Migration Plan

No migration is required. This change adds new write paths only. Existing `.opsx-plan/` state files and logs are not modified.

The new `.opsx-plan/telemetry/` directory is created on first telemetry write. The `.opsx-plan/.gitignore` already ignores all contents (`*`), so telemetry files are excluded from version control by default.

The per-change worker state schema gains an optional `telemetry` key with a `latest_telemetry` field. Existing state files without this key are handled gracefully (the field is treated as absent/NULL by readers).

## Open Questions

None. All deferred questions from the schema change (UUID vs tuple identity, pricing structure) are already resolved or deferred to downstream changes.
