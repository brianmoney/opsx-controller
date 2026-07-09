## Why

`define-opencode-plugin-usage-contract` defines a per-stage OpenCode usage sidecar, but no OpenCode plugin currently emits that sidecar. Existing usage extraction relies on worker JSON and recognized log metadata, which are not reliable sources for complete OpenCode token usage across direct `opsx-plan` stage invocations.

Adding a small OpenCode adapter plugin provides the missing emission point. The plugin can observe token-bearing OpenCode events while a stage runs and append normalized JSONL usage records that the later `consume-opencode-usage-sidecar` change can read.

## What Changes

- Add an OpenCode usage emitter plugin under the OpenCode adapter.
- Make the plugin inert unless `OPSX_USAGE_PATH` and valid stage identity environment variables are present.
- Emit append-only JSONL sidecar records that conform to the archived OpenCode plugin usage contract.
- Classify token-bearing update events as incremental usage and final turn/session events as final usage.
- Normalize model identity, token counts, request count, latency, timestamps, and event type without fabricating unavailable values.
- Update the OpenCode adapter installer to deploy the plugin into the installed OpenCode configuration and verify its presence.
- Add fixture/unit validation for representative incremental, final, malformed, and disabled-plugin event cases.

No orchestrator sidecar creation, telemetry consumption, report/dashboard updates, database scraping, provider API calls, or historical telemetry backfill are included.

## Capabilities

### Modified Capabilities

- `plan-run-observability`: Adds the OpenCode adapter plugin that emits usage sidecar records matching the existing OpenCode plugin usage contract.

### New Capabilities

- None.

## Impact

- Affected specs: `openspec/specs/plan-run-observability/spec.md` (modified by future archive; this change adds plugin emission requirements).
- Affected code: OpenCode adapter plugin files and `adapters/opencode/install.sh` deployment/verification logic.
- Affected tests: fixture or unit tests for plugin event normalization and installer verification.
- Runtime behavior: OpenCode behavior is unchanged outside `opsx-plan` stage runs because the plugin is inert unless `OPSX_USAGE_PATH` is set.
- Follow-up change: `consume-opencode-usage-sidecar` will pass stage environment variables and consume the emitted sidecar.
