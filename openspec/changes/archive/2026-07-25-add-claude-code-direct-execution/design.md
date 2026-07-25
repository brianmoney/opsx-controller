## Context

`opsx-plan` has two execution paths. The legacy path launches a nested
controller (`/opsx-drive`) once per change and reads the controller's own state
file. The direct path dispatches one bounded worker per subprocess for the
implement, review, and archive phases, owns round control and retry budgets
itself, writes per-stage logs under `.opsx-plan/logs/`, and emits telemetry that
feeds `report`, `dashboard`, and the `budget_usd` spend gate.

Only OpenCode can take the direct path. The gate is `is_direct_opencode()` in
`orchestrator/opsx-plan.py`, which ANDs an adapter-identity test
(`cfg["adapter"] == "opencode"`) with a configuration test (all three
`*_invoke` keys present). The `claude-code` entry in `ADAPTER_DEFAULTS` supplies
only `invoke` and `state_file`, so both halves of the gate fail.

Everything downstream of the gate is already adapter-neutral: plan-owned worker
state, the worker input block, stage dispatch, and the "final assistant message
is exactly one line of JSON" output contract. The Claude Code worker agents
(`adapters/claude-code/agents/opsx-{implementer,reviewer,archiver}.md`) already
declare that contract and are installed by `adapters/claude-code/install.sh`.
The Claude Code CLI accepts a positional prompt in print mode and supports
`--agent`, `--model`, `--permission-mode`, and `--output-format`, which covers
every capability the direct path needs from a client.

Two coupling points remain genuinely OpenCode-specific and are the substance of
this design: how a per-stage model reaches the worker, and where token usage
comes from.

## Goals / Non-Goals

**Goals:**

- Make direct dispatch available to the `claude-code` adapter with the same
  round control, gates, state, and evidence-driven completion OpenCode gets.
- Keep OpenCode runs bit-for-bit unchanged in behavior, state, and telemetry.
- Make `OPSX_*_MODEL` actually select the per-stage model under `claude-code`,
  rather than being required by `doctor` and silently ignored.
- Produce real token usage for Claude Code stages so `budget_usd`, `report`,
  and `dashboard` work rather than reporting every stage as unresolved.

**Non-Goals:**

- Extending direct dispatch to `codex-cli`. The gate generalization makes it
  reachable, but its invoke defaults, agent contract, and usage source are out
  of scope here.
- Changing the plan manifest schema, plan state schema, or telemetry record
  schema version.
- Retiring the legacy `/opsx-drive` path for any adapter.
- Adding a plan-load warning for unrecognized manifest keys. That is a real
  defect in `load_plan()`, but it is orthogonal drift and belongs in its own
  change.

## Decisions

### 1. Gate on configuration, not adapter identity

Replace `is_direct_opencode(cfg)` with `is_direct_mode(cfg)`, which returns
true when `implement_invoke`, `review_invoke`, and `archive_invoke` are all
non-empty. The adapter-identity term is dropped entirely.

This is behavior-preserving for OpenCode, whose defaults already populate all
three keys, and it means an operator can opt any adapter into direct dispatch
by supplying invokes in `[plan]` — the same escape hatch that already exists
for overriding a single stage command.

*Alternative considered:* an explicit `direct = true` manifest flag. Rejected —
it adds a second source of truth that can disagree with the invokes, and every
existing plan would need it.

### 2. Claude Code invoke defaults

```
implement_invoke = claude -p --agent opsx-implementer --model "$OPSX_IMPLEMENTER_MODEL" --permission-mode bypassPermissions --output-format json
review_invoke    = claude -p --agent opsx-reviewer   --model "$OPSX_REVIEWER_MODEL"   --permission-mode bypassPermissions --output-format json
archive_invoke   = claude -p --agent opsx-archiver   --model "$OPSX_ARCHIVER_MODEL"   --permission-mode bypassPermissions --output-format json
```

The worker input block is appended as the trailing positional argument by
`invoke_direct_stage`, which is exactly how Claude Code takes a prompt in print
mode — no change to the dispatch mechanism.

`bypassPermissions` is the parity choice, not a shortcut. Direct runs are
unattended: in print mode an interactive permission request cannot be answered,
so any other mode turns a routine `Bash` or `Write` call into a stalled or
permission-denied transcript. Tool scope is still bounded, by the agents' own
`tools:` frontmatter (Read, Edit, MultiEdit, Write, Glob, Grep, Bash) — the same
place OpenCode bounds it, via its `permission:` block. Operators who want a
tighter posture override the invoke in `[plan]`.

### 3. Expand environment variables in invoke strings

`invoke_direct_stage` currently does a bare `shlex.split(cfg[f"{stage}_invoke"])`,
so `$OPSX_IMPLEMENTER_MODEL` would reach the CLI as a literal. Apply
`os.path.expandvars` to each token after splitting.

