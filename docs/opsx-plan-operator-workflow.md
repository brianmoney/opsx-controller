# opsx-plan: Operator Workflow Guide

How to activate, run, and supervise an `opsx-plan` from plan creation through
pull-request delivery, using the upgraded command surface introduced by the
operator-workflow-upgrade series.

This document covers the full lifecycle: compile, activate, preflight with
`doctor`, run with budgets, manage gates, inspect logs, monitor with
`report`/`dashboard`, receive notifications, and finish on a delivery branch
that opens a pull request.

> **Plan authoring**: For writing compilable markdown implementation plans, see
> [`core/plan-authoring.md`](../core/plan-authoring.md) — the single
> client-neutral reference for plan structure, dependency forms, and compile
> conventions.

---

## Quick Start

```bash
# 1. Author a plan document (markdown), then compile to TOML.
#    Defaults to OpenCode; use --adapter to select another client.
opsx-plan compile docs/my-plan.md -o plan.toml

# For Claude Code users:
opsx-plan compile --adapter claude-code docs/my-plan.md -o plan.toml

# 2. Activate the plan (subsequent commands resolve it automatically)
opsx-plan use plan.toml

# 3. Run preflight checks (or opsx-plan doctor --adapter claude-code for
#    plan-less preflight against a specific adapter)
opsx-plan doctor

# 4. Dry-run to review the DAG and gate config
opsx-plan run --dry-run

# 5. Run the plan (interruptible; state persists, resume by re-running)
opsx-plan run

# 6. Monitor progress
opsx-plan status
opsx-plan logs
opsx-plan report plan.toml

# 7. When complete: review the delivery branch and PR (if configured)
```

The `opsx-plan` command (and `opsx-run`) default to the current working
directory as the host project root. Use `--repo <path>` to point at a different
repository; all state is placed under `<repo>/.opsx-plan/`. Add `.opsx-plan/`
to the host project's `.gitignore`.

---

## Plan Activation

`opsx-plan` resolves the plan source through a standard three-level precedence.
You do not need to repeat the plan path on every command.

### Resolution precedence

| Priority | Source | Example |
|---|---|---|
| 1 (highest) | Explicit CLI argument | `opsx-plan run plan.toml` |
| 2 | `OPSX_PLAN` environment variable | `export OPSX_PLAN=plan.toml` |
| 3 | Active-plan pointer file | Set by `opsx-plan use` or auto-set after `compile`/`run` with an explicit in-repository path |

When a plan path is provided explicitly (as a CLI argument) and the plan
resides inside the repository, the command also auto-sets the active-plan
pointer so later commands resolve the same plan without repeating the path.
Plans outside the repository cannot be auto-activated.

### Commands for plan activation

```bash
# Activate a plan for subsequent commands
opsx-plan use plan.toml

# Check which plan is active
opsx-plan status       # prints "plan: <name>  (active: plan.toml)"

# Deactivate or override for one command by using an explicit path
opsx-plan run other-plan.toml   # runs 'other-plan.toml' and sets it as active
```

### Fail-closed: stale active-plan pointer

The active-plan pointer is stored as a repo-relative path under
`.opsx-plan/active-plan`. If the referenced TOML file is deleted or moved,
commands that resolve the active plan fail with a clear error message:

```
active plan pointer references missing file: plan.toml
Set a new active plan with: opsx-plan use <plan.toml>
```

This protects against silently operating on a plan that no longer exists.

The active-plan pointer is **never** resolved through `OPSX_PLAN` or any
fallback — it must reference an existing, loadable plan. You recover by
activating a new plan with `opsx-plan use`.

---

## Model Configuration

Model selection is stored per adapter and per role in
`~/.config/opsx-controller/models.toml`, with an optional machine-local
`<repo>/.opsx-plan/models.toml` override (gitignored by `write_active_plan`).
The required roles are `controller`, `implementer`, `reviewer`, `archiver`.
The optional roles are `implementer_escalation` (used by
`escalate_after_review_fails`) plus the supervised roles `supervisor`,
`supervised_author`, `acceptance_reviewer`, `fixer`, and `verifier`.
`opsx-plan` resolves all roles for the active plan's adapter when the plan
loads and exports them as `OPSX_*_MODEL` for the rest of the process, so
the active plan's adapter automatically gets the right model set with no
manual switching. A resolved optional role exports its
`OPSX_<ROLE>_MODEL` variable; an unresolved optional role is left unset and
never blocks a run on its own. The escalation role is optional: leaving it
unresolved does not block runs unless `escalate_after_review_fails > 0`.

The supervised roles are inert until the supervision enforcement changes
land: registering or resolving one does not change legacy resolution,
activation, or dispatch, and a configuration that defines none of them
resolves and dispatches exactly as before.

```bash
opsx-plan models init                       # seed the file from the current environment
$EDITOR ~/.config/opsx-controller/models.toml
opsx-plan models show --adapter opencode     # inspect resolution and source
opsx-plan models show --adapter claude-code  # same file, different adapter table
opsx-plan models env --adapter opencode      # emit shell export statements
```

Precedence, highest first: repo-local `[adapters.<adapter>]`, user-global
`[adapters.<adapter>]`, repo-local `[defaults]`, user-global `[defaults]`,
then the ambient `OPSX_<ROLE>_MODEL` environment variable (the sole
mechanism before this file existed, kept as a fallback so existing `.env`
setups keep working until you create `models.toml`). See
`models.example.toml` at the repository root for the full file shape.

### The inexpensive-model allowlist

Supervised jobs additionally use an operator-maintained inexpensive-model
allowlist, stored in the same `models.toml` files in an `[allowlist]` table:

```toml
[allowlist]
models = ["deepseek/deepseek-v4-flash", "moonshotai/kimi-k2"]
```

The allowlist has the same file locations and precedence as roles (repo-local
first, then user-global) and **no** environment-variable source. A repo-local
`[allowlist].models` replaces the user-global list wholesale — lists never
merge, including when the local list is explicitly empty. When neither file
has the table the effective allowlist is empty and resolution still succeeds;
a present-but-malformed table fails with a named error naming the offending
file. The allowlist is never consulted for legacy, unsupervised runs.
`opsx-plan models show` prints the effective allowlist and its source when
configured, plus each supervised dispatch role's membership; `opsx-plan
doctor` reports the same information without failing on unconfigured roles.

A model change in `models.toml` takes effect on the next `opsx-plan run` —
no installer re-run needed for direct dispatch, the default execution path
for every adapter. `opsx-plan doctor` reports each resolved model with its
source and flags an identifier that doesn't match the target adapter's
syntax (see [Preflight with `doctor`](#preflight-with-doctor) below).

---

## Preflight with `doctor`

`opsx-plan doctor` runs a suite of preflight checks without dispatching any
stages. It is safe to run at any time and never modifies state. Use it before
an unattended run to catch configuration problems early.

```bash
opsx-plan doctor

