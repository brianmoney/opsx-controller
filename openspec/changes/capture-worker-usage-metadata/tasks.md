## 1. Usage Extraction

- [ ] 1.1 Add a helper that extracts recognized token fields from parsed worker JSON and returns nullable values for input, output, cached input, reasoning, and total tokens.
- [ ] 1.2 Add validation so only non-negative integer token counts are accepted; malformed fields are ignored rather than coerced.
- [ ] 1.3 Add fallback extraction for recognizable log metadata when worker JSON has no usable token counts.
- [ ] 1.4 Set `usage.usage_available` to `true` when at least one token field is non-null and set `usage.usage_source` to `"worker_json"` or `"log_metadata"` based on the winning source.

## 2. Model Metadata Extraction

- [ ] 2.1 Extract `model.provider`, `model.model_id`, and `model.model_alias` from structured worker metadata when reliable fields are present.
- [ ] 2.2 Extract model identity from recognized log metadata when structured worker metadata is unavailable.
- [ ] 2.3 Leave model fields as `null` when metadata is missing or ambiguous.

## 3. Telemetry Integration

- [ ] 3.1 Integrate usage/model extraction into direct stage telemetry record construction after worker output parsing and before JSONL write.
- [ ] 3.2 Preserve default-unavailable usage and null model fields for timeouts, spawn errors, invalid output, and unknown formats.
- [ ] 3.3 Ensure extraction failures are logged or ignored without changing the stage outcome or control loop behavior.

## 4. Unit Tests

- [ ] 4.1 Test structured worker JSON with full token usage populates all usage fields and `usage_source="worker_json"`.
- [ ] 4.2 Test structured worker JSON with partial token usage preserves `null` for missing categories and marks usage available.
- [ ] 4.3 Test reported zero token values remain `0` and mark usage available.
- [ ] 4.4 Test structured worker JSON with provider/model metadata populates the telemetry `model` object.
- [ ] 4.5 Test recognizable log metadata populates usage/model fields when worker JSON has no usage.
- [ ] 4.6 Test worker JSON usage takes precedence over conflicting log metadata.
- [ ] 4.7 Test unknown formats produce telemetry with usage unavailable and null model fields.
- [ ] 4.8 Test malformed, negative, floating-point, and non-integer token values do not fabricate usage.
- [ ] 4.9 Test timeout, spawn error, and invalid output records keep default-unavailable usage.

## 5. Verification

- [ ] 5.1 Run `python3 -m unittest tests/orchestrator/test_opsx_plan.py`.
- [ ] 5.2 Run `openspec validate capture-worker-usage-metadata --strict`.
- [ ] 5.3 Run `bash adapters/opencode/install.sh --global --verify` after implementation because this change will touch orchestrator runtime behavior.
