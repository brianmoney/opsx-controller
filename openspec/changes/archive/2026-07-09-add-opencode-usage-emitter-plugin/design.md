## Context

The archived `define-opencode-plugin-usage-contract` change defines the sidecar record shape, stage identity environment variables, final-vs-incremental semantics, conservative malformed-record handling, and source precedence for future consumers. This change implements only the OpenCode adapter emission half of that contract.

The plugin belongs in the OpenCode adapter because it is OpenCode-specific runtime integration. The orchestrator will not depend on OpenCode internals directly; it will later consume the sidecar file through the stable contract.

## Goals / Non-Goals

**Goals:**

- Add a minimal OpenCode plugin that appends usage records to `OPSX_USAGE_PATH` during direct stage runs.
- Keep the plugin inert when `OPSX_USAGE_PATH` is absent or empty.
- Require valid stage identity variables before writing records, so the plugin never emits sidecar lines that cannot be joined to a stage invocation.
- Normalize available OpenCode event usage fields into the contract's token/model/request/latency fields.
- Emit incremental records from token-bearing update events and final records from final turn/session events when final usage is available.
- Deploy and verify the plugin through the OpenCode adapter installer.
- Cover representative event shapes with tests or fixture validation.

**Non-Goals:**

- Do not change `opsx-plan.py` to create sidecar paths or pass `OPSX_*` variables.
- Do not read sidecars or populate telemetry from `opencode_plugin` usage.
- Do not infer usage from `opencode stats`, OpenCode databases, logs, or provider APIs.
- Do not add report/dashboard changes.
- Do not make usage capture mandatory for non-OpenCode adapters.

## Decisions

### 1. Plugin is gated by environment, not config options

The plugin checks `OPSX_USAGE_PATH` and the stage identity environment variables at runtime. When `OPSX_USAGE_PATH` is absent or empty, it returns hooks that do not write files or simply no write-capable behavior.

**Rationale:** The plugin can be safely installed globally for OpenCode while remaining disabled for normal interactive use. `opsx-plan` can later enable it per stage by setting environment variables.

### 2. Missing or invalid stage identity suppresses writes

When a path is present but `OPSX_PLAN_NAME`, `OPSX_RUN_ID`, `OPSX_CHANGE_ID`, `OPSX_STAGE`, or `OPSX_ROUND` is missing or invalid, the plugin does not append records.

**Rationale:** The contract requires records to contain exact stage identity. Emitting malformed or unjoinable records would create ambiguity for the future consumer.

### 3. Appends are best-effort and non-fatal

File write failures, unsupported event shapes, or missing usage fields are ignored by the plugin after avoiding any effect on OpenCode behavior.

**Rationale:** Usage capture is observability. It must not alter model execution, tool execution, permissions, or stage outcomes.

### 4. Numeric normalization is conservative

The plugin copies only non-negative integer token, request, and latency values. It does not round floats, parse prose, coerce negative values, or synthesize totals unless OpenCode provides a reliable total.

**Rationale:** The usage contract distinguishes unavailable from zero usage. Fabricated or coerced token counts would make downstream cost estimates misleading.

### 5. Event classification is explicit

Token-bearing progress/update events such as `message.updated` are emitted as `event_type = "incremental"`. Final turn/session events such as `session.idle`, when they expose usable final usage, are emitted as `event_type = "final"`.

**Rationale:** The future consumer can prefer final records and only use incremental records for abnormal termination cases, matching the archived contract.

### 6. Installer owns deployment and verification

The OpenCode adapter installer copies the plugin into the installed OpenCode configuration and `--verify` confirms that the deployed plugin file exists.

**Rationale:** This repository's runtime uses installed files under `~/.config/opencode/`, not the working tree. Installer verification is required for the change to affect real runs.

## Risks / Trade-offs

- [Risk] OpenCode event payload shapes may change. -> Mitigation: normalize defensively, ignore unsupported shapes, and cover representative fixtures so changes are visible.
- [Risk] Globally installed plugin could affect interactive OpenCode sessions. -> Mitigation: environment gating keeps it inert unless `OPSX_USAGE_PATH` is present.
- [Risk] Sidecar writes could fail due to filesystem permissions. -> Mitigation: failures are non-fatal and later telemetry consumption will preserve unavailable usage when no valid sidecar exists.
- [Risk] Final events may not always include complete usage. -> Mitigation: emit finals only when usable final data is observed; incremental events remain available for timeout/failure scenarios after the consumer change.

## Migration Plan

No data migration is required. Installing the OpenCode adapter deploys the plugin. Existing runs and telemetry remain unchanged until a later orchestrator change sets `OPSX_USAGE_PATH` for stage invocations and consumes sidecars.

After implementation, run `bash adapters/opencode/install.sh --global --verify` so the installed OpenCode adapter includes the new plugin.