# For plan-less preflight, specify the adapter explicitly:
opsx-plan doctor --adapter claude-code
```

### Checks performed

| Check | What it validates |
|---|---|
| Installed orchestrator matches repo copy | SHA-256 comparison of `~/.local/bin/opsx-plan` against `orchestrator/opsx-plan.py` |
| Model roles resolve for the target adapter | All four roles (`controller`, `implementer`, `reviewer`, `archiver`) resolve for the resolved plan's adapter via `models.toml`/ambient environment; reports each resolved model with its source. When no plan is active, `--adapter` selects the adapter to resolve against (defaults to `opencode`). |
| Resolved model identifiers match adapter syntax | Flags a provider-prefixed identifier under `claude-code` or a bare identifier under `opencode`, before it fails at dispatch |
| Supervised model configuration is reported | Prints the effective `[allowlist]` and its source (or `absent`), plus each supervised dispatch role's resolution and membership. Informational: an unconfigured supervised role is never an error. |
| `openspec` available (repo or global) | OpenSpec CLI resolves repo-locally first (`<repo>/node_modules/.bin/openspec`), falling back to a global install on `PATH` |
| OpenSpec initialized in repo | The repo has a durable `openspec/config.yaml` (written by `openspec init`) **and** `openspec list --json` resolves a healthy root from the repo directory. Direct-dispatch workers read their per-project phase prompts from files `openspec init` writes, so an uninitialized repo ships workers that fail mid-run; the check fails closed with the exact `openspec init` command and the installed CLI version. |
| Adapter client on PATH | e.g. `opencode`, `claude`, or `codex`. When `--adapter` is set without a plan, validates the specified adapter's client. |
| No tracked bytecode | No `__pycache__/` or `.pyc` files tracked in git |
| Tracked tree is clean | No uncommitted modifications to tracked files |
| Plan loads successfully | The resolved plan TOML is valid and parses without errors |
| PR delivery prerequisites | When `create_pull_request = true`: `gh` on PATH and a git remote configured |
| Direct-dispatch worker agents installed | When the resolved plan uses direct dispatch: the configured adapter's `opsx-implementer`, `opsx-reviewer`, and `opsx-archiver` agents exist in that adapter's agent directory. Skipped when no plan is resolved or the plan does not use direct dispatch. |

### Doctor failure behavior

A doctor check failure prints a cross-mark (✗) with a remediation hint. The
`doctor` command exits with status 1 on any failed check (0 if all pass).
The `doctor` command itself does not gate `run` — it is a diagnostic that
reports findings without blocking anything. The same checks re-run as warnings
before each `run` (visible as ⚠ lines) without changing the run outcome.
However, `opsx-plan run` has its **own independent fail-closed guards** (e.g.,
`require_clean_tracked`, PR delivery preflight, and the OpenSpec-initialization
gate) that will refuse to dispatch stages regardless of doctor results. Fix
doctor failures before an unattended run; treat them as actionable, not
informational.

#### OpenSpec-initialization gate on `run`

`run` fails closed — before any dispatch — when the repo is not initialized
(no `openspec/config.yaml` or `openspec list --json` cannot resolve a root),
printing the exact `openspec init` command to run. Direct-dispatch workers read
their per-project phase prompts from files that `openspec init` writes, so an
uninitialized repo would otherwise dispatch workers that fail mid-run for a
missing prompt file. Pass `--skip-openspec` only when you deliberately want to
proceed without the check (dispatch may then fail once workers cannot find
their prompt files). `--dry-run` never triggers the gate.

---

## Plan Manifest

The plan manifest is a TOML file with a `[plan]` table and one or more
`[[changes]]` entries. A canonical example is at `orchestrator/samples/sample-plan.toml`.

### `[plan]` table: key config keys and defaults

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string | filename stem | Plan display name |
| `adapter` | string | `"opencode"` | Client adapter: `opencode`, `claude-code`, `codex-cli`, or `dsh` |
| `timeout_minutes` | float | `90` | Per-change stage timeout |
| `max_rounds` | int | `5` | Implement-review loop ceiling |
| `no_progress_limit` | int | `2` | Consecutive no-progress rounds before failing |
| `escalate_after_review_fails` | int | `0` | Promote implement to escalation model after *N* failed reviews. First escalates in round *N*+1 (`N=2` escalates round 3). `0` disables. |
| `fast_checks` | list\[str\] | `[]` | Post-archive CLI commands (all must pass) |
| `check_timeout_minutes` | float | `15` | Timeout for each fast check |
| `require_clean_tracked` | bool | `true` | Refuse to start a change on a dirty tracked tree |
| `notify_cmd` | string | `""` (disabled) | Command invoked for run-event notifications |
| `plan_doc` | string | `""` | Source markdown plan for `create_invoke` |
| `create_invoke` | string | `""` | Authoring command for auto-creating changes |
| `create_timeout_minutes` | float | `30` | Create stage timeout |
| `create_max_attempts` | int | `2` | Create retry ceiling |
| `review_created` | bool | `true` | Require operator `accept` before driving created changes |
| `created_check` | string | `"openspec validate {change} --strict"` | Post-create validation |
| `state_file` | string | adapter default | Controller state file path |
| `implement_invoke` | string | adapter default | Direct implement command |
| `review_invoke` | string | adapter default | Direct review command |
| `archive_invoke` | string | adapter default | Direct archive command |
| `acceptance_invoke` | string | adapter default (`""` on adapters without the stage) | Supervised acceptance-review command; the supervised acceptance stage fails closed when empty |
| `fix_invoke` | string | adapter default (`""` on adapters without the stage) | Supervised fixer command used by the acceptance `fix` route |
| `verify_invoke` | string | adapter default (`""` on adapters without the stage) | Supervised verifier command used by the acceptance `fix` route |

### `[[changes]]` entry fields

| Key | Type | Default | Description |
|---|---|---|---|
| `id` | string | **required** | Unique change identifier (slug) |
| `phase` | int | none | Phase number for display ordering |
| `depends_on` | list\[str\] | `[]` | IDs of changes that must complete first |
| `pause_before` | bool | `false` | Wait for `opsx-plan approve` before running |
| `pause_before_human_only` | bool | gated: `true`; ungated: `false` | Approval authority for a `pause_before` gate: absent on a gated change means human-only; `false` delegates release to the supervised job's policy-bound authority; `true` without `pause_before = true` is a load error |
| `enabled` | bool | `true` | Set `false` to defer a change |
| `timeout_minutes` | float | plan-level timeout | Per-change stage timeout override |
| `create_invoke` | string | plan-level create_invoke | Per-change authoring command override |
| `create_max_attempts` | int | plan-level value | Per-change create attempt override |

### `[plan.git_delivery]` config keys

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Enable branch/PR delivery for this plan |
| `branch` | string | `"opsx/<name>"` | Delivery branch name (derived from plan name if unset) |
| `base_ref` | string | current branch | Base ref for the delivery branch (current HEAD if unset) |
| `create_pull_request` | bool | `false` | Push the branch and open a PR after all changes complete |

**Constraint:** `create_pull_request = true` requires `enabled = true`. Setting
`create_pull_request` without `enabled` is a plan-load error.

### Direct dispatch is adapter-neutral

A plan takes the direct implement-review-archive path — bounded per-stage
worker subprocesses, plan-owned round control, stage logs under
`.opsx-plan/logs/`, and telemetry — whenever all three of `implement_invoke`,
`review_invoke`, and `archive_invoke` resolve to a non-empty command. This is
a configuration test only; it does not depend on which `adapter` is
configured. A plan missing one or more of the three stage invokes fails at
load time with a `PlanError` naming all three keys (`implement_invoke`,
`review_invoke`, `archive_invoke`) — there is no fallback execution path.

`ADAPTER_DEFAULTS` supplies all three stage invokes for both `opencode` and
`claude-code`, so plans using either adapter take the direct path with no
manifest changes. The `claude-code` defaults are:

```
implement_invoke = claude -p --agent opsx-implementer --model "$OPSX_IMPLEMENTER_MODEL" --permission-mode bypassPermissions --output-format json
review_invoke    = claude -p --agent opsx-reviewer   --model "$OPSX_REVIEWER_MODEL"   --permission-mode bypassPermissions --output-format json
archive_invoke   = claude -p --agent opsx-archiver   --model "$OPSX_ARCHIVER_MODEL"   --permission-mode bypassPermissions --output-format json
```

`--permission-mode bypassPermissions` is required for unattended runs — in
print mode an interactive permission prompt cannot be answered. Tool scope is
still bounded by each worker agent's own `tools:` frontmatter (installed at
`~/.claude/agents/` or `<project>/.claude/agents/`), the same place OpenCode
bounds tool scope via its `permission:` block.

`codex-cli` has no `implement_invoke`/`review_invoke`/`archive_invoke`
defaults. Because the gate is configuration-driven, an operator can still
opt `codex-cli` (or any adapter) into direct dispatch by hand-writing all
three invokes in `[plan]` — this is reachable but unvalidated.

Any stage invoke may be overridden per-plan by setting the corresponding key
in `[plan]`; overriding one stage leaves the other two on their adapter
defaults.

#### Environment variable expansion in stage invokes

Before dispatching a stage, `opsx-plan` expands `$VAR`/`${VAR}` references in
each argument of the resolved invoke string — this is how `OPSX_*_MODEL`
selects the per-stage model for both `claude-code` and `opencode` direct
stage invokes (both now pass an explicit `--model "$OPSX_*_MODEL"` argument).
This also applies to the templated `create_invoke` authoring command: the
`{change}`, `{plan_doc}`, and `{controller_model}` placeholders are formatted
first, then each resulting argument is expanded exactly like a direct stage
invoke, so a create command such as
`opencode run --model "$OPSX_CONTROLLER_MODEL" ...` receives the resolved
model value. If a referenced variable is unset, the stage fails immediately
with a message naming the variable — no client subprocess is started. The
`exec[stage]` log line always shows the already-expanded command (with the
worker input block elided).

The four `OPSX_*_MODEL` variables are populated once per process, when the
plan loads, by resolving each role against the plan's adapter through
`~/.config/opsx-controller/models.toml` (see [Model
Configuration](#model-configuration) below) — not read directly from ambient
environment variables the way earlier versions worked, though ambient
variables remain the fallback while no `models.toml` exists.

**`doctor` validates model-string format.** The `Resolved model identifiers
match adapter syntax` check in `opsx-plan doctor` catches the
adapter/identifier mismatch that used to only surface at dispatch time.
OpenCode-style provider-prefixed ids (for example `deepseek/deepseek-v4-pro`
or `openai/gpt-5.6-luna`) are valid for the `opencode` adapter but are not
valid `--model` arguments for the Claude Code CLI; a bare id like
`claude-sonnet-5` is rejected the other way, since `opencode` requires the
`provider/model` form. `doctor` fails closed and names the offending role
before the stage ever dispatches — no need to wait for a client-side
rejection. Run `opsx-plan models show --adapter <adapter>` to inspect
resolution and fix the offending role in `models.toml`.

### Unrecognized manifest keys are silently ignored

`load_plan()` in `orchestrator/opsx-plan.py` builds its config from an explicit
`.get()` per known key. Any other key in `[plan]` or in a `[[changes]]` entry is
dropped with no warning and no load error. A manifest that configures behavior
this controller does not implement therefore loads clean and runs as if the key
were never written, which reads as "configured and working" in review.

Consequences for plan authors:

- Treat any key not listed in the three tables above as decorative. Compiled
  manifests in particular carry descriptive metadata (`title`, `purpose`,
  `planning_principles`, `pause_reason`, and similar) that the runtime never
  reads.

Worth fixing at the source: a plan-load warning that lists unknown keys would
catch this whole class of drift at `doctor` time rather than after a run.

---

## Compiling a Plan

`opsx-plan compile` converts a markdown implementation plan into a runnable
TOML manifest. It invokes the selected compile client (default `opencode`,
also supports `--adapter claude-code`) with the `controller` role resolved
for that adapter. The `codex-cli` and `dsh` adapters are not supported for
compilation.

```bash
# Required: a controller model resolved for the opencode adapter
opsx-plan models show --adapter opencode   # confirm it resolves

