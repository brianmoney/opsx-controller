## Context

`record-direct-stage-telemetry` added exactly one telemetry record for each direct implement, review, and archive stage invocation. Those records intentionally set `usage.usage_available` to `false`, all token fields to `null`, and all `model` fields to `null` because usage extraction was deferred.

This change fills that deferred layer. It consumes only metadata already available to the orchestrator after a worker invocation: parsed worker JSON and the stage log file. The extraction must be conservative because OpenCode, provider adapters, and worker implementations may change their output formats independently of `opsx-plan.py`.

## Goals / Non-Goals

**Goals:**

- Extract token usage from structured worker output when it contains recognized token fields.
- Extract token usage from recognizable metadata lines in stage logs when structured worker output does not contain usage.
- Preserve `null` for unavailable token fields and distinguish unavailable usage from reported zero usage.
- Record the extraction source in `usage.usage_source`.
- Populate model identity from the same trusted metadata when provider/model fields are present.
- Treat malformed, negative, non-integer, or unknown usage formats as unavailable instead of failing the run.
- Preserve the existing implement-review-archive control loop and telemetry write semantics.

**Non-Goals:**

- Do not call provider APIs or client CLIs to fetch usage after the invocation.
- Do not estimate tokens from raw prompt, output, log size, or text length.
- Do not calculate dollar cost.
- Do not add dashboard, report, or aggregation behavior.
- Do not make usage extraction required for telemetry record validity.

## Decisions

### 1. Prefer structured worker output over log metadata

Usage extraction first inspects the parsed worker result object. If recognized usage fields are found and validated, telemetry records `usage.usage_source` as `"worker_json"`.

If structured output has no usable token fields, extraction may scan the stage log for recognized metadata and record `usage.usage_source` as `"log_metadata"` when successful.

**Rationale:** Structured worker output is less ambiguous than text logs and is already parsed by the orchestrator. Logs remain useful for clients that expose provider metadata outside the final worker JSON.

### 2. Accept only non-negative integer token counts

Each token field is populated only when the extracted value is an integer greater than or equal to zero. Missing, negative, floating-point, string-with-units, or otherwise malformed values are ignored for that field.

If no token field survives validation, `usage.usage_available` remains `false`, `usage.usage_source` remains `null`, and all token fields remain `null`.

**Rationale:** Cost estimation and reporting depend on exact numeric counts. Fabricated or coerced values would make later comparisons misleading.

### 3. Support partial usage records

Extraction does not require all token categories to be present. A record with only input and output token counts is valid and sets `usage.usage_available` to `true`; unavailable categories remain `null`.

**Rationale:** Providers differ in whether they report cache hits, reasoning tokens, or total tokens. The schema already supports partial usage.

### 4. Preserve reported zero values

Reported zero token counts are retained as `0` and make usage available when at least one token field is known.

**Rationale:** `0` means confirmed zero, while `null` means unknown. Collapsing zero into null would break cost and efficiency calculations.

### 5. Populate model identity only from reliable fields

Model identity is populated when structured output or recognized log metadata exposes provider, model id, or model alias fields. The extractor does not infer model identity from worker names, agent names, or freeform prose.

**Rationale:** Later cost lookup keys depend on accurate provider/model identity. Guessing from stage configuration risks attaching usage to the wrong pricing entry.

## Recognized Metadata Shape

Implementation may support multiple client-specific shapes, but it must normalize them into the existing telemetry schema:

```json
{
  "usage": {
    "input_tokens": 100,
    "output_tokens": 20,
    "cached_input_tokens": 10,
    "reasoning_tokens": 5,
    "total_tokens": 135
  },
  "model": {
    "provider": "openai",
    "model_id": "gpt-5.5",
    "model_alias": "primary"
  }
}
```

Equivalent provider naming such as `prompt_tokens` / `completion_tokens` may be mapped to the schema fields when the mapping is unambiguous.

## Risks / Trade-offs

- [Risk] Client log formats change and extraction stops working. -> Mitigation: unknown formats are treated as unavailable usage, not as stage failures.
- [Risk] Overly broad text parsing may capture unrelated numbers. -> Mitigation: only recognized metadata structures or explicitly labeled fields are parsed.
- [Risk] Both worker JSON and logs contain conflicting values. -> Mitigation: structured worker output wins; logs are fallback evidence.

## Migration Plan

No migration is required. Existing telemetry records remain readable. New records may populate previously-null `usage` and `model` fields while keeping schema version `1` because the fields already exist in the schema.

## Open Questions

None.
