# bound-compile-prompt Design

## Context

`lib/orchestrator/compiler.py` owns compile prompt construction
(`discover_template_pairs`, `resolve_sample_plan_pair`,
`build_compile_prompt`) and client invocation (`COMPILE_CLIENTS`,
`_build_compile_argv`, `run_compile_client`). Today the prompt appends every
active and archived `openspec/plans/` pair without a size limit, and the
`claude-code` registry entry interpolates `{prompt}` inline into argv. See
proposal.md — Why for the observed failures.

Constraints that shape the approach:

- The markdown plan-authoring contract (`core/plan-authoring.md`) must not
  change; all semantic rules the model needs live in the schema guidance and
  compile instructions, which are small and always included.
- The generated TOML must still pass the existing `load_plan()` validation
  path in `cmd_compile`; nothing about validation changes.
- Existing callers must need no new arguments.

## Goals / Non-Goals

**Goals:**

- Prompt size bounded by an explicit budget regardless of repository age.
- Compile reliability independent of the OS argument-list limit for every
  supported adapter.
- A deterministic, actionable pre-spawn error when a client cannot accept
  the prompt, instead of a hang or `Errno 7`.
- An explicit, documented compile timeout knob.

**Non-Goals:**

- Raising the default timeout to mask slow compiles (the default stays 600s).
- Retaining archived-plan examples behind an opt-in flag (excluded
  unconditionally, per operator decision).
- Changing TOML validation, plan markdown syntax, or dependency conventions.
- Redesigning prompt content beyond bounding which examples are included.

## Decisions

### Budget value: ~128,000 characters

`COMPILE_PROMPT_BUDGET_CHARS = 128_000` as a module-level constant. This is
a ~4x reduction from the observed 559k failure prompt while leaving ample
room for a large source plan plus the fixed sections. Alternatives: 64k
(too tight — a large source plan would leave no room even for the canonical
sample) and 256k (too permissive — still pushes cost/latency and invites
long model calls). A constant, not a config knob: there is no evidence
operators need to tune it, and a single value keeps behavior predictable
across repos.

### Inclusion is priority-ordered, never truncated

Fixed sections (source markdown, schema guidance, compile instructions) are
always included whole — truncating the source plan would corrupt compile
semantics, and truncating guidance would silently change output rules.
Optional examples are included all-or-nothing in priority order: canonical
sample pair first, then the single smallest fitting active repository
template pair. A partial example (half a plan file) is worse than none, so
examples are dropped atomically with a log note. If the source alone exceeds
the budget, it is still included with a logged warning: the source is the
input, not an example.

### One repository template pair, smallest fitting

The canonical sample pair already demonstrates the full markdown→TOML
mapping; a repository pair adds repo-local conventions, where one example
suffices. Choosing the smallest fitting pair maximizes the chance of
inclusion under the budget. Alternative considered: include as many pairs as
fit — rejected because marginal value per extra pair is low and it reopens
unbounded growth in repos with many active plans.

### Archived plans excluded unconditionally

Archived plans are historical records, not conventions to imitate, and they
are what actually blew up the prompt in Knowledge Forge. An opt-in flag was
considered and rejected: it adds CLI/test/doc surface for a use case
(compiling against archived style) that the canonical sample and one active
template already cover.

### Claude Code transport: stdin via `subprocess.run(input=...)`

The installed Claude CLI contract was verified on the target machine:
`echo "<prompt>" | claude -p --model <model>` reads the prompt from stdin
when piped. The `claude-code` registry entry drops `{prompt}` from its
`argv_template` and gains a `prompt_transport: "stdin"` marker; the
`opencode` entry is marked `"file"` (its existing workspace-local `--file`
attachment behavior is unchanged). Alternative considered: a temp file plus
shell redirection — rejected because it adds file lifecycle complexity that
stdin already solves, and the opencode path only needs a file because of
OpenCode's sandbox permission model, which does not apply to Claude.

### Pre-spawn argv guard

After argv construction, any single element larger than a conservative
threshold (~100 KB, far below `ARG_MAX`) fails with a `PlanError` naming the
adapter and stating the prompt is too large for argv delivery. This is a
safety net for future argv-transport regressions; with stdin/file
transports it should never fire. Failing before spawn converts an opaque OS
error into an actionable diagnostic.

### Timeout knob: `--timeout-minutes` CLI flag

Matches the manifest's `*_timeout_minutes` naming and the compile
subcommand's existing flag style (`--force`, `--adapter`). Default 10.0
minutes preserves current behavior; `run_compile_client` takes
`timeout_minutes` and its timeout diagnostic names the flag. An env var was
considered and rejected as a second, redundant mechanism.

## Risks / Trade-offs

- [A repository whose only active plan pairs are individually larger than
  the remaining budget gets no repo-local example] → Acceptable: the
  canonical sample pair plus schema guidance still fully specify the output
  contract; the omission is logged.
- [The stdin contract could differ across Claude CLI versions] → The
  contract was verified against the installed CLI; if a future version
  breaks it, the failure surfaces as a non-zero exit or empty output, both
  of which already produce named, actionable `PlanError`s — never an argv
  overflow.
- [Smaller prompts could marginally reduce compile fidelity in repos that
  previously benefited from many examples] → The semantic rules live in the
  always-included guidance and instructions, not the examples; examples are
  illustrative only.
- [Prompt budget constant may need tuning later] → Single named constant
  with a docstring; changing it is a one-line, test-covered edit.

## Migration Plan

Purely behavioral fix in the compiler; no data or config migration. After
merge, the maintainer re-runs one global installer
(`bash adapters/opencode/install.sh --global --verify`) so the installed
`lib/orchestrator/` runtime picks up the change — required, not optional,
per the repo's deploy-after-merge rule, since the installed runtime is what
`opsx-plan compile` actually executes.