# Compile a markdown plan to TOML
opsx-plan compile docs/my-plan.md -o plan.toml

# Overwrite an existing manifest
opsx-plan compile docs/my-plan.md -o plan.toml --force
```

### Compile behavior

- Refuses to overwrite an existing output file unless `--force` is passed.
- Fails before invoking the compile client if the `controller` role does not
  resolve for the selected adapter (via `models.toml` or the ambient
  `OPSX_CONTROLLER_MODEL` fallback), or if the resolved model identifier
  violates the adapter's model syntax rules.
- The generated TOML is validated locally: it must parse as valid TOML, pass
  `load_plan()` (unique ids, known deps, no cycles), and is written through a
  temporary file with atomic replacement.
- On success, auto-activates the output plan when the output path is inside the
  repository; plans compiled to a location outside the repository are not
  auto-activated (a warning is printed instead).

### Compile inputs

The compiler builds a self-contained prompt that includes:
- The source markdown content
- The expected TOML schema (derived from the plan loader)
- Dependency-resolution rules
- Adapter defaults
- Repository template plan pairs from `openspec/plans/` when available

Always run `opsx-plan run --dry-run` after compiling to review the DAG before
an unattended run.

---

## Running a Plan

```bash
# Preview order, gates, and current status without dispatching stages
opsx-plan run --dry-run

# Run all ready changes (serial; Ctrl-C is safe — state persists)
opsx-plan run

# Run up to 3 changes, then stop
opsx-plan run --max-changes 3

# Restrict this run to specific change ids (dependencies must be done)
opsx-plan run --only add-feature-a add-feature-b

# Create + verify ready changes without driving them
opsx-plan run --create-only
```

### Run behavior

- **Serial execution**: Changes run one at a time. Two mutating plan-stage runs
  in one worktree is a known failure mode.
- **Interruptible**: Ctrl-C sends SIGTERM to the active worker, persists state,
  and exits. Resume by re-running the same command.
- **Dirty-tree refusal**: When `require_clean_tracked = true` (default), the
  orchestrator refuses to start a new change while tracked files are dirty.
  Untracked leftovers are allowed.
- **Reconciliation on startup**: The orchestrator reconciles recorded state
  against the repository. A stale `running` status from a killed run is
  recovered to `pending`. For direct dispatch (any adapter with all three
  stage invokes configured), when repository archive evidence exists but the
  plan state lacks matching archive worker evidence, the change is marked as
  failed (fail-closed).

### `--only` flag

Restricts this invocation to the listed change ids. Changes not listed are
skipped for this run only. Listed changes must have all dependencies done.

### `--create-only` flag

Creates and verifies all ready changes (those whose `create_invoke` is
configured) without dispatching implementation stages. Use to batch-create +
verify the actionable frontier.

### `--dry-run` flag

Prints the plan DAG, current status of each change, and phase ordering without
dispatching any worker. Safe to run at any time.

---

## Budget Controls

All budget controls are **opt-in flags** — the default is no budget enforcement.

### Wall-clock budget: `--budget-minutes`

Sets a maximum elapsed wall-clock time for the run. The orchestrator checks the
budget before dispatching each stage. When the budget is exhausted, the current
change is left in `pending` status so it can resume on the next run.

```bash
opsx-plan run --budget-minutes 60
```

### Spend budget: `--budget-usd`

Sets a cumulative cost ceiling based on telemetry-reported estimates. Before
dispatching each stage, the orchestrator reads telemetry records for the
current `run_id` and sums all `estimated`-status cost entries. When cumulative
spend meets or exceeds the budget, the current change is left pending.

```bash
opsx-plan run --budget-usd 5.00
```

### Budget stop semantics

Budget exhaustion is **not a failure**. The change is left in `pending` status
with a reason like `"budget exhausted while waiting to run archive"` or
`"spend budget exhausted: $5.23 >= $5.00 (12 stages resolved, 1 unresolved)"`.
Re-run to continue from where it left off.

The spend budget counts only stages whose cost status is `estimated`.
`unresolved` and `unavailable` stages are listed in the exhaustion message but
do not contribute to the cumulative total.

---

## Gate Controls

### Manual gates: `pause_before`

A change with `pause_before = true` waits for explicit approval before the
orchestrator dispatches it. Use for human judgment gates such as
new-capability approvals or phase exit reviews.

Gates are human-only by default: only a human operator can release them. To
delegate release to the supervised job's policy-bound authority, set
`pause_before_human_only = false` on the gated change. Setting
`pause_before_human_only = true` on a change without `pause_before = true`
is a plan-load error, and non-boolean values are rejected rather than
coerced. Manifests that never set the key keep their existing human-approved
semantics.

```bash
# Approve a single change
opsx-plan approve add-new-capability

# Approve all changes currently awaiting approval
opsx-plan approve --all

# Approve by phase number (P prefix)
opsx-plan approve P3
```

#### Broker-mediated approval in a registered supervised job

When the worktree holds a **registered supervised job**, the approval commands
above are no longer direct JSON edits: they are **broker mediated**. The broker
in the trusted authority domain is the sole authority that releases approval
and acceptance gates, and each release is recorded as a durable receipt bound
to the exact checkpoint (the change and gate kind) and to the current
**material revision** (the gate-relevant manifest fields taken from the
protected snapshot, plus the snapshot identity and explicit policy revision).

- **Operator approvals** go through the **operator OS-authenticated path**.
  The operator endpoint authenticates the kernel-reported peer uid
  (`SO_PEERCRED`), not a token, so the approval is genuinely yours:
  ```bash
  # The operator runs these as the operator principal
  opsx-plan approve add-security-hardening        # one change, via the operator path
  opsx-plan approve --all                          # every human-only gate awaiting approval
  opsx-plan accept add-new-capability              # acceptance is operator-only too
  opsx-plan reset add-security-hardening           # an operator reset is a durable receipt
  ```
  The operator endpoint is hosted by the trusted service, which boots the
  broker session and **installs the projection writer** before accepting any
  request:
  ```bash
  # The trusted service runs this (once the host is provisioned)
  opsx-plan supervise serve <plan.toml>            # host the endpoint surface
  # A single request then exit, useful for smoke checks:
  opsx-plan supervise serve <plan.toml> --once
  ```
  Because the writer is installed at boot, every receipt the service records
  regenerates the JSON projection from broker and ledger state, so a live
  approval updates legacy `status`/`report` views. The session serves only the
  current worktree's active registration: an explicit `--job-id` naming another
  worktree's job (or a terminal one) is refused as a mismatch, so the service
  can never record receipts for a foreign job while projecting into this repo.
  A missing service store, a worktree with no active job, an unprovisioned
  principal, or an unbindable socket fails the command closed with
  `BrokerUnavailableError` before any request is served, tearing the partially
  booted session down rather than leaking a raw socket error.
- **Delegated gates** (`pause_before_human_only = false`) are released only by
  the job's scoped service action, never by an operator approval. Asking the
  operator path to approve a delegated gate is refused.
- **Worker refusals** carry named errors. A worker-domain or unauthenticated
  process that runs a mediated command in a registered job fails closed:
  ```
  error: BrokerMediationError: this worktree holds a registered supervised job;
  run dispatch is broker mediated and only the supervised execution may dispatch
  ```
  When the job is registered but the broker cannot be reached (for example the
  supervised service is not running), the command fails closed with
  `BrokerUnavailableError` rather than silently falling back to JSON
  authority. A relied-upon approval whose material revision has changed is
  reported as `StaleMaterialError` and the gate is re-armed.
- **When a gate re-arms.** A receipt stays valid across unrelated updates
  (task progress, telemetry, another change's state). It is invalidated — the
  gate re-arms and awaits approval again — when you record an **explicit plan
  or policy revision**: registering a new protected manifest snapshot, or
  revising the job policy. Worker edits to the repo plan or JSON state never
  move the material revision.
- **Tampered files do not escape mediation.** A worker that removes
  supervised markers from the JSON state or the repo plan does not turn the
  job back into an unmediated one; registration is read from service-owned
  storage.
- **A substituted store fails closed.** Registration detection always consults
  the authority-validated service-owned store (the service principal's home, or
  the root-owned `/var/lib` directory), never the invoking user's home. Setting
  `OPSX_SUPERVISOR_STATE_FILE` to a missing or empty external path does not
  hide a registered job: it fails closed with `BrokerUnavailableError` instead
  of falling back to legacy JSON. Batch and phase selection
  (`approve --all`, `approve P<N>`, `accept --all`, `reset --failed`) resolve
  membership, order, phases, and `review_created` from the protected snapshot,
  so editing the repo plan cannot redirect an approval.
- **Dispatch authorization is not an environment variable.** A worker cannot
  self-authorize by exporting `OPSX_SUPERVISED_EXECUTION=1`, by writing a
  `.opsx-plan` fencing file, or by importing/assigning any in-process
  supervision helper (the control plane exposes no local dispatch flag). A
  registered job dispatches only inside the supervised execution the trusted
  service started, proven from the job's service-owned ledger fencing record
  (live boot identity and process start time, not released or fenced) plus
  real process ancestry. Anything less is refused with
  `BrokerMediationError`.
- **Legacy jobs are unchanged.** An unregistered worktree takes the legacy
  JSON path byte-identically and needs no broker, ledger, or backend.

#### The acceptance stage in a supervised round

A registered supervised job's change runs one extra review stage between an
implementation review `pass` and archive. It is dispatched through the same
gated journal boundary as the other stages, under the policy's pinned
`acceptance_reviewer` role, and it reviews the change's **real artifacts** — the
accepted plan and its dependency edges, proposal, design, tasks, spec deltas
with their delta identity, and the referenced canonical specs. It returns
exactly one of three outcomes:

- **`accept`** — the change satisfies its accepted intent. The loop advances to
  archive only on a non-stale accept whose `artifacts_reviewed` names exactly
  the authoritative artifact set the engine derived from the captured review
  set (the manifest snapshot hash, every dependency edge, and every file
  artifact; a partial, arbitrary, or manifest-omitting accept fails the change
  with a named `acceptance_invalid` error instead of advancing). The verdict is
  recorded in the append-only `acceptance_reviews` ledger table against the
  exact **artifact revision** it reviewed; the per-change JSON `acceptance`
  posture is only a projection of that ledger state.
- **`fix`** — a mechanical defect the pinned cheap `fixer` can repair. The
  engine dispatches the fixer, then the independent `verifier`; the repair is
  consumed only after the verifier validates the actual diff. A verified repair
  runs a fresh acceptance, and the route is bounded by the change's round
  budget, failing with a reason naming the unrepaired defect on exhaustion.
- **`escalate`** — a hard judgment returned to the primary session. It is
  recorded as unresolved blocking state and never defaulted to `accept` or
  `fix`; archive is blocked until the primary resolves it, after which a fresh
  acceptance runs.

What the operator should expect:

- A stale or unresolved verdict is surfaced, not hidden. If a worker edits the
  change's artifacts between revision capture and the verdict, the engine
  recomputes the revision before recording an `accept` and rejects the stale
  verdict, running a fresh acceptance over the new revision (bounded by the
  round budget).
- A failing created-change check (`openspec validate <change> --strict` by
  default) blocks the stage with the recorded reason and no accept is recorded.
- An `accept` is a review outcome, not an approval authority: it releases no
  `pause_before` gate, does not satisfy the operator `acceptance` receipt for an
  orchestrator-created change, and does not replace the implementation review
  verdict or the task-completeness gates.
- OpenCode is the first adapter with the stage's invocations. A supervised run
  on an adapter whose `acceptance_invoke` is empty fails closed with a named
  error rather than skipping acceptance; legacy unregistered runs never enter
  the stage.
- The acceptance and repair durable writes fail closed. If the verdict cannot be
  written to the `acceptance_reviews` ledger, the accept does not satisfy the
  stage and the change fails with a named persistence error instead of advancing
  to archive; if the verifier's repair evidence cannot be written, the repair is
  not consumed and no fresh acceptance runs. The `acceptance.persistence_error`
  field in the state projection names the failure.

### Created-change acceptance: `review_created`

When `review_created = true` (default), changes created by the orchestrator
stop at `awaiting_acceptance` so you can review the proposal and spec deltas,
then continue with `opsx-plan accept`. Changes you authored by hand are
presumed reviewed and skip this gate.

```bash
# Accept a single orchestrator-created change
opsx-plan accept add-new-capability

