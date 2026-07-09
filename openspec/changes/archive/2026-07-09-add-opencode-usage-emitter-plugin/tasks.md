## 1. Plugin Implementation

- [x] 1.1 Add an OpenCode usage emitter plugin under the OpenCode adapter.
- [x] 1.2 Gate all sidecar writes on `OPSX_USAGE_PATH` being present and non-empty.
- [x] 1.3 Validate `OPSX_PLAN_NAME`, `OPSX_RUN_ID`, `OPSX_CHANGE_ID`, `OPSX_STAGE`, and `OPSX_ROUND` before writing records.
- [x] 1.4 Listen for token-bearing update events and emit `event_type = "incremental"` records when usage is available.
- [x] 1.5 Listen for final turn/session events and emit `event_type = "final"` records when final usage is available.
- [x] 1.6 Normalize provider, model id, model alias, input/output/cached/reasoning/total tokens, request count, latency, and emission timestamp into the sidecar contract.
- [x] 1.7 Treat missing, malformed, negative, floating-point, non-numeric, or ambiguous numeric fields as unavailable rather than coercing them.
- [x] 1.8 Make file append failures and unsupported event shapes non-fatal to OpenCode behavior.

## 2. Installer Deployment

- [x] 2.1 Update `adapters/opencode/install.sh` to deploy the plugin into the installed OpenCode configuration.
- [x] 2.2 Update installer `--verify` checks to confirm the deployed plugin is present.
- [x] 2.3 Ensure installation remains idempotent and does not require users to edit `opencode.json` manually if plugin auto-discovery is used.

## 3. Tests and Fixtures

- [x] 3.1 Add tests or fixture validation for an incremental token-bearing event.
- [x] 3.2 Add tests or fixture validation for a final usage event.
- [x] 3.3 Add tests or fixture validation proving the plugin is inert when `OPSX_USAGE_PATH` is absent.
- [x] 3.4 Add tests or fixture validation proving malformed numeric values are not coerced.
- [x] 3.5 Add tests or fixture validation proving invalid stage identity suppresses sidecar writes.

## 4. Verification

- [x] 4.1 Run the relevant plugin tests or fixture validation command.
- [x] 4.2 Run `openspec validate add-opencode-usage-emitter-plugin --strict`.
- [x] 4.3 Run `bash adapters/opencode/install.sh --global --verify` after implementation because the OpenCode adapter changes.
