## 1. Telemetry Record Builder

- [x] 1.1 Implement `build_telemetry_record()` that constructs a JSONL-ready dict from stage identity, invocation context, outcome, and result data, with all required fields populated and `usage`/`cost` set to default unavailable values.
- [x] 1.2 Generate a UUID `uid` for each record to enable state linking.

## 2. Telemetry Writer

- [x] 2.1 Implement `write_telemetry_record()` that serializes a record dict and atomically appends it to `.opsx-plan/telemetry/<plan_name>.jsonl`, creating the directory and file if they don't exist.
- [x] 2.2 Ensure the `.opsx-plan/.gitignore` already covers the `telemetry/` subdirectory (verify existing `*` pattern).

## 3. Orchestrator Integration (Direct Mode)

- [x] 3.1 Capture `started_at` timestamp before `invoke_direct_stage()` is called in `run_direct_change()`.
- [x] 3.2 After `invoke_direct_stage()` returns (or raises), capture `ended_at`, compute `duration_ms`, and determine the outcome status (`"completed"`, `"timeout"`, `"spawn_error"`, `"failed"`, or `"invalid_output"`).
- [x] 3.3 Call `build_telemetry_record()` with the collected data and call `write_telemetry_record()` to persist it.
- [x] 3.4 Populate the `result` object: set `log_path` from the returned log path; set `stage_status` from the parsed worker JSON when available; set `error_message` for spawn_error/invalid_output outcomes; set `verdict` and finding counts for review stages.
- [x] 3.5 Populate the `invocation` object from the dispatch parameters (adapter, worker command, timeout, retry_attempt).
- [x] 3.6 Set `model` fields to `null` (model identity extraction is deferred to `capture-worker-usage-metadata`).

## 4. State Linking

- [x] 4.1 Add a `telemetry` key to the per-change worker state dict with a `latest_telemetry` field storing the record UID.
- [x] 4.2 Update `persist_direct_state()` or the worker-state persistence path to include the telemetry reference.
- [x] 4.3 Ensure existing state files without the `telemetry` key are handled gracefully (optional field, absent = no telemetry linked).

## 5. Run ID Stability

- [x] 5.1 Derive `run_id` from the plan state's `started_at` timestamp. If absent, generate a UUID and persist it in the plan state for stability across resumptions.
- [x] 5.2 Pass `run_id` consistently to all telemetry records within the same plan run.

## 6. Unit Tests

- [x] 6.1 Test that a successful implement stage produces a telemetry record with `status="completed"`, non-null `ended_at`/`duration_ms`, and correct identifier fields.
- [x] 6.2 Test that a successful review stage produces a telemetry record with `result.verdict` and finding counts populated.
- [x] 6.3 Test that a successful archive stage produces a telemetry record with `status="completed"`.
- [x] 6.4 Test that a stage timeout produces a telemetry record with `status="timeout"` and `result.error_message` populated.
- [x] 6.5 Test that a spawn error (command not found) produces a telemetry record with `status="spawn_error"` and `result.error_message` populated.
- [x] 6.6 Test that a stage returning invalid (non-parseable) JSON produces a telemetry record with `status="invalid_output"`.
- [x] 6.7 Test that the telemetry record is appended to the correct plan-scoped JSONL file.
- [x] 6.8 Test that the per-change worker state includes the `telemetry.latest_telemetry` UID after a stage completes.
- [x] 6.9 Test that `usage` is set to default-unavailable (all `null`, `usage_available=false`) and `cost` is `"unavailable"`.
- [x] 6.10 Test that the `.opsx-plan/telemetry/` directory is created on first write.
- [x] 6.11 Test that `run_id` is stable across a pause-and-resume sequence.
- [x] 6.12 Test that the existing resume behavior is preserved (state, round tracking, and phase transitions are unchanged).

## 7. Verification

- [x] 7.1 Run all existing tests and ensure no regressions in `opsx-plan.py`.
- [x] 7.2 Run `openspec validate record-direct-stage-telemetry --strict` and ensure zero findings.
- [x] 7.3 Manually verify that a sample telemetry record matches the schema: required fields present, nullable fields use `null` not absent, enum values are valid.