# Accept all changes currently awaiting acceptance
opsx-plan accept --all
```

The `accept` command re-verifies that the created artifacts pass the
`created_check` (default: `openspec validate <id> --strict`) before accepting.

### Failure recovery: `reset`

Resets a failed change to pending for a retry. Resetting clears the change's
entire state record (rounds, review results, archive state, history) to factory
defaults and sets `max_rounds` from the current plan config.

```bash
# Reset a single change
opsx-plan reset failed-change-id

# Reset all failed changes
opsx-plan reset --failed

# Reset by phase
opsx-plan reset P2
```

### Worktree execution lock

The mutating commands `opsx-plan run`, `opsx-plan reset`, `opsx-run`, and its
alias `opsx-plan run-one` acquire a per-worktree **execution lock** before
performing any mutating work and hold it until they exit, releasing it on both
success and failure. All four names serialise against the same lock in the
same worktree, including `opsx-run` against `opsx-plan run-one`.

When another mutating process already holds the lock, the command fails fast
with the named `LockContentionError` and a non-zero exit code rather than
waiting or proceeding unlocked:

```
error: worktree /path/to/repo execution lock is already held by 'opsx-plan run (my-plan)'; refusing to proceed
```

When the lock is held by a **supervised** execution, an ordinary mutating
command is refused with the named `SupervisedOwnershipError`, the one
intentional behavior change for legacy runs:

```
error: worktree /path/to/repo is owned by a supervised execution ('...', job 3); refusing to proceed
```

An ordinary run still works with no supervisor ledger and no separate
principal; this refusal is the only new rejection.

The lock is **not** acquired by the gate commands (`approve`, `accept`) or by
the read-only diagnostics (`doctor`, `status`, `logs`, `report`,
`dashboard`). Those run to completion even while a mutating command holds the
lock, so an operator can always release a gate or inspect a run.

The lock lives in two files under `.opsx-plan/`: `execution.lock` (the
kernel-held inode that provides mutual exclusion) and `execution-lock.json`
(a fencing record carrying the owner and its process identity — pid, process
start time, boot identity — so a stale owner is distinguishable from a live
one across PID reuse and reboots). A stale owner is fenced only after
verified quiescence — both the kernel-held lock released **and** no live
process matching the recorded identity (matching boot identity plus matching
process start time) — so a live owner is refused even if its flock was
released. When the platform exposes no boot identity or process start time,
identity liveness cannot be established and the kernel-held flock alone
arbitrates. Release rewrites the record to its released state
before unlocking; if that write or the supervised release event cannot be
persisted, the kernel lock is still released but the command reports a named
release failure rather than a false success. See `core/plan-supervision.md`
for the full contract.

---

## Monitoring

### Status

```bash
opsx-plan status           # resolve active plan
opsx-plan status plan.toml # inspect a specific plan
```

Status reconciles state against the repository and prints each change's
computed status with phase ordering. Awaiting-approval, awaiting-acceptance,
and failed changes print guidance for the next operator command (approve,
accept, or reset respectively).

When the resolved worktree has a registered supervised job, `status` also
prints a supervised-job block: the job state and progress, the active policy
revision, the budget posture against its protected limits, any open human or
stop waits, and recent incidents. For a plan with no registered job the output
is byte-identical to the pre-supervision behavior.

`status --json` emits the same plan summary plus a structured `supervision`
object; the object is omitted entirely when no supervised job is registered.

Output example:

```
plan: my-plan  (active: plan.toml)
  P1 add-feature-a          done
  P2 add-feature-b          awaiting_approval
    → opsx-plan approve add-feature-b
  P2 add-feature-c          failed (no progress ceiling reached)
    → opsx-plan reset add-feature-c
  supervised job:
    job 7: paused (updated 2026-07-01T10:05:00+00:00)
    policy revision: 2
    budget: total_cost_usd=25.0 charged_cost_usd=4.5 elapsed_minutes=42
    human wait: gate:approval:add-feature-a (2026-07-01T10:01:00+00:00)
```

### Logs

```bash
# Show the latest stage log for the resolved plan
opsx-plan logs

# Show the latest log for a specific change
opsx-plan logs --change add-feature-a

# Show the latest review log
opsx-plan logs --stage review

# List all available matching logs
opsx-plan logs --list

# Follow an in-progress log like tail -f
opsx-plan logs --follow
```

Log selection prefers recorded state metadata (the stage's stored log path),
falling back to the newest matching file in `.opsx-plan/logs/` by modification
time.

### Report

```bash
# Human-readable tables for the latest run
opsx-plan report plan.toml

# JSON output for machine consumption
opsx-plan report plan.toml --json

# Filter by change, run, stage, or model substring
opsx-plan report plan.toml --change add-feature-a
opsx-plan report plan.toml --run-id <run-id>
opsx-plan report plan.toml --stage implement
opsx-plan report plan.toml --model gpt-4o

# Target a single-change run's derived manifest
opsx-plan report --for-change add-feature-a

