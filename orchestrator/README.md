# opsx-plan: plan-level orchestrator

> **Operator workflow guide**: For the full operator workflow — activation,
> `doctor`, budgets, gate controls, logs, notifications, and branch/PR delivery
> — see [`docs/opsx-plan-operator-workflow.md`](../docs/opsx-plan-operator-workflow.md).
> This README is the technical reference for the orchestrator's design,
> manifest schema, and execution model.

`orchestrator/opsx-plan.py` iterates a TOML plan manifest of OpenSpec changes
(a dependency DAG) and, for each ready change, runs a staged lifecycle:

1. **create** — if `openspec/changes/<id>` does not exist, invoke your change
   authoring command (e.g. `/opsx-ff ... create a change for {change}`) and
   verify the result independently
2. **implement / review / archive** — for OpenCode-backed runs, dispatch the
   fixed `opsx-implementer`, `opsx-reviewer`, and `opsx-archiver` workers as
   separate one-shot subprocesses, with `opsx-plan` owning phase state,
   retries, recovery, and verification

The orchestrator is deliberately a deterministic script, not an agent. All
LLM judgment stays inside `/opsx-ff` and the configured implement/review/archive
workers. This layer only does ordering, dispatch, verification, retry policy,
and durable bookkeeping.

## Requirements

- Python 3.11+ (stdlib only; uses `tomllib`)
- A host project that already uses OpenSpec, with an opsx-controller adapter
  installed and verified to work for single changes
- Each change either already exists as an accepted OpenSpec change, or a
  `create_invoke` command is configured so the orchestrator can author it
- A headless invocation that works for your client (see Adapter invocation)

## The compile stage

`compile` converts a markdown implementation plan into a runnable TOML
manifest by invoking your selected compile adapter client with its
configured controller model. OpenCode is the default; use `--adapter
claude-code` to compile through Claude Code instead.

The compiler builds a self-contained prompt that includes the source
markdown, adapter-aware TOML schema (derived from the plan loader),
dependency-resolution rules, adapter defaults, and compile instructions.
The prompt is bounded by an explicit budget (~128,000 characters):
optional examples are included only while they fit the remaining budget,
in fixed priority order — the canonical sample plan pair first, then at
most one repository template pair (the smallest active `openspec/plans/`
pair that fits). Pairs under `openspec/plans/archived/` are excluded
unconditionally, and any example omitted for budget reasons is logged
with a note, so the prompt never grows without bound no matter how large
the plan archive becomes.

The generated TOML is validated locally before writing: it must parse as
valid TOML, pass the existing `load_plan()` path (unique ids, known deps,
no cycles), and its `adapter` field must match the selected compile
adapter. Output is written through a temporary file with atomic
replacement so invalid output never replaces an existing manifest.

Usage:

```bash
# Compile through OpenCode (the default)
opsx-plan compile docs/my-plan.md

# Compile to an explicit path
opsx-plan compile docs/my-plan.md -o plan.toml

# Compile through Claude Code
opsx-plan compile --adapter claude-code docs/my-plan.md

# Overwrite an existing manifest
opsx-plan compile docs/my-plan.md -o plan.toml --force

# Raise the compile client timeout to 20 minutes
opsx-plan compile --timeout-minutes 20 docs/my-plan.md
```

The compile command refuses to overwrite an existing output file unless
`--force` is passed. It fails before client invocation if the controller
model is unconfigured for the selected adapter.

The compile prompt is delivered per adapter: OpenCode receives it as a
workspace-local `--file` attachment, while Claude Code reads it from
standard input (`claude -p --model <model>`) so prompt size is never
limited by the operating-system argument-list limit. A pre-spawn guard
rejects any invocation whose argv would carry an oversized inline prompt
with a clear error naming the adapter, instead of surfacing an opaque OS
error. Compile client invocations time out after 10 minutes by default;
`--timeout-minutes <minutes>` overrides the limit, and timeout
diagnostics name the flag.

OpenCode compilation runs `opencode run --model <model>` and appends
`--variant <variant>` when the `controller` role resolves a reasoning
variant (`controller_variant` key or `OPSX_CONTROLLER_VARIANT`); with no
variant resolved the flag is omitted so the client's built-in default
applies. Claude Code compilation ignores the controller variant — the
Claude CLI has no reasoning-variant flag, so it is never passed through.

Two things the compiler cannot do, by design: detect a dependency the doc
forgot to state, and place judgment gates such as phase exit reviews — add
those `pause_before = true` entries yourself. Always review the DAG
(`run --dry-run`) before an unattended run.

If you author plan docs with a frontier model, telling it to follow this
convention (backticked slugs in `Depends on:`, explicit `(proposed` capability
markers) makes its output directly compilable.

