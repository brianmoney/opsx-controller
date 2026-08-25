## Why

The plan orchestrator's create stage formats and tokenizes `create_invoke`,
then passes the resulting list directly to `subprocess.Popen`. Unlike the
implement, review, and archive stages, it does not expand environment
variables in those tokens. A documented command such as
`opencode run --model "$OPSX_CONTROLLER_MODEL" ...` therefore sends the
literal `$OPSX_CONTROLLER_MODEL` string to the model client, which fails as a
model lookup with `UnknownError: "Unexpected server error"`.

The direct-stage path already provides the required expansion and fail-closed
behavior through `_expand_invoke_token()`. The create path should use that
same behavior rather than maintaining a second expansion implementation.

## What Changes

- Expand `$VAR` and `${VAR}` references in `create_invoke` tokens after
  applying the existing `{change}`, `{plan_doc}`, and `{controller_model}`
  substitutions.
- Reuse `_expand_invoke_token()` and preserve its unset-variable fail-closed
  behavior, including the existing `env_error` message format.
- Ensure a create-stage environment error is handled as an explicit terminal
  configuration failure rather than being passed to the client or treated as a
  retryable change-verification failure.
- Preserve empty-token handling and `{controller_model}` placeholder behavior.
- Add focused unit coverage for both environment-variable syntaxes, unset
  variables, and the controller-model placeholder.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `plan-driven-claude-code-execution`: extend the existing stage-invoke
  environment expansion contract to include templated create-stage invokes.

## Impact

- `orchestrator/opsx-plan.py`: create-stage token expansion and environment
  error handling.
- `tests/orchestrator/test_opsx_plan.py`: create-stage expansion regression
  tests.
- `docs/opsx-plan-operator-workflow.md`: clarify that the documented
  environment expansion contract includes `create_invoke`.

Implement, review, archive, model activation, and model environment export
behavior are otherwise unchanged.