# Recompute costs from stored usage against the current pricing catalog
opsx-plan report plan.toml --reprice
```

The report includes:
- **Plan Summary**: overall completion rate, duration, tokens, cost
- **Per-Change Metrics**: status, rounds, duration, tokens, cost per change
- **Stage Aggregates**: average durations, review failure rate, cost per change
- **Model Leaderboard**: grouped by `(implementer, reviewer, archiver)` tuple
- **Supervision** (registered supervised jobs only): job state, budget posture,
  open waits, recent incidents, steering request acknowledgements, and the
  cost-per-correct-completion definition

For a registered supervised job, `report --json` adds a top-level
`supervision` object carrying the same fields. It is built read-only from the
ledger, never mutates the ledger, telemetry, or execution state, and leaves
every existing key and value untouched. A plan with no registered job produces
output with no supervision section, and supervisor-family role usage stays out
of the legacy model leaderboard.

#### Steering acknowledgements

`opsx-plan supervise pause`, `drain`, and `cancel` return the durable
`request_id` of the recorded steering request and its acknowledgement. A
request is acknowledged only at the safe boundary for its kind: `stop` for a
pause or a drained stop hold, `terminal` for a cancel, `change` for a
per-change pause/steer or retry/reset request, and `policy` for a job-level
policy revision, which is applied atomically and reaches its boundary
immediately. The acknowledgement names the boundary reached
and is recorded once; re-acknowledging a request is a no-op, and an
unacknowledged request survives a restart and is acknowledged at the next
boundary. `--json` on those commands emits the same identity and
acknowledgement as structured output. Notification selection never advances the
durable delivery watermark; the high-water advances only after a response is
delivered, so a delivery failure is redelivered on reboot or retry rather than
suppressed.

#### Repricing historical costs

Telemetry is append-only and each record keeps the pricing snapshot it was
estimated with, so a record that was `unresolved` (or priced from an older
catalog) stays that way on disk. `--reprice` shows what the recorded usage
would cost under the **current** catalog: each selected record's `cost` is
recomputed in memory from its stored `usage` and `model`, using the same
estimation routine as dispatch. Telemetry and state are not modified, records
that still have no catalog entry stay `unresolved`, and the output names the
catalog version used. Without the flag, stored values are reported unchanged.

#### Usage source provenance

Each telemetry record's `usage.usage_source` field names where its token
counts and model identity came from, in deterministic precedence order:

| Source | Meaning |
|---|---|
| `worker_json` | Parsed from the worker's one-line JSON result — highest precedence |
| `claude_result_json` | Extracted from a Claude Code result envelope (`--output-format json`/`stream-json`) when worker JSON had no usable usage or model fields |
| `log_metadata` | Recovered by scanning the raw stage log for recognizable token/model fields, when neither of the above applied |
| `opencode_plugin` | Read from the OpenCode usage-emitter plugin sidecar, used only when no higher-precedence source provided usage |

A record with no usable source anywhere in the chain reports usage as
unavailable rather than guessing.

Each telemetry record's `model.attribution` field says whether its model
identity was observed at runtime or only configured:

- `"observed"` — extracted from worker output, a result envelope, stage log
  metadata, an `--model`/`--agent` in the worker invocation, or the OpenCode
  sidecar.
- `"configured"` — supplied only by the dsh configuration fallback: dsh
  invokes carry no model flag, so the resolved role model is attributed as
  configured rather than pretending it was observed.
- `null` — no model identity was available anywhere in the chain.

The dsh adapter exposes no usage through headless output, so dsh telemetry
records have unavailable usage and unresolved cost until a session-usage
integration lands; do not treat their cost columns as measured spend.

### Dashboard

```bash
# Generate a static HTML dashboard
opsx-plan dashboard plan.toml

# Custom output path
opsx-plan dashboard plan.toml --output .opsx-plan/dashboards/my-plan.html

# Filter by change or run
opsx-plan dashboard plan.toml --change add-feature-a
opsx-plan dashboard plan.toml --run-id <run-id>

# Target a single-change run's derived manifest
opsx-plan dashboard --for-change add-feature-a

# Recompute costs from stored usage against the current pricing catalog
opsx-plan dashboard plan.toml --reprice
```

The dashboard is a self-contained HTML file with no external dependencies. It
includes seven sections: plan summary header, model leaderboard, per-change
table, failure breakdown, cost breakdown bar chart, rounds histogram, and stage
timeline. `--reprice` recomputes costs the same way as
`opsx-plan report --reprice` and adds a notice naming the catalog version used;
telemetry and state are never modified.

When the plan has a registered supervised job, the dashboard appends a
supervision section: job state and progress, open waits, recent incidents,
budget posture and limits, steering request acknowledgements, and the
cost-per-correct-completion definition. The section is rendered read-only from
the ledger, and a plan with no registered job renders the same seven sections
byte-for-byte as before.

---

## Notifications

`opsx-plan` supports operator-configurable run-event notifications via the
`[plan].notify_cmd` config key.

### Configuring notifications

```toml
[plan]
notify_cmd = "/path/to/notify-script.sh"
```

When `notify_cmd` is set, `opsx-plan` invokes it as a subprocess with a JSON
payload containing the event type, plan name, timestamp, summary, and (for
change-specific events) the change id.

**Default: disabled** (`notify_cmd = ""`). Without this key, notification
behavior is a no-op and the orchestrator runs identically to versions that
pre-date run-event notifications.

### Notification payload schema

```json
{
  "event_type": "change_done",
  "plan_name": "my-plan",
  "timestamp": "2026-07-11T18:00:00+00:00",
  "summary": "change add-feature-a completed",
  "change_id": "add-feature-a"
}
```

For plan-wide events (e.g. `plan_complete`), `change_id` is omitted.

### Supported event types

| Event | Scope | Trigger |
|---|---|---|
| `awaiting_approval` | change | A `pause_before` change becomes ready for approval |
| `awaiting_acceptance` | change | An orchestrator-created change waits for operator review |
| `change_done` | change | After verified archive + fast checks pass |
| `change_failed` | change | Any failure reason (blocked, timeout, max rounds, etc.) |
| `plan_complete` | plan | All enabled changes are done |
| `pull_request_opened` | plan | PR delivery succeeded (includes PR URL) |

### Notification failure isolation

The notification command is invoked as a **best-effort side effect** with a
30-second timeout. Notification failures — non-zero exit, timeout, command not
found — are logged for operator triage but **never** change stage verdicts,
plan-state transitions, or overall run exit semantics.

---

## Git Delivery

When `[plan.git_delivery].enabled = true`, `opsx-plan` manages a delivery
branch for the plan run. On the first run, it creates the branch from the
configured `base_ref` (or current HEAD). On subsequent runs, it verifies that
HEAD is on the recorded branch. After all changes complete, it can push the
branch and open a GitHub pull request.

### Configuration

```toml
[plan]
name = "my-plan"

[plan.git_delivery]
enabled = true
branch = "opsx/my-feature"       # optional; defaults to "opsx/<plan-name>"
base_ref = "main"                # optional; defaults to current branch
create_pull_request = true       # optional; requires gh on PATH + a git remote
```

### Default-off behavior

All git delivery features are disabled by default:
- `enabled` defaults to `false` — no branch creation or verification occurs.
- `create_pull_request` defaults to `false` — no PR creation occurs even when
  `enabled = true`.

### Fail-closed guards

| Guard | Behavior |
|---|---|
| **Clean tracked tree** | Branch creation refuses to proceed if the tracked tree is dirty. Commit or stash changes first. |
| **Wrong-branch resume refusal** | If a recorded delivery branch exists and HEAD is on a different branch, the run fails with a message to checkout the recorded branch. |
| **PR preflight failure** | If `create_pull_request = true` and `gh` is not on PATH or no git remote is configured, the run fails before any stage dispatch. |
| **Push failure** | If `git push` fails, the orchestrator reports the error and does **not** record a successful PR delivery. |
| **PR creation failure** | If `gh pr create` fails (after a successful push), the orchestrator reports the error and leaves the state unambiguous for operator inspection. |
| **Stale pointer to missing plan** | The active-plan pointer referencing a deleted TOML file fails commands with a clear message. |

### One-run overrides

| Flag | Effect |
|---|---|
| `--no-branch` | Skip delivery branch creation on the **first run only**. If a branch is already recorded in state, `--no-branch` is **rejected** with an error — you cannot suppress delivery after the branch has been created. |
| `--no-pr` | Skip the PR-delivery preflight check and skip completion-time PR creation for this invocation only. The delivery branch is still managed normally. |

### Delivery completion

After all enabled changes reach `done` status:
1. If `create_pull_request = true` and no `--no-pr` override:
   - The orchestrator pushes the recorded delivery branch.
   - Generates a PR body from plan report evidence (per-change status, rounds, durations, costs).
   - Creates a GitHub PR via `gh pr create`.
   - Records the PR URL in plan state (`git_delivery.pull_request_url`).
2. PR creation is idempotent: if a `pull_request_url` is already recorded, the
   orchestrator logs a skip message and does not create a duplicate.

### GH CLI requirement

`create_pull_request = true` requires the GitHub CLI (`gh`) on PATH and at
least one git remote configured. The run fails before any stage dispatch if
these prerequisites are not met. These prerequisites are also checked by
`opsx-plan doctor`.

---

## Single-Change Execution: `opsx-run`

For a single already-authored OpenSpec change, use `opsx-run` to skip the plan
manifest entirely:

```bash
opsx-run add-gardening-suggestions
opsx-run add-gardening-suggestions --budget-usd 2.00
```

`opsx-run` synthesizes a one-change OpenCode configuration with the same
defaults as plan-level execution (`max_rounds=5`, `no_progress_limit=2`,
`require_clean_tracked=true`) and runs the direct implement-review-archive loop.
The change must already exist at `openspec/changes/<change-id>/` with
`proposal.md` and `tasks.md` — `opsx-run` does not create changes.

Durable state is persisted to `.opsx-plan/run-<change-id>.state.json`, and
stage logs go to `.opsx-plan/logs/`. Interrupted runs can be resumed by
re-invoking the same `opsx-run <change-id>` command.

This is equivalent to `opsx-plan run-one <change-id>`.

### Derived Manifest

Every `opsx-run` invocation produces a **derived manifest** at
`.opsx-plan/plans/run-<change-id>.toml`. The manifest is written only after the
`require_clean_tracked` guard (and any other run-time refusal check) passes —
a rejected run leaves no manifest behind. The manifest is a fully
round-tripped TOML document that mirrors the one-change configuration the
orchestrator uses internally: every plan-level field and the single
`[[changes]]` entry are serialized, written to a temp file, loaded back through
the standard `load_plan` parser, and compared field-by-field before it replaces
any existing copy. If the round-trip comparison detects divergence, the stale
manifest and the temp file are both removed and a `PlanError` is raised to
prevent an incorrect manifest from persisting.

The derived manifest enables the same reporting and dashboard tooling that
multi-change plan manifests support. Use the bare change id (no `run-` prefix)
with `--for-change`:

```bash
# Report targeting a derived manifest (manifest-driven lookup):
opsx-plan report --for-change add-gardening-suggestions

# Dashboard targeting the same derived manifest:
opsx-plan dashboard --for-change add-gardening-suggestions