## Plan placement and archival

Active plan manifests live under `openspec/plans/`. Archived (completed or
superseded) plans move into `openspec/plans/archived/`. The `opsx-plan
archive-plan` command moves a manifest pair:

```bash
# Archive a completed plan
opsx-plan archive-plan openspec/plans/my-plan.toml
```

`archive-plan` moves the `.toml` and its sibling `.md` (when present) into
`openspec/plans/archived/`, using `git mv` for tracked files. When the
active-plan pointer references the archived plan, it is cleared. The command
reports the moved paths and reminds you that the move still needs committing —
it never creates a commit.

## The create stage

Creation is just-in-time: a change is authored only when its dependencies are
done, because later changes write spec deltas against spec state produced by
earlier ones. `{change}` and `{plan_doc}` are substituted into
`create_invoke`; per-change `create_invoke` overrides are supported for
changes that need a custom prompt.

A change counts as created only when independent evidence agrees:
`openspec/changes/<id>` contains `proposal.md` and `tasks.md`, the
`created_check` command (default `openspec validate <id> --strict`) exits 0,
and creation modified no tracked files. Creation gets its own attempt budget
(`create_max_attempts`).

With `review_created = true` (the default), orchestrator-created changes stop
at `awaiting_acceptance` so you can review the proposal and spec deltas, then
continue with:

```bash
python3 .../opsx-plan.py accept plan.toml <change-id>
```

Changes you created by hand are presumed reviewed and skip this gate. Use
`run --create-only` to batch create+verify the currently actionable frontier
without dispatching implementation stages. Set `review_created = false` for
fully unattended create→implement (recommended only after `/opsx-ff` output has earned that trust
on a few supervised runs).

## Usage

### Single-change execution

For a single already-authored OpenSpec change, use `opsx-run` to skip the plan
manifest:

```bash
# Direct entrypoint (requires the orchestrator installed at ~/.local/bin)
opsx-run add-gardening-suggestions

# Equivalent subcommand under opsx-plan
python3 /path/to/opsx-controller/orchestrator/opsx-plan.py run-one add-gardening-suggestions
```

`opsx-run <change-id>` synthesizes a one-change OpenCode configuration with the
same defaults as plan-level execution (`max_rounds=5`, `no_progress_limit=2`,
`require_clean_tracked=true`) and runs the direct implement-review-archive loop.
The change must already exist at `openspec/changes/<change-id>/` with
`proposal.md` and `tasks.md` authored — `opsx-run` does not create changes.

Durable state is persisted to `.opsx-plan/run-<change-id>.state.json`, and stage
logs go to `.opsx-plan/logs/`. Interrupted runs can be resumed by re-invoking
the same `opsx-run <change-id>` command. Each run also writes a derived
manifest at `.opsx-plan/plans/run-<change-id>.toml`, so `opsx-plan report` and
`opsx-plan dashboard` can target the run by id with `--for-change <id>`.

### Plan-level execution (activate-then-run)

The recommended workflow activates a plan once, then all subsequent commands
resolve it automatically through the active-plan pointer (or `OPSX_PLAN` env
var). See the [operator workflow guide](../docs/opsx-plan-operator-workflow.md)
for full details on activation, budgets, gates, logs, notifications, and
branch/PR delivery.

From the host project root:

```bash
# 0. generate plan.toml from your phased plan doc, then REVIEW the DAG
opsx-plan compile docs/phased-implementation-plan.md
# Compile auto-activates the output plan when inside the repository.
# Plans outside the repository receive a warning and are not activated.
# Use -o to pick an explicit path, e.g.:
# opsx-plan compile docs/phased-implementation-plan.md -o plan.toml

# preview order, gates, and current status without running anything
opsx-plan run --dry-run

# preflight checks before an unattended run
opsx-plan doctor

# run the plan (serial; Ctrl-C is safe — state persists, resume by re-running)
opsx-plan run

# approve a pause_before gate
opsx-plan approve add-atomic-runtime-state-writes

# inspect / recover
opsx-plan status
opsx-plan reset <change-id>
```

Useful run flags: `--max-changes N`, `--budget-minutes N`, `--budget-usd N`,
`--only <id> [<id>...]`, `--create-only`, `--no-branch`, `--no-pr`,
`--repo <path>`.

Orchestrator state lives at `.opsx-plan/<plan-name>.state.json` in the host
project. For OpenCode-backed direct runs, that file is the authoritative
durable source for each change's phase, round, latest fix prompt, review
result, archive result, tracked change files, and last-stage log metadata.
Per-stage logs live at `.opsx-plan/logs/<change>.<stage>.r<round>.*.log`, and
compatibility worker-state snapshots used as phase inputs live under
`.opsx-plan/workers/`. Add `.opsx-plan/` to the host project's `.gitignore`.