This exists because the two clients resolve models differently. OpenCode agents
interpolate `model: "{env:OPSX_IMPLEMENTER_MODEL}"` in frontmatter; Claude Code
agent frontmatter has no environment interpolation, so the model has to arrive
as a flag. Expansion in the invoke string is the one mechanism that serves both
and keeps model selection visible in the plan manifest and in the `exec[stage]`
log line.

A token that expands to empty SHALL fail the stage with a clear message naming
the unset variable, rather than silently invoking the client with a dangling
`--model` value. `doctor` already requires these variables to be set, so this
is a fail-closed backstop for the drift case.

*Alternative considered:* baking the model into agent frontmatter at install
time. Rejected — it makes the model a property of the installation rather than
of the plan, so two plans on one machine cannot use different models, and
`doctor`'s environment-variable check would stay decorative.

### 4. Unwrap the Claude Code result envelope

With `--output-format json`, stdout is a single envelope object carrying the
final text in `result` plus `usage` and cost fields. `parse_stage_json` scans
the log in reverse for the last parseable single-line JSON object, so it would
find the envelope and hand the control loop an object with no worker fields —
a spurious `invalid_output` failure on every stage.

Make the parser envelope-aware: when the object it finds is a Claude Code result
envelope (a `type` of `result` with a string `result` field), re-run the same
line scan over the unwrapped `result` text to find the worker's one-line JSON,
and retain the envelope alongside it as a usage source. Selecting the *last*
`type: result` object also makes the parser correct under
`--output-format stream-json`, which emits JSONL and streams — useful for
`opsx-plan logs -f`, where the single-envelope form shows nothing until the
stage exits. `json` stays the default for its smaller, more predictable logs;
`stream-json` remains a supported operator override rather than a second code
path.

The existing failure-marker handling (permission rejection, provider failure)
applies to the unwrapped text as well as the raw log, so a worker that never
reached JSON output is still reported actionably.

### 5. Envelope usage is its own precedence source

Register the envelope as usage source `claude_result_json`, ranked after
`worker_json` and before `log_metadata` and the OpenCode sidecar.

An explicit source is deliberate. The generic `_scan_log_for_usage` would
already find the envelope's nested `usage` dict by accident, since it inspects
nested `usage` on any JSON line — but it takes the first value found per field
scanning forward, which under `stream-json` would capture an intermediate
assistant message's partial usage instead of the final totals. A named source
that reads the selected envelope is deterministic under both output formats and
makes `report` output honest about where a number came from.

`_TOKEN_FIELD_MAP` gains `cache_creation_input_tokens`; it already maps
`cache_read_input_tokens`.

### 6. Adapter-aware agent model resolution

`_invocation_model` resolves an agent's declared model by reading
`~/.config/opencode/agents/<name>.md` — a hardcoded OpenCode path used when the
invoke carries no `--model`. Key that directory off the adapter
(`~/.claude/agents/` for `claude-code`). With decision 3 the Claude invokes
always carry `--model`, which is parsed first, so this is a correctness fix for
overridden invokes rather than the primary path.

### 7. Doctor checks worker agents for direct plans

When the resolved plan uses direct dispatch, `doctor` verifies that the
adapter's three worker agents are installed. A missing agent currently surfaces
as a stage failure mid-run; this moves it to preflight, consistent with the
existing stale-install and adapter-client checks.

## Risks / Trade-offs

- **Claude Code CLI flag drift** → `ADAPTER_DEFAULTS` already carries a comment
  telling operators to verify invoke commands against their client version. The
  flags used here (`-p`, `--agent`, `--model`, `--permission-mode`,
  `--output-format`) are long-standing, and the doctor agent check catches the
  most likely breakage. Defaults are overridable per plan.

- **`bypassPermissions` widens blast radius relative to an interactive session**
  → Bounded by the agents' `tools:` frontmatter and by the same clean-tree and
  evidence-verification gates that already guard direct runs. This is parity
  with the OpenCode adapter's agent permission block, not a new posture.

- **Envelope unwrapping is a second parse layer that could mask worker output
  bugs** → The unwrap reuses the existing line scanner and failure markers
  rather than introducing separate matching, so a malformed worker payload
  fails the same way it does under OpenCode.

- **Generalizing the gate makes direct dispatch reachable for `codex-cli` by
  configuration, before that adapter has been validated for it** → Acceptable:
  it requires an operator to hand-write three invokes, and the failure mode is a
  loud stage failure, not silent misbehavior. Documented in the adapter
  reference.

- **Two adapters now write usage through different sources** → Mitigated by
  keeping one precedence chain with named sources, which `report` already
  surfaces, so a run's usage provenance stays inspectable.