# When the manifest is absent but state exists (e.g. from a pre-change
# run namespace that predates manifest serialization), --for-change still
# resolves via the plan name fallback:
opsx-plan report --for-change add-adapter-aware-plan-compilation
```

The derived manifest path follows the convention
`.opsx-plan/plans/run-<change-id>.toml`. It is re-created (not appended to) on
every `opsx-run` invocation, so the manifest always reflects the configuration
that the most recent run used. The active-plan pointer (`.opsx-plan/active-plan`)
is **not** updated by `opsx-run` — it remains unchanged regardless of how many
single-change runs are dispatched.

---

## State and Recovery

### State location

All orchestrator state lives at `.opsx-plan/` in the host project root:
- `<name>.state.json` — plan-level state: approvals, per-change records, git
  delivery state, notified events
- `active-plan` — the active-plan pointer file
- `plans/` — derived single-change manifests (`run-<change-id>.toml`) and
  other .opsx-plan artifacts
- `logs/` — per-stage log files
- `workers/<plan>/<change>.json` — worker-compatible state snapshots used as
  phase inputs
- `telemetry/<plan>.jsonl` — telemetry records (JSON Lines)
- `usage/<plan>/<change>/` — OpenCode plugin usage sidecar files
- `dashboards/` — generated HTML dashboard files

Add `.opsx-plan/` to the host project's `.gitignore`. The orchestrator creates
a `.gitignore` in `.opsx-plan/` containing `*` to prevent accidental commits.

### Recovery after interruption

The orchestrator is designed for safe interruption:
- **Ctrl-C**: Sends SIGTERM to the active worker process group (15s grace,
  then SIGKILL). Persists state and exits.
- **Kill / crash**: On the next `opsx-plan run`, the `reconcile` step recovers
  a `running` status to `pending`.
- **Resume**: Re-run the same `opsx-plan run` command. The orchestrator resumes
  from the persisted phase, round, and fix prompt.

### Retention and cleanup

- Log files and telemetry JSONL append indefinitely. Prune `.opsx-plan/logs/`
  and `.opsx-plan/telemetry/` periodically if disk is a concern.
- The plan state file (`<name>.state.json`) is essential for resumption. Do not
  delete it while a plan is in progress.

---

## End-to-End Worked Example

This example walks through the full lifecycle: compile a plan, activate it,
preflight, run with budgets, approve a manual gate, recover from a failure,
monitor progress, and complete with a pull request.

### Setup

```bash
# Prerequisites: OpenSpec CLI, OpenCode, gh (for PR), and the opsx-controller
# adapter installed. Model roles are resolved from
# ~/.config/opsx-controller/models.toml (opsx-plan models init to seed it).

cd /path/to/host-project
git rev-parse HEAD > .baseline-commit   # record baseline for clean re-runs
```

### 1. Compile the plan

```bash
# Confirm the controller model resolves for the opencode adapter (required for compile)
opsx-plan models show --adapter opencode

# Compile a markdown plan into a runnable TOML manifest
opsx-plan compile docs/my-hardening-plan.md -o plan.toml
# Compiled: plan.toml
#   Changes: 5
#   Phases:  1, 2, 3
#   Gates:   1 change(s) with pause_before
#   Review the DAG with: opsx-plan status plan.toml
```

The compile auto-activates the output plan (when inside the repository).

### 2. Inspect the DAG

```bash
opsx-plan status
# plan: my-hardening-plan  (active: plan.toml)
#   P1 add-unit-tests                pending
#   P2 add-integration-coverage      pending
#   P3 add-security-hardening        pending
#   P3 fix-race-condition            pending
#   P4 add-logging-audit             pending
```

### 3. Run preflight checks

```bash
opsx-plan doctor
#   ✓ Installed orchestrator matches repo copy
#   ✓ Model roles resolve for the target adapter
#       controller   github-copilot/gpt-5.4      [user-global config (~/.config/opsx-controller/models.toml)]
#       implementer  deepseek/deepseek-v4-pro     [user-global config (~/.config/opsx-controller/models.toml)]
#       reviewer     github-copilot/gpt-5.4      [user-global config (~/.config/opsx-controller/models.toml)]
#       archiver     github-copilot/gpt-5.4      [user-global config (~/.config/opsx-controller/models.toml)]
#   ✓ Resolved model identifiers match adapter syntax
#   ✓ openspec available (repo or global)
#   ✓ opencode on PATH
#   ✓ No tracked __pycache__ or .pyc files
#   ✗ Tracked tree is clean
#     → Tracked files have uncommitted modifications; commit or stash before running unattended work
#   ✓ Plan loads successfully
#   ✓ PR delivery prerequisites (gh + git remote)

# Fix the dirty tree, then re-check
git stash
opsx-plan doctor    # all green now
```

### 4. Dry-run

```bash
opsx-plan run --dry-run
# Prints each change with its computed status and dependency edges.
# No stages are dispatched.
```

### 5. Run with budgets

```bash
# Run with a 30-minute time budget and $3.00 spend budget
opsx-plan run --budget-minutes 30 --budget-usd 3.00
```

Output during the run shows per-change dispatch, stage outcomes, and budget
checks:

```
[opsx-plan 14:30:00] === add-unit-tests direct opencode execution (round 1) ===
[opsx-plan 14:30:01]   exec[implement]: opencode run --agent opsx-implementer <input> (timeout 90m, log .opsx-plan/logs/...)
[opsx-plan 14:31:00]   done: add-unit-tests
...
```

The run-log line names whichever adapter is configured — e.g. `direct
claude-code execution` for a Claude Code plan.

### 6. Handle a manual gate

Suppose `add-security-hardening` has `pause_before = true`:

```bash
opsx-plan status
#   P1 add-unit-tests                done
#   P2 add-integration-coverage      running
#   P3 add-security-hardening        awaiting_approval
#     → opsx-plan approve add-security-hardening
#   P3 fix-race-condition            blocked

# Approve it
opsx-plan approve add-security-hardening

# Or approve all awaiting at once
opsx-plan approve --all
```

### 7. Recover from a failure

Suppose `fix-race-condition` hits the `no_progress_limit`:

```bash
opsx-plan status
#   P3 fix-race-condition            failed (no progress ceiling reached)
#     → opsx-plan reset fix-race-condition

# Investigate the logs
opsx-plan logs --change fix-race-condition

# Reset and re-run
opsx-plan reset fix-race-condition
opsx-plan run
```

### 8. Monitor with logs and report

```bash
# Follow an in-progress stage
opsx-plan logs --follow

# Review plan-level metrics
opsx-plan report plan.toml

# Focus on one change
opsx-plan report plan.toml --change add-unit-tests

# Generate a dashboard for sharing
opsx-plan dashboard plan.toml --output .opsx-plan/dashboards/hardening.html
```

### 9. PR delivery (if configured)

With this in `plan.toml`:

```toml
[plan.git_delivery]
enabled = true
branch = "opsx/hardening"
base_ref = "main"
create_pull_request = true
```

After all five changes complete:

```
[opsx-plan 15:45:00] git delivery: creating branch 'opsx/hardening' from 'main'
[opsx-plan 15:45:01] git delivery: branch 'opsx/hardening' ready (base: main)
... (changes run) ...
[opsx-plan 16:20:00] git delivery: pushing branch 'opsx/hardening' to remote 'origin'
[opsx-plan 16:20:05] git delivery: pushed 'opsx/hardening' successfully
[opsx-plan 16:20:05] git delivery: creating PR 'opsx-plan: my-hardening-plan' from 'opsx/hardening' to 'main'
[opsx-plan 16:20:08] git delivery: PR created: https://github.com/org/repo/pull/42
[opsx-plan 16:20:08] git delivery: delivery complete, PR opened at https://github.com/org/repo/pull/42
```

### 10. Suppressing delivery for one run

```bash
# Skip branch creation (first run only)
opsx-plan run --no-branch

# Skip PR delivery for this invocation
opsx-plan run --no-pr
```

---

## Command Reference

### `opsx-plan use`

```
opsx-plan use <plan.toml>
```
Activate a plan for subsequent commands. The plan path must be inside the
repository. The plan is validated through `load_plan()` before the pointer is
written.

### `opsx-plan compile`

```
opsx-plan compile <source.md> [-o <output.toml>] [--force] [--adapter <name>]
```
Compile a markdown plan into a runnable TOML manifest. Requires a
`controller` model resolved for the selected adapter (default `opencode`,
`--adapter claude-code` for Claude Code). Refuses to overwrite
an existing output unless `--force` is passed. When `-o` is omitted,
defaults to `openspec/plans/<source-stem>.toml`.

OpenCode compilation appends `opencode run --variant <variant>` when the
controller role resolves a reasoning variant (`controller_variant` key or
`OPSX_CONTROLLER_VARIANT`); when no variant resolves the flag is omitted
entirely. Claude Code compilation ignores the controller variant, since the
Claude CLI has no reasoning-variant flag.

### `opsx-plan models`

```
opsx-plan models show [--adapter <name>]
opsx-plan models env [--adapter <name>]
opsx-plan models init [--force]
```
Inspect and seed per-adapter model configuration. `show` prints each role's
resolved model and source, plus any identifier-syntax warnings. `env` prints
shell `export` statements for the four resolved variables and exits non-zero
if any role is unresolved. `init` seeds
`~/.config/opsx-controller/models.toml` from the current environment,
refusing to overwrite an existing file without `--force`. `--adapter`
defaults to the active plan's adapter when omitted.

### `opsx-plan run`

```
opsx-plan run [plan.toml] [--dry-run] [--only <id>...] [--max-changes N]
              [--budget-minutes N] [--budget-usd N] [--create-only]
              [--no-branch] [--no-pr]