## Plan manifest

See `orchestrator/samples/sample-plan.toml` for a canonical example. Per-change fields: `id` (required), `depends_on`,
`phase` (informational), `pause_before` (requires explicit `approve` before
dispatch — use for human gates like new-capability approvals or phase exit
reviews), `enabled` (set `false` for deferred changes), and per-change
`timeout_minutes` override.

Review the `depends_on` graph by hand before an unattended run. The
orchestrator validates ids and rejects cycles, but it cannot detect a
*missing* edge.

## How completion is verified

A change is marked `done` only when all of the following agree:

1. a fresh archive worker run returned machine-readable success for the current
   change
2. `openspec/changes/<id>` is absent from the worktree
3. a dated `openspec/changes/archive/YYYY-MM-DD-<id>` directory exists on disk
4. all `fast_checks` commands exit 0 (e.g. `openspec validate --all`,
   your fast test suite)

Whether an `archive(<id>):` commit is *also* required depends on the repo:

- **`openspec/changes/archive/` is tracked** (the default OpenSpec layout): the
  commit is required evidence. A missing or unreachable commit fails the change,
  because it means the archive was never durably recorded.
- **`openspec/changes/archive/` is gitignored** (as in this repo): the archiver
  has nothing to stage, so no commit is produced. The commit degrades to a
  corroborating signal — logged as a note when present, never blocking when
  absent — and the on-disk dated directory plus the change directory's removal
  are the load-bearing evidence.

The orchestrator decides which case applies by asking git whether the path is
covered by an ignore rule, independent of what happens to be tracked today.

The worker process exit code is never treated as success.

On startup the orchestrator reconciles recorded state against the repo:
recorded-done changes whose evidence has disappeared are downgraded to failed;
a stale `running` status from a killed run is recovered to pending; and a
direct change resumes from the persisted plan-owned phase and round in the plan
state file.

## Retry and failure policy

- For OpenCode-backed runs, `opsx-plan` owns the implement-review-archive loop
  directly. It persists the active phase, round, latest fix prompt,
  no-progress streak, review verdict, archive result, and tracked log path in
  the plan state file and resumes from that state on the next run.
- Review failures loop back to implement inside `opsx-plan`; the script
  advances rounds itself and stops when `max_rounds` or `no_progress_limit` is
  reached.
- `escalate_after_review_fails` (default 0, disabled) promotes the
  implement stage to a stronger model after *N* failed reviews (round *N*+1
  first uses the escalation model). The escalation model is resolved through
  the same per-adapter precedence ladder under the optional
  `implementer_escalation` role and exported as
  `OPSX_IMPLEMENTER_ESCALATION_MODEL`. Runs fail closed at plan load when
  `escalate_after_review_fails > 0` and the escalation role is unresolved.
- A plan missing one or more of the three stage invokes
  (`implement_invoke`, `review_invoke`, `archive_invoke`) fails at load time
  with a `PlanError` naming all three required keys — there is no fallback
  execution path. Direct dispatch is the only execution model; the legacy
  nested-controller path is removed.
- A change that archives successfully but then fails `fast_checks` is marked
  failed without retry (re-archiving an already archived change cannot fix the
  repo).
- A failed change blocks its dependents; independent branches keep running.
- `require_clean_tracked` (default true) refuses to start a new change while
  tracked files are dirty, so failures cannot bleed across changes.
  Untracked leftovers are allowed, matching the archiver contract.

## Adapter invocation

Defaults (override with `implement_invoke` / `review_invoke` /
`archive_invoke` in `[plan]`):

| adapter | invocation | state file |
|---|---|---|
| `opencode` | `opencode run --agent opsx-implementer --model "$OPSX_IMPLEMENTER_MODEL" --variant "$OPSX_IMPLEMENTER_VARIANT"`, and similarly for reviewer/archiver | `.opsx-plan/<plan>.state.json` |
| `claude-code` | `claude -p --agent opsx-implementer --model "$OPSX_IMPLEMENTER_MODEL" --permission-mode bypassPermissions --output-format json`, and similarly for reviewer/archiver | `.opsx-plan/<plan>.state.json` |
| `codex-cli` | Direct dispatch not available by default — `codex-cli` has no default stage invokes and a codex-cli plan missing explicit `implement_invoke` / `review_invoke` / `archive_invoke` keys fails at load time with a `PlanError` naming all three required keys. An operator can opt into direct dispatch by hand-writing all three invokes in `[plan]`. | `.opsx-plan/<plan>.state.json` |

