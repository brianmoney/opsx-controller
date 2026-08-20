# bound-compile-prompt Proposal

## Why

`opsx-plan compile` builds its prompt by appending every active and archived
plan pair under `openspec/plans/`, so in a mature repository the prompt grows
without bound (observed: ~559,000 chars in Knowledge Forge). The OpenCode path
then stalls until the hardcoded 600-second client timeout, and the Claude Code
path places the full prompt inline in argv, failing at spawn with
`[Errno 7] Argument list too long` before the model is ever invoked.

## What Changes

- Bound the compile prompt with an explicit character budget
  (~128,000 chars): always include the source markdown, schema guidance, and
  compile instructions; include the canonical sample pair and at most one
  small active repository template pair only while they fit the budget;
  omit lower-priority examples (with a log note) when they would not fit.
- Exclude `openspec/plans/archived/` plan pairs from template context
  unconditionally.
- Change Claude Code compilation to deliver the prompt via stdin
  (`claude -p --model <model>` reading the prompt from standard input)
  instead of an inline argv argument; the installed Claude CLI contract was
  verified to support this.
- Add a pre-spawn argv-size guard: any compile client invocation whose argv
  would carry an oversized inline prompt fails with a clear, actionable
  `PlanError` before spawn instead of surfacing `Errno 7`.
- Add a `--timeout-minutes` option to `opsx-plan compile` (default 10,
  matching today's 600s) so client timeout behavior is explicit and
  configurable; timeout errors name the flag. The fix does not rely on
  raising the timeout.
- Update `orchestrator/README.md` to document the bounded prompt inclusion
  rules, the Claude stdin transport, and the new flag.

## Capabilities

### New Capabilities

- `bounded-compile-prompt-context`: prompt budget, fixed-priority inclusion
  order (source, schema guidance, instructions always; canonical sample pair
  and one small active repository template pair when they fit), unconditional
  exclusion of archived plan pairs, and pre-spawn argv-size guarding.

### Modified Capabilities

- `adapter-aware-plan-compilation`: Claude Code compilation delivers the
  prompt via stdin rather than inline argv; the compile client timeout
  becomes configurable via `--timeout-minutes` with the 600-second default
  retained, instead of a fixed hardcoded timeout.

## Impact

- `lib/orchestrator/compiler.py`: `discover_template_pairs`,
  `build_compile_prompt`, `COMPILE_CLIENTS` registry, `_build_compile_argv`,
  `run_compile_client`; new budget constant and template-selection helper.
- `orchestrator/opsx-plan.py`: `compile` subparser gains `--timeout-minutes`;
  `cmd_compile` passes it through.
- `tests/orchestrator/test_compiler.py`: new tests for budget bounding,
  archived exclusion, inclusion priority, Claude stdin transport, pre-spawn
  guard, and end-to-end compile validation in a large-archive synthetic repo;
  two existing Claude argv tests updated to the stdin contract.
- `orchestrator/README.md`: compile-stage documentation updates.
- No changes to markdown plan syntax, dependency conventions,
  `core/plan-authoring.md`, or `load_plan()` validation. Existing callers
  need no new arguments; small repositories compile as before.