```
Run the plan. All flags are optional. The plan argument is optional when an
active plan is set.

### `opsx-plan status`

```
opsx-plan status [plan.toml]
```
Reconcile state against the repository and print per-change status.

### `opsx-plan supervise`

```
opsx-plan supervise status [--json]
opsx-plan supervise probe
opsx-plan supervise serve [plan.toml] [--store PATH] [--job-id N] [--once]
opsx-plan supervise watchdog [plan.toml] [--store PATH] [--job-id N]
                              [--once] [--interval SECONDS] [--json]
```

Inspect and gate the operator authority boundary. `supervise status` is a
read-only capability report: it prints the detected backend (`available`,
`unprovisioned`, or `unsupported`), the three principals and their uids, the
authority-store file location, and any reasons. It never creates accounts,
installs units, writes the authority store, or changes host configuration, and
it exits 0 in all cases — including `unsupported` — because it only reports.
When the status is `unprovisioned`, the report points at the manual
provisioning step.

The authority store is an explicit service-owned regular file. Its default is
derived from the **service** principal's home (`<service-home>/.local/share/
opsx-controller/supervisor/supervisor.sqlite3`) or, before that principal is
provisioned, from the root-owned `/var/lib/opsx-controller/supervisor/
supervisor.sqlite3`; it never follows the invoking user's home. Override it
with `OPSX_SUPERVISOR_STATE_FILE`. The target is canonicalized (symlinks and
`..` resolved) before validation, and detection requires the file to exist, be
owned by the service principal, carry a mode that denies the worker principal
a write, and sit in a parent chain owned by the root trust root or the service
principal and not writable by the worker. Denial is evaluated against the
POSIX ACL as well as the mode bits, so a named ACL grant to the worker is
caught and an unreadable ACL fails closed. A missing file, a directory target,
wrong ownership, a worker-writable mode, an ACL-granted worker write, or a
worker-writable/non-service ancestor is reported `unprovisioned` with the
failing condition named.

`supervise probe` runs the fail-closed enablement gate. On a host without a
supported backend it exits non-zero naming the `UnsupportedHostError`; on an
available host it runs the mandatory activation probe — a real
worker-principal process proves its effective uid and its attempt to open the
store file for writing is denied with `EACCES`, and the reported execution
evidence must agree with the exit status and a fresh per-invocation nonce —
and exits non-zero naming the `ActivationProbeError` when the boundary does
not hold or cannot be proven. The restricted-spawn launcher is authenticated
from trusted system directories with owner/mode/ACL checks (never the ambient
`PATH`, never an untrusted helper), and its path is canonicalized before it is
both validated and executed, so only the verified real file is run; the probe
child runs in Python isolated mode with a scrubbed environment, so
`PYTHONPATH`, `sitecustomize`, and shell startup hooks cannot forge the
evidence. There is deliberately no no-probe route: every available path runs
the real probe.
Neither subcommand provisions anything, and there is no silent downgrade to a
weaker isolation posture: provisioning accounts and the service is a manual
operator step. See `core/plan-supervision.md` ("Operator authority boundary")
for the trust model and provisioning guidance.

`supervise serve` is the trusted service-side endpoint host, and the **single
production call site** that installs and retains the broker projection writer.
Before it accepts any operator or worker endpoint request it opens the
service-owned ledger, identifies the worktree's active nonterminal registration,
and installs the writer through which every committed receipt transaction
regenerates the JSON projection (approvals, acceptance flags, change records)
from broker and ledger state. Every session is bound to that registration: an
explicit `--job-id` is accepted only when it *is* the worktree's active job, so
a foreign (another worktree's) or terminal job id is refused as a mismatch
before the writer is installed. It then binds the operator and worker-actions
Unix sockets with allow-lists derived from the OS authority layer (never a
worker-selectable name), owner-only modes, and dispatches authenticated requests
until stopped; `--once` serves at most one request and exits, which is useful for
smoke checks. A missing store, an unregistered worktree, an unprovisioned
operator/service principal, or an unbindable socket fails closed with
`BrokerUnavailableError` before any socket is bound, tearing the partially
booted session down rather than leaking a raw socket error, so the service can
never record a durable receipt whose projection it cannot regenerate.

`supervise serve` also owns the watchdog loop: it performs the boot-scan
reconciliation for the registered non-terminal jobs before serving and runs a
periodic watchdog tick from its serve loop. The boot scan attempts to
reconnect and adopt an existing session before any respawn, and only a
`dead`, quiesced job that already had a prior execution is reconstituted, under
the bounded restart backoff.

`supervise watchdog` runs that same deterministic runner directly, without the
execution lock and without a live service. Every tick evaluates **all**
registered non-terminal jobs; with `--once` it runs exactly one tick and exits,
and without it, it ticks every `--interval` seconds until interrupted. An
explicit `--job-id` does not narrow the tick: it only selects which job's report
is shown at the top level, while `--json` still lists every assessed job under
`jobs`/`assessments`. It reports each job's classification (`live`, `quiet`,
`stalled`, `dead`, or `expected_human_wait`), the three separate liveness,
progress, and deadline signals, the restart-attempt state, and the recent
reconstitution events, in human-readable form or as `--json`. A prior owner
whose fencing record no longer holds the lock but whose recorded identity still
matches a live process is surfaced as a blocking hazard and is never
reconstituted. It exits non-zero with the named unknown-job error when the
worktree has no registered supervised job, and it never mutates anything when
read-only observation is requested through `status`/`inspect`/`report`. An
expected human wait is reported as `expected_human_wait` and receives no model,
recovery, or reconstitution action. See `core/plan-supervision.md` ("Watchdog
and reconstitution") for the signal, classification, quiescence, and
bounded-restart contracts.

`doctor`, `status`, `logs`, and `report` acquire no boundary dependency and
keep working unchanged on hosts where the boundary is unavailable.

#### Hermetic supervised verification

Automated verification of the supervision stack runs **hermetically**: the
fault-injection suite (`tests/supervisor/test_supervision_faults.py`) uses only
local loopback fake model servers, real local subprocesses, and temporary
sandboxes. It opens no external network connection, makes no paid model call,
and performs no global install or daemon provisioning. A `hermetic_supervision`
guard enforces this and fails closed — a non-loopback connect, a real provider
credential, a non-fake model identifier, or an operator installer command fails
the check instead of being silently allowed — and the same guard is installed
inside every helper subprocess the suite spawns, including the real controller
and `reset` commands, each of which announces its guarded pid so the harness can
assert the guard was live before its target ran. The suite kills every real
participant — the fake worker at the journaled intent/dispatch/result/
verification boundary, the supervised service inside its production receipt
transaction, and the real controller in its mediated request path — with
`Popen.kill()` plus a bounded wait, then restarts a fresh service against the
same store to observe recovery. Supervised verification is
therefore free and offline: it needs no credentials, no provisioned host, and
no network. See `core/plan-supervision.md` ("Fault-injection and test policy")
for the matrix and the checkpoint kill-and-recover evidence rule.

#### Service packaging and activation

Every global install also deploys the supervision service packaging — the
versioned systemd user unit template (`systemd/opsx-supervise.service.in`) and
the provisioning document (`docs/opsx-supervision-service.md`) — into the
installed runtime tree (`~/.local/lib/opsx-controller/systemd/` and
`.../docs/`). The deployment is **disabled by default**: the installer copies
data only, never writes a unit into a service-manager directory, never enables
or starts the service, and never creates or modifies an OS account or the
authority store.

Enabling unattended operation is a **separate, deliberate operator action**, not
an install side effect. Follow `docs/opsx-supervision-service.md`: create the
service and worker accounts, provision the authority-store file, enable the
service principal's user manager (`loginctl enable-linger`), render and reload
the template **from that manager**, run `opsx-plan supervise probe` (the
mandatory activation probe), and only then `systemctl --user enable --now
opsx-supervise.service` as the service principal. The unit pins its effective
identity with `AssertUser=`: a user unit runs under whoever's manager loads it,
so enabling it under any other user fails closed. A host whose probe fails or
cannot run is not enabled; an unsupported host (non-Linux, no peer-credential
backend, no distinct principals, no trusted store, no systemd user manager, or
no OpenCode session bridge) fails closed with the named `UnsupportedHostError`
and no weaker posture is substituted.

A repeated install refreshes the template and document but never activates the
service. `opsx-plan doctor` reports the packaged service state read-only —
installed template/document, any rendered unit, the supervisor ledger schema
version, the backend capability status, and the service-host prerequisites —
and stays green for an operator who has not enabled supervision.

### Lifecycle commands

```
opsx-plan supervise register [plan.toml] [--store PATH] [--budget-usd N]
                             [--budget-minutes N] [--per-action-usd N]
                             [--per-action-minutes N] [--deadline-minutes N]
                             [--max-incident-attempts N] [--primary-session]
opsx-plan supervise start    [plan.toml] [--store PATH] [--job-id N] [--no-drive]
opsx-plan supervise inspect  [plan.toml] [--store PATH] [--job-id N] [--json]
opsx-plan supervise resume   [plan.toml] [--store PATH] [--job-id N] [--no-drive]
opsx-plan supervise pause    [plan.toml] [--store PATH] [--job-id N]
opsx-plan supervise drain    [plan.toml] [--store PATH] [--job-id N]
opsx-plan supervise cancel   [plan.toml] [--store PATH] [--job-id N]
opsx-plan supervise watchdog [plan.toml] [--store PATH] [--job-id N]
                             [--once] [--interval SECONDS] [--json]