The OpenCode invokes reference an `OPSX_*_VARIANT` variable alongside the
model. The orchestrator exports it per role from the resolved
`<role>_variant` configuration; when a role has no variant configured the
variable is exported empty and `opsx-plan` drops the `--variant` flag before
spawning, so the client's built-in default applies. Claude Code has no
reasoning-variant flag, so its invokes never reference a variant.

Verify your client's headless syntax and permission configuration before an
unattended run — for example, Claude Code's `-p` mode needs tool permissions
pre-granted (settings or `--permission-mode`), and Codex needs
`agents.max_depth >= 1`. Test with `--max-changes 1` first.

## Execution model

Serial by design: two mutating plan-stage runs in one worktree is a known failure
mode, and the archive commit per change keeps each step independently
revertable. If you later want parallel independent branches, run them in
separate `git worktree` checkouts with a merge step gated on `fast_checks` —
that belongs above this script, not inside it.

## Source layout

`orchestrator/opsx-plan.py` is the CLI entrypoint: argument parsing and
subcommand dispatch. It is not self-contained — the report, dashboard, and
cost-estimation commands, along with plan location/loading and a handful of
shared primitives (`log`, `PlanError`, plan status constants), live in the
importable `lib/orchestrator/` package alongside the existing `lib/metrics`,
`lib/pricing`, and `lib/models` runtime packages:

- `lib/orchestrator/base.py` — `log`, `utcnow`, `PlanError`, status constants,
  adapter defaults, header constants (`ARCHIVE_DIR_RE`, `TASK_RE`,
  `ADAPTER_CLIENTS`, `_RUNTIME_ROOTS`). Zero dependency on any other
  orchestrator module.
- `lib/orchestrator/planref.py` — plan location and loading (`load_plan`,
  `resolve_plan`, and the rest of the plan-resolution closure). Depends on
  `base`.
- `lib/orchestrator/cost.py` — `estimate_stage_cost` and its pricing-catalog
  helpers. Depends on `base`.
- `lib/orchestrator/groundtruth.py` — `git`, `change_dir`, archive-locating
  helpers, `verify_change_*`, `run_fast_checks`, and tracked-worktree helpers.
  Depends on `base`.
- `lib/orchestrator/state.py` — `.opsx-plan/<name>.state.json` accessors:
  `load_state`, `save_state`, `rec`, `set_status`, and task-count helpers.
  Depends on `base` and `groundtruth`.
- `lib/orchestrator/telemetry.py` — telemetry record construction and writing,
  plus usage/model extraction helpers. Depends on `base`, `cost`, and `state`.
- `lib/orchestrator/delivery.py` — branch resolution, PR prerequisites, PR
  body generation, and `attempt_pr_delivery`. Depends on `base` and
  `groundtruth`.
- `lib/orchestrator/doctor.py` — twelve individual `_check_*` preflight probes.
  Depends on `base`, `groundtruth`, `planref`, and `telemetry`.
- `lib/orchestrator/compiler.py` — compile source/output resolution, prompt
  construction, client invocation (`run_compile_client`), and TOML extraction.
  Depends on `base`.
- `lib/orchestrator/logs.py` — log discovery, parsing, and selection helpers.
  Depends on `state`.
- `lib/orchestrator/report.py` — `opsx-plan report`'s table/JSON rendering
  and `cmd_report`. Depends on `base` and `planref`.
- `lib/orchestrator/dashboard.py` — `opsx-plan dashboard`'s HTML rendering
  and `cmd_dashboard`. Depends on `base`, `planref`, and `report`.

Dependency direction (acyclic, mechanically verified by
`tests/orchestrator/test_module_layout.py`):

```
dashboard → report → planref → base
cost → base
groundtruth → base
state → groundtruth → base
telemetry → state → groundtruth → base
  (also telemetry → cost → base)
delivery → groundtruth → base
doctor → {groundtruth, telemetry, planref, base}
compiler → base
logs → state → groundtruth → base
```

Modules call across this package through the module object
(`from lib.orchestrator import base; base.log(...)`), never by importing
names directly — this keeps `mock.patch.object(module, "name", ...)`
effective regardless of which module resolves the call. An installed
`opsx-plan` that predates this layout (has `metrics`/`pricing`/`models` but
no `orchestrator` package under
`~/.local/lib/opsx-controller/lib`) exits with a diagnostic naming the
missing package rather than a bare `ModuleNotFoundError`, and `opsx-plan
doctor` reports such an installation as stale.

## Model Efficiency Workflow

See [`core/model-efficiency-workflow.md`](../core/model-efficiency-workflow.md)
for the operator workflow that uses `opsx-plan compile`, `opsx-plan run`,
`opsx-plan report`, and `opsx-plan dashboard` to benchmark model choices.
