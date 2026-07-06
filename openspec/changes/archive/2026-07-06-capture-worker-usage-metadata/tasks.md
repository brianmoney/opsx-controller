## 1. Usage Extraction

- [x] 1.1 Add a helper that extracts recognized token fields from parsed worker JSON and returns nullable values for input, output, cached input, reasoning, and total tokens.
- [x] 1.2 Add validation so only non-negative integer token counts are accepted; malformed fields are ignored rather than coerced.
- [x] 1.3 Add fallback extraction for recognizable log metadata when worker JSON has no usable token counts.
- [x] 1.4 Set `usage.usage_available` to `true` when at least one token field is non-null and set `usage.usage_source` to `"worker_json"` or `"log_metadata"` based on the winning source.

## 2. Model Metadata Extraction

- [x] 2.1 Extract `model.provider`, `model.model_id`, and `model.model_alias` from structured worker metadata when reliable fields are present.
- [x] 2.2 Extract model identity from recognized log metadata when structured worker metadata is unavailable.
- [x] 2.3 Leave model fields as `null` when metadata is missing or ambiguous.

## 3. Telemetry Integration

- [x] 3.1 Integrate usage/model extraction into direct stage telemetry record construction after worker output parsing and before JSONL write.
- [x] 3.2 Preserve default-unavailable usage and null model fields for timeouts, spawn errors, invalid output, and unknown formats.
- [x] 3.3 Ensure extraction failures are logged or ignored without changing the stage outcome or control loop behavior.

## 4. Unit Tests

- [x] 4.1 Test structured worker JSON with full token usage populates all usage fields and `usage_source="worker_json"`.
- [x] 4.2 Test structured worker JSON with partial token usage preserves `null` for missing categories and marks usage available.
- [x] 4.3 Test reported zero token values remain `0` and mark usage available.
- [x] 4.4 Test structured worker JSON with provider/model metadata populates the telemetry `model` object.
- [x] 4.5 Test recognizable log metadata populates usage/model fields when worker JSON has no usage.
- [x] 4.6 Test worker JSON usage takes precedence over conflicting log metadata.
- [x] 4.7 Test unknown formats produce telemetry with usage unavailable and null model fields.
- [x] 4.8 Test malformed, negative, floating-point, and non-integer token values do not fabricate usage.
- [x] 4.9 Test timeout, spawn error, and invalid output records keep default-unavailable usage.

## 5. Verification

- [x] 5.1 Run `python3 -m unittest tests/orchestrator/test_opsx_plan.py`.
- [x] 5.2 Run `openspec validate capture-worker-usage-metadata --strict`.
- [x] 5.3 Run `bash adapters/opencode/install.sh --global --verify` after implementation because this change will touch orchestrator runtime behavior.