```

The job state machine is `registered → active → (paused → active)* →
completed | failed | cancelled`, with the last three terminal. Every command
except `inspect` is a durable ledger transition; a refused transition records
nothing.

**`register`** records the complete job: repository root and worktree identity,
the protected manifest snapshot captured from the plan's canonical manifest
(with its hash derived from that content), the standing permissions, the frozen
model selection and inexpensive allowlist, the budgets and deadlines at
operator revision 1, and the primary-session linkage configuration. It runs as
the trust root (the endpoint host is job-scoped and cannot exist before a job
does) and fails closed with `UnsupportedHostError` on a host without a
supported isolation backend, recording nothing. A second active job for the
same worktree is refused with `DuplicateJobError`. Nothing is written to the
worktree or to JSON execution state.

**`start`** transitions `registered → active`, records the live
service-owned execution fence, and drives the existing run engine — the same
`opsx-plan run` loop, no new DAG. `--no-drive` performs only the transition.

**`resume`** transitions `paused → active` only after resume revalidation
confirms that every relied-upon approval and acceptance receipt still matches
the current material revision. A receipt invalidated by an explicit policy or
plan revision re-arms its gate and the command fails with `StaleMaterialError`
(the job stays `paused` and the affected change returns to awaiting its
approval authority). After the wait clears, the engine is driven as for
`start`.

**`pause`** and **`drain`** are the two stop boundaries. Both record a durable
stop request (a job-scoped receipt plus an open `stop` wait) that survives a
restart and wakes a waiting job through the normal receipt scan; neither
requires the worktree execution lock. `pause` interrupts in-flight actions and
marks them `uncertain` for reconciliation, then enters `paused`. `drain` forbids
new dispatch but lets in-flight actions reach a terminal outcome, entering
`paused` only afterward. A job that is still `active` under a drain hold refuses
dispatch with the `stop` gate until the in-flight work is terminal.

**`cancel`** records the `cancelled` terminal state. An action holding only an
intent is failed with a cancellation reason; a dispatched action whose outcome
cannot be confirmed is marked `uncertain`; an already-uncertain action is left
for evidence. Cancellation ends open waits and frees the worktree for a new
registration. A cancelled job refuses every later receipt, stop request, and
lifecycle verb with the named terminal-job error.

**`inspect`** is a read-only projection — state, recorded waits, policy
revision, budget posture, recent actions and incidents, and the pending
`(manual)` operator checklist. It acquires neither the execution lock nor a live
service, and fails with the named unknown-job error when no registered job
exists.

Mutating commands (`start`, `resume`, `pause`, `drain`, `cancel`) for a live job
are mediated through the operator OS-authenticated endpoint, so a
worker-domain process cannot invoke them. When no operator socket is
configured the command acts as the trust root directly against the
service-owned ledger (the non-live job path); a configured-but-unreachable
endpoint fails closed with `BrokerUnavailableError` rather than acting
unmediated. An unregistered worktree fails with `UnknownJobError`; an illegal
transition fails with `IllegalTransitionError`; a terminal job fails with
`TerminalJobError`; an unsupported host fails with `UnsupportedHostError`. All
exit non-zero.

A supervised job reaches `completed` only from plan, archive, and fast-check
evidence — never from a worker or primary claim. Pending `(manual)` tasks are
reported on the completion record and the `inspect` output as the operator
checklist, and never mark the job incomplete or failed. Legacy, unregistered
runs are unaffected.

### `opsx-plan doctor`

```
opsx-plan doctor [plan.toml] [--adapter <name>]
```
Run preflight checks without dispatching stages. Exits with status 1 on any
failed check, 0 if all pass. When a plan is resolved, its declared adapter is
authoritative and `--adapter` is ignored. When no plan is active, `--adapter`
selects the adapter to check model resolution and client PATH against (defaults
to `opencode`).

The checks include a read-only supervision service report: whether the service
unit template and provisioning document are installed, whether a rendered unit
is present, the supervisor ledger schema version (when a ledger is present), and
the isolation-backend capability status. It reports an absent or unsupported
service state plainly and never fails the run for an operator who has not
enabled supervision.

### `opsx-plan approve`

```
opsx-plan approve [plan.toml] <change-id> [<change-id>...]
opsx-plan approve --all
```
Approve `pause_before` changes. Accepts phase prefixes (e.g. `P3`), which
resolve against the protected manifest snapshot in a registered job. In a
registered supervised job this is broker mediated: an operator approval is
recorded as a durable receipt through the OS-authenticated operator path, a
delegated gate is refused (release it through the scoped service action), and a
worker-domain or unauthenticated attempt fails with `BrokerMediationError`. An
unreachable or substituted broker path fails closed with
`BrokerUnavailableError`. An unregistered legacy job behaves exactly as before.

### `opsx-plan accept`

```
opsx-plan accept [plan.toml] <change-id> [<change-id>...]
opsx-plan accept --all
```
Accept orchestrator-created changes for driving. Re-verifies created artifacts
before accepting. In a registered supervised job acceptance is broker mediated
(operator authority); the projected acceptance state follows the recorded
receipt.

### `opsx-plan reset`

```
opsx-plan reset [plan.toml] <change-id> [<change-id>...]
opsx-plan reset --failed
```
Reset failed changes to pending. Accepts phase prefixes. In a registered
supervised job a reset is a durable broker transaction through the operator
path; a worker-domain reset is refused with `BrokerMediationError` and resets
nothing. A broker reset never takes the worktree execution lock.

### `opsx-plan logs`

```
opsx-plan logs [plan.toml] [--change <id>] [--stage <stage>]
               [--list] [--follow]
```
Inspect stage logs. Resolves the active or explicit plan, surfaces the most
relevant log by default.

### `opsx-plan report`

```
opsx-plan report [plan.toml] [--json] [--change <id>]
                 [--run-id <id>] [--stage <stage>] [--model <substr>]
                 [--for-change <id>]
```
Emit plan-run efficiency metrics from telemetry and state. `--for-change`
targets a derived single-change run manifest instead of a plan path.

### `opsx-plan dashboard`

```
opsx-plan dashboard [plan.toml] [--output <path>]
                    [--run-id <id>] [--change <id>]
                    [--for-change <id>]
```
Generate a static HTML efficiency dashboard from telemetry. `--for-change`
targets a derived single-change run manifest instead of a plan path.

### `opsx-plan run-one`

```
opsx-plan run-one <change-id> [--budget-usd N]
```
Run a single authored OpenSpec change directly through the OpenCode
implement-review-archive loop.

`run-one` has no `--adapter` flag: `build_single_change_config` always builds
its config from `ADAPTER_DEFAULTS["opencode"]`, regardless of which adapter
is configured elsewhere. There is no way to run a single change directly
through the `claude-code` (or `codex-cli`) loop via `run-one` — use a
plan manifest with `adapter = "claude-code"` and `opsx-plan run` instead.
Adding an `--adapter` flag to `run-one` is tracked as follow-up work, not
implemented here.

### `opsx-plan archive-plan`

```
opsx-plan archive-plan <plan.toml>
```
Archive a plan manifest pair (`.toml` and sibling `.md`) into
`openspec/plans/archived/`. Uses `git mv` for tracked files; clears the
active-plan pointer when it referenced the archived plan. Does not create
a commit — you commit the move yourself after reviewing.

### `opsx-run` (executable-name alias)

```
opsx-run <change-id> [--repo <path>] [--budget-usd N]
```
Equivalent to `opsx-plan run-one`.

---

## Flags Reference

### `opsx-plan run` flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `--dry-run` | flag | `false` | Print planned order and status without dispatching |
| `--only <id>...` | list | none | Restrict to these change ids |
| `--max-changes N` | int | `0` (no limit) | Stop after N changes complete |
| `--budget-minutes N` | float | `0` (disabled) | Wall-clock time budget in minutes |
| `--budget-usd N` | float | `0` (disabled) | Cumulative spend budget in USD |
| `--create-only` | flag | `false` | Create + verify ready changes without driving |
| `--no-branch` | flag | `false` | Skip delivery branch creation (first run only; rejected if branch already recorded) |
| `--no-pr` | flag | `false` | Skip PR preflight + completion-time PR creation |

### `opsx-plan compile` flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `-o`, `--output` | string | `openspec/plans/<source-stem>.toml` | Output TOML path |
| `--force` | flag | `false` | Overwrite existing output |
| `--adapter` | string | `"opencode"` | Compile client adapter (`opencode` or `claude-code`) |

### `opsx-plan approve` / `accept` / `reset` flags

| Flag | Applies to | Description |
|---|---|---|
| `--all` | `approve` | Approve all changes awaiting approval |
| `--all` | `accept` | Accept all changes awaiting acceptance |
| `--failed` | `reset` | Reset all failed changes to pending |

### Broker refusal errors

| Error | Raised when |
|---|---|
| `BrokerMediationError` | A worker-domain or unauthenticated process attempts an approval-family or reset mutation, a dispatch outside the supervised execution, or a delegated/human-only gate through the wrong authority |
| `BrokerUnavailableError` | The job is registered but the broker path cannot be reached; the command fails closed rather than falling back to JSON authority |
| `StaleMaterialError` | A relied-upon receipt no longer matches the current material revision (an explicit plan/policy revision re-armed the gate), or a supervised dispatch is attempted with an unresolved gate |

### `opsx-run` flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `--repo <path>` | string | `.` | Host project root |
| `--budget-usd N` | float | `0` (disabled) | Spend budget in USD |

### Global flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `--repo <path>` | string | `.` | Host project root |
