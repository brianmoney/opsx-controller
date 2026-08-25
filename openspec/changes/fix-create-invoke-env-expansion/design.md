# fix-create-invoke-env-expansion Design

## Context

`run_stage()` in `orchestrator/opsx-plan.py` handles the templated create
command by calling `.format(...)`, passing the result to `shlex.split()`, and
spawning the resulting argv. `subprocess.Popen(..., shell=False)` does not
perform shell environment expansion, so `$OPSX_CONTROLLER_MODEL` remains a
literal argument.

`invoke_direct_stage()` already expands each token with
`_expand_invoke_token()`. That helper uses `os.path.expandvars()` and detects
unresolved references, returning the variable name so callers can write the
existing `stage invoke references unset environment variable '<name>'`
diagnostic and return `env_error` before spawning a client.

## Goals / Non-Goals

**Goals:**

- Make create-stage `$VAR` and `${VAR}` references resolve exactly like direct
  stage invokes.
- Keep template substitution before token expansion so all existing create
  placeholders continue to work.
- Fail closed on an unset variable with a clear diagnostic and no client
  subprocess.
- Add regression tests that inspect both the logged command and spawned argv
  without making a live model call.

**Non-Goals:**

- Changing `_expand_invoke_token()` or the behavior of
  `invoke_direct_stage()`.
- Changing model resolution, `apply_model_env`, adapter defaults, or model
  identifier validation.
- Introducing shell execution for create commands.
- Changing the meaning of `{change}`, `{plan_doc}`, or `{controller_model}`.

## Decisions

### 1. Expand after formatting and tokenization

`run_stage()` will retain its existing `.format(change=..., plan_doc=...,
controller_model=...)` operation and `shlex.split()` parsing. It will then
pass every split token through `_expand_invoke_token()`. This preserves quoted
argument boundaries while resolving environment references and ensures the
literal `{controller_model}` placeholder remains a separate, supported
substitution mechanism.

### 2. Reuse the direct-stage expansion semantics

The implementation will call `_expand_invoke_token()` rather than duplicating
its `os.path.expandvars()` and unresolved-variable detection. Empty expanded
tokens and a preceding dangling flag will be handled the same way as
`invoke_direct_stage()`. Any refactoring to share token-list assembly must
preserve the current direct-stage behavior.

### 3. Treat create environment errors as terminal

When create token expansion returns an unset variable, `run_stage()` will
write the existing diagnostic format to the create log and return `env_error`
without calling `run_logged_command()`. The create-stage caller will handle
that outcome explicitly, mark the change failed with the diagnostic, and
avoid retrying a deterministic configuration error as if change verification
had failed.

### 4. Verify at the argv and log boundaries

Tests will use the existing temporary-repository and subprocess style from
`InvokeDirectStageEnvExpansionTests`. Successful tests will assert that the
resolved model appears in the create log and, where needed, in captured argv.
The unset-variable test will assert the named variable and `env_error`, with a
mock or argv sentinel proving no client was started.

## Risks / Trade-offs

- A create command that intentionally relied on a literal `$VAR` token will
  now fail closed if that variable is unset. This is intentional and matches
  the established direct-stage contract.
- The `{controller_model}` placeholder continues to use the value injected by
  the existing `.format()` call, while `$OPSX_CONTROLLER_MODEL` continues to
  use the process environment. Keeping both paths avoids an unrelated manifest
  compatibility change.

## Migration Plan

No migration is required. Existing manifests using `{change}`, `{plan_doc}`,
or `{controller_model}` continue to format as before. Manifests using
`$OPSX_CONTROLLER_MODEL` begin receiving the resolved value instead of the
literal token; manifests referencing an unset variable fail immediately with
an actionable error.
