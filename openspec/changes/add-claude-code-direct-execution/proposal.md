## Why

`opsx-plan` can only run plans end to end under the OpenCode adapter. The
`claude-code` adapter is limited to the legacy nested-controller path
(`claude -p "/opsx-drive {change}"`), because direct implement-review-archive
dispatch is gated on adapter identity rather than on configuration. The
Claude Code worker agents, their one-line JSON output contract, and the
installer already exist, so the gap is orchestrator-side only: operators who
run Claude Code get none of the plan-owned round control, stage logs,
telemetry, or spend budgets that OpenCode operators get.

## What Changes

- Replace the adapter-identity gate for direct dispatch with a
  configuration-driven predicate: a plan runs direct when all three of
  `implement_invoke`, `review_invoke`, and `archive_invoke` are configured,
  regardless of adapter. OpenCode behavior is unchanged because its adapter
  defaults already supply all three.
- Add `implement_invoke`, `review_invoke`, and `archive_invoke` defaults for
  the `claude-code` adapter, dispatching the installed `opsx-implementer`,
  `opsx-reviewer`, and `opsx-archiver` agents in Claude Code print mode with
  the worker input block passed as the positional prompt.
- Expand environment variables in stage invoke strings before execution, so
  `OPSX_IMPLEMENTER_MODEL` and its siblings select the per-stage model on
  adapters whose agent frontmatter cannot interpolate environment variables.
  Today those variables are required by `doctor` but silently inert for
  `claude-code`.
- Resolve an agent's declared model from the adapter's own agent directory
  instead of the hardcoded OpenCode path, so telemetry records the model that
  actually ran.
- Extract token usage and model identity from the Claude Code result envelope
  (`--output-format json`), unwrapping the envelope to find the worker's
  one-line JSON result. This gives `claude-code` the usage data that OpenCode
  gets from its usage-emitter plugin sidecar, which in turn makes `budget_usd`,
  `report`, and `dashboard` functional for Claude Code runs.
- Extend `doctor` to check that the configured adapter's worker agents are
  installed when a plan is configured for direct dispatch.
- Update the operator workflow guide and adapter reference so direct execution
  is documented as an adapter-neutral capability rather than an OpenCode one.

No breaking changes. Existing OpenCode plans, manifests, and state files
continue to load and run identically.

## Capabilities

### New Capabilities

- `plan-driven-claude-code-execution`: Direct implement-review-archive worker
  dispatch under the `claude-code` adapter — invocation shape, agent and
  per-stage model resolution, permission posture for unattended runs, worker
  output contract, and result-envelope handling.

### Modified Capabilities

- `plan-driven-opencode-execution`: The direct-dispatch gate is respecified as
  configuration-driven rather than conditioned on the OpenCode adapter.
  Existing OpenCode requirements and scenarios are preserved.
- `plan-run-observability`: Usage and model extraction gains a Claude Code
  result-envelope source, with its position in the existing deterministic
  source precedence made explicit.
- `plan-operator-cli`: `doctor` additionally verifies worker-agent availability
  for the configured adapter when the resolved plan uses direct dispatch.

## Impact

- `orchestrator/opsx-plan.py`: `ADAPTER_DEFAULTS`, the `is_direct_opencode`
  predicate and its call sites, `invoke_direct_stage`, `parse_stage_json`,
  `extract_usage_and_model`, `_TOKEN_FIELD_MAP`, agent-model resolution, and
  the `doctor` check set.
- `adapters/claude-code/`: agent definitions and installer, if per-stage model
  selection requires frontmatter or install-time changes.
- `docs/opsx-plan-operator-workflow.md` and
  `skills/opsx-controller/references/adapters.md`: operator-facing
  documentation of direct execution and its adapter matrix.
- `tests/orchestrator/test_opsx_plan.py`: direct-mode tests currently assume
  the OpenCode adapter.
- No changes to plan manifest schema, plan state schema, or the telemetry
  record schema version.
