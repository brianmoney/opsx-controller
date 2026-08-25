## 1. Implement create-stage expansion

- [ ] 1.1 Update `run_stage()` to apply its existing `.format()` substitutions, then expand every `shlex.split()` token with `_expand_invoke_token()`.
- [ ] 1.2 Preserve direct-stage empty-token handling, including removal of a preceding dangling flag when a variable expands to an empty string.
- [ ] 1.3 Return `env_error` with the existing unset-variable diagnostic and write the create log before any client subprocess is spawned.
- [ ] 1.4 Handle create-stage `env_error` explicitly at the run loop so a deterministic environment configuration error fails the change without falling through to verification retries.

## 2. Add regression coverage

- [ ] 2.1 Add a test proving `$OPSX_CONTROLLER_MODEL` expands in `create_invoke` and appears resolved in the exec/log command.
- [ ] 2.2 Add a test proving `${OPSX_CONTROLLER_MODEL}` expands in `create_invoke`.
- [ ] 2.3 Add a test proving an unset create-stage variable returns `env_error`, names the variable, and does not spawn the client.
- [ ] 2.4 Add a test proving `{controller_model}` placeholder substitution still reaches the create command.
- [ ] 2.5 Preserve the existing direct-stage expansion tests and add any focused create-loop assertion needed for terminal `env_error` handling.

## 3. Document and validate

- [ ] 3.1 Update the stage-invoke expansion documentation to state that it applies to `create_invoke` and that create placeholders are formatted before environment expansion.
- [ ] 3.2 Run `ruff check orchestrator/opsx-plan.py tests/orchestrator/test_opsx_plan.py` when `ruff` is available in the validation environment.
- [ ] 3.3 Run `python3 -m unittest discover -t . -s tests`.
- [ ] 3.4 Run `node tests/opencode/test-opsx-usage-emitter.js`.
- [ ] 3.5 Run `openspec validate fix-create-invoke-env-expansion --strict`.
