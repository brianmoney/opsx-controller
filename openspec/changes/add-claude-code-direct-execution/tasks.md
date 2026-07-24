## 1. Generalize the direct-dispatch gate

- [ ] 1.1 Replace `is_direct_opencode(cfg)` in `orchestrator/opsx-plan.py` with `is_direct_mode(cfg)` that tests only for non-empty `implement_invoke`, `review_invoke`, and `archive_invoke`
- [ ] 1.2 Update all call sites of the old predicate (worker-state persistence, reconcile, run loop, status reporting) to the new name
- [ ] 1.3 Replace the `direct OpenCode execution` run-log wording with adapter-neutral wording that names the configured adapter
- [ ] 1.4 Add tests covering: OpenCode plan still takes the direct path with no manifest change; a plan with fewer than three invokes takes the nested-controller path

## 2. Claude Code adapter defaults

- [ ] 2.1 Add `implement_invoke`, `review_invoke`, and `archive_invoke` to the `claude-code` entry in `ADAPTER_DEFAULTS`, targeting the installed worker agents in print mode with a result envelope and an `OPSX_*_MODEL` reference per stage
- [ ] 2.2 Select the unattended permission posture in the defaults and confirm worker tool scope is bounded by the agent `tools:` frontmatter in `adapters/claude-code/agents/`
- [ ] 2.3 Add tests asserting a `claude-code` plan with no invoke overrides resolves all three stage commands and reports direct dispatch
- [ ] 2.4 Add a test asserting a single overridden stage invoke is honored while the other two fall back to defaults

## 3. Environment expansion in stage invokes

- [ ] 3.1 Expand environment variables per argument in `invoke_direct_stage` after `shlex.split`
- [ ] 3.2 Fail the stage with a message naming the unset variable when an argument expands to empty, before any subprocess is spawned
- [ ] 3.3 Confirm the `exec[stage]` log line shows the expanded command with the input block elided
- [ ] 3.4 Add tests for successful expansion, the unset-variable failure path, and an invoke containing no variable references

## 4. Result envelope parsing

- [ ] 4.1 Make `parse_stage_json` recognize a Claude Code result envelope and select the last envelope object in the log
- [ ] 4.2 Re-scan the unwrapped result text for the worker's single-line JSON object, reusing the existing line-scanning and backtick-stripping rules
- [ ] 4.3 Apply the existing permission-rejection and provider-failure markers to the unwrapped text as well as the raw log
- [ ] 4.4 Return the selected envelope alongside the worker payload so telemetry can consume it
- [ ] 4.5 Add tests for: worker JSON recovered from an envelope; envelope with no worker JSON reported as `invalid_output`; permission rejection inside an envelope reported actionably; unwrapping correct under a streamed multi-line log; plain unwrapped output still parses unchanged

## 5. Usage and model extraction

- [ ] 5.1 Add `cache_creation_input_tokens` to `_TOKEN_FIELD_MAP`
- [ ] 5.2 Add envelope usage and model extraction with source name `claude_result_json`, reading only the selected envelope
- [ ] 5.3 Insert the envelope source into `extract_usage_and_model` between worker JSON and log metadata, leaving the OpenCode sidecar last
- [ ] 5.4 Add tests for each precedence pair in the chain and for a stage where the envelope is the only usage source
- [ ] 5.5 Verify a Claude Code stage produces a telemetry record with resolved cost, and that `report` and `dashboard` render it without schema changes

## 6. Adapter-aware agent model resolution

- [ ] 6.1 Replace the hardcoded `~/.config/opencode/agents/` lookup in the agent-model resolver with an adapter-keyed agent directory
- [ ] 6.2 Add tests covering model resolution from a `claude-code` agent file and unchanged resolution for OpenCode

## 7. Doctor preflight

- [ ] 7.1 Add a `doctor` check that the configured adapter's three worker agents exist when the resolved plan uses direct dispatch
- [ ] 7.2 Report each missing agent by name with the installer that provides it, and exit non-zero
- [ ] 7.3 Skip the check when no plan is resolved or the plan does not use direct dispatch
- [ ] 7.4 Add tests for the missing-agent failure, the all-present pass, and the skipped cases

## 8. Documentation

- [ ] 8.1 Update `docs/opsx-plan-operator-workflow.md` to describe direct execution as adapter-neutral, including the `claude-code` invoke defaults and the environment-expansion rule
- [ ] 8.2 Update `skills/opsx-controller/references/adapters.md` with the adapter capability matrix, noting that `codex-cli` direct dispatch is reachable by configuration but unvalidated
- [ ] 8.3 Document the `claude_result_json` usage source alongside the existing sources in the observability documentation

## 9. End-to-end verification

- [ ] 9.1 Run the orchestrator test suite and confirm no OpenCode-path regressions
- [ ] 9.2 Run `opsx-plan doctor` against a `claude-code` plan and confirm all checks pass
- [ ] 9.3 Run a single-change `claude-code` plan through implement, review, and archive, and confirm plan state, stage logs, telemetry with resolved cost, and archive evidence
