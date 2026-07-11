# opsx-plan: Operator Workflow Guide

How to activate, run, and supervise an `opsx-plan` from plan creation through
pull-request delivery, using the upgraded command surface introduced by the
operator-workflow-upgrade series.

This document covers the full lifecycle: compile, activate, preflight with
`doctor`, run with budgets, manage gates, inspect logs, monitor with
`report`/`dashboard`, receive notifications, and finish on a delivery branch
that opens a pull request.

---

## Quick Start

```bash
# 1. Author a plan document (markdown), then compile to TOML
opsx-plan compile docs/my-plan.md -o plan.toml

# 2. Activate the plan (subsequent commands resolve it automatically)
opsx-plan use plan.toml

# 3. Run preflight checks
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

The `opsx-plan` command (and `opsx-run`) must be invoked from the host project
root. The orchestrator places all operational state under `.opsx-plan/`. Add
this directory to the host project's `.gitignore`.

---

## Plan Activation

`opsx-plan` resolves the plan source through a standard three-level precedence.
You do not need to repeat the plan path on every command.

### Resolution precedence

| Priority | Source | Example |
|---|---|---|
| 1 (highest) | Explicit CLI argument | `opsx-plan run plan.toml` |
| 2 | `OPSX_PLAN` environment variable | `export OPSX_PLAN=plan.toml` |
| 3 | Active-plan pointer file | Set by `opsx-plan use` or auto-set after `compile`/`run` with an explicit path |

When a plan path is provided explicitly (as a CLI argument), the command also
auto-sets the active-plan pointer so later commands resolve the same plan
without repeating the path.

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

## Preflight with `doctor`

`opsx-plan doctor` runs a suite of preflight checks without dispatching any
stages. It is safe to run at any time and never modifies state. Use it before
an unattended run to catch configuration problems early.

```bash
opsx-plan doctor
```

### Checks performed

| Check | What it validates |
|---|---|
| Installed orchestrator matches repo copy | SHA-256 comparison of `~/.local/bin/opsx-plan` against `orchestrator/opsx-plan.py` |
| Required `OPSX_*_MODEL` env vars | `OPSX_CONTROLLER_MODEL`, `OPSX_IMPLEMENTER_MODEL`, `OPSX_REVIEWER_MODEL`, `OPSX_ARCHIVER_MODEL` |
| `openspec` on PATH | OpenSpec CLI is installed and reachable |
| Adapter client on PATH | e.g. `opencode`, `claude`, or `codex` |
| No tracked bytecode | No `__pycache__/` or `.pyc` files tracked in git |
| Tracked tree is clean | No uncommitted modifications to tracked files |
| Plan loads successfully | The resolved plan TOML is valid and parses without errors |
| PR delivery prerequisites | When `create_pull_request = true`: `gh` on PATH and a git remote configured |

### Doctor failure behavior

A doctor check failure prints a cross-mark (✗) with a remediation hint. The
`doctor` command returns the count of failures as its exit code. Doctor
**never** blocks a `run` — the same checks run as warnings before each `run`
(visible as ⚠ lines) without changing the run outcome. A failure is a strong
signal to fix the issue before continuing, but the orchestrator does not refuse
to run.

---

## Plan Manifest

The plan manifest is a TOML file with a `[plan]` table and one or more
`[[changes]]` entries. An example is at `orchestrator/plan.example.toml`.

### `[plan]` table: key config keys and defaults

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string | filename stem | Plan display name |
| `adapter` | string | `"opencode"` | Client adapter: `opencode`, `claude-code`, or `codex-cli` |
| `timeout_minutes` | float | `90` | Per-change stage timeout |
| `max_attempts` | int | `2` | Legacy drive retry ceiling |
| `max_rounds` | int | `5` | Implement-review loop ceiling |
| `no_progress_limit` | int | `2` | Consecutive no-progress rounds before failing |
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
| `invoke` | string | adapter default | Legacy single-command controller invocation |
| `state_file` | string | adapter default | Controller state file path |
| `implement_invoke` | string | `opencode run --agent opsx-implementer` | Direct implement command (OpenCode) |
| `review_invoke` | string | `opencode run --agent opsx-reviewer` | Direct review command (OpenCode) |
| `archive_invoke` | string | `opencode run --agent opsx-archiver` | Direct archive command (OpenCode) |

### `[[changes]]` entry fields

| Key | Type | Default | Description |
|---|---|---|---|
| `id` | string | **required** | Unique change identifier (slug) |
| `phase` | int | none | Phase number for display ordering |
| `depends_on` | list\[str\] | `[]` | IDs of changes that must complete first |
| `pause_before` | bool | `false` | Wait for `opsx-plan approve` before running |
| `enabled` | bool | `true` | Set `false` to defer a change |
| `timeout_minutes` | float | plan-level timeout | Per-change stage timeout override |
| `max_attempts` | int | plan-level max_attempts | Legacy drive attempt override |
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

---

## Compiling a Plan

`opsx-plan compile` converts a markdown implementation plan into a runnable
TOML manifest. It invokes OpenCode with the model configured in
`OPSX_CONTROLLER_MODEL`.

```bash
# Required: set the controller model
export OPSX_CONTROLLER_MODEL="your-model-id"

# Compile a markdown plan to TOML
opsx-plan compile docs/my-plan.md -o plan.toml

# Overwrite an existing manifest
opsx-plan compile docs/my-plan.md -o plan.toml --force
```

### Compile behavior

- Refuses to overwrite an existing output file unless `--force` is passed.
- Fails before invoking OpenCode if `OPSX_CONTROLLER_MODEL` is unset or empty.
- The generated TOML is validated locally: it must parse as valid TOML, pass
  `load_plan()` (unique ids, known deps, no cycles), and is written through a
  temporary file with atomic replacement.
- On success, auto-activates the output plan so subsequent commands resolve it.

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
  recovered to `pending`; changes archived outside plan control are marked
  done; inconsistencies are surfaced.

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

```bash
# Approve a single change
opsx-plan approve add-new-capability

# Approve all changes currently awaiting approval
opsx-plan approve --all

# Approve by phase number (P prefix)
opsx-plan approve P3
```

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

---

## Monitoring

### Status

```bash
opsx-plan status           # resolve active plan
opsx-plan status plan.toml # inspect a specific plan
```

Status reconciles state against the repository and prints each change's
computed status with phase ordering. Blocked, awaiting-approval, and
awaiting-acceptance changes print guidance for the next operator command.

Output example:

```
plan: my-plan  (active: plan.toml)
  P1 add-feature-a          done
  P2 add-feature-b          awaiting_approval
    → opsx-plan approve add-feature-b
  P2 add-feature-c          failed (no progress ceiling reached)
    → opsx-plan reset add-feature-c
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
```

The report includes:
- **Plan Summary**: overall completion rate, duration, tokens, cost
- **Per-Change Metrics**: status, rounds, duration, tokens, cost per change
- **Stage Aggregates**: average durations, review failure rate, cost per change
- **Model Leaderboard**: grouped by `(implementer, reviewer, archiver)` tuple

### Dashboard

```bash
# Generate a static HTML dashboard
opsx-plan dashboard plan.toml

# Custom output path
opsx-plan dashboard plan.toml --output .opsx-plan/dashboards/my-plan.html

# Filter by change or run
opsx-plan dashboard plan.toml --change add-feature-a
opsx-plan dashboard plan.toml --run-id <run-id>
```

The dashboard is a self-contained HTML file with no external dependencies. It
includes seven sections: plan summary header, model leaderboard, per-change
table, failure breakdown, cost breakdown bar chart, rounds histogram, and stage
timeline.

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
| `create_started` | change | Before create stage starts |
| `awaiting_approval` | change | A `pause_before` change becomes ready for approval |
| `awaiting_acceptance` | change | An orchestrator-created change waits for operator review |
| `change_started` | change | Before implement stage starts |
| `change_done` | change | After verified archive + fast checks pass |
| `change_failed` | change | Any failure reason (blocked, timeout, max rounds, etc.) |
| `plan_complete` | plan | All enabled changes are done |
| `pull_request_opened` | plan | PR delivery succeeded (includes PR URL) |
| `plan_abandoned` | plan | Plan aborted early (e.g. budget exhausted or spawn error) |

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

---

## State and Recovery

### State location

All orchestrator state lives at `.opsx-plan/` in the host project root:
- `<name>.state.json` — plan-level state: approvals, per-change records, git
  delivery state, notified events
- `active-plan` — the active-plan pointer file
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
# adapter installed. The four OPSX_*_MODEL env vars are set in .env.

cd /path/to/host-project
git rev-parse HEAD > .baseline-commit   # record baseline for clean re-runs
```

### 1. Compile the plan

```bash
# Set the controller model (required for compile)
export OPSX_CONTROLLER_MODEL="anthropic/claude-sonnet-4-20250514"

# Compile a markdown plan into a runnable TOML manifest
opsx-plan compile docs/my-hardening-plan.md -o plan.toml
# Compiled: plan.toml
#   Changes: 5
#   Phases:  1, 2, 3
#   Gates:   1 change(s) with pause_before
#   Review the DAG with: opsx-plan status plan.toml
```

The compile auto-activates the output plan.

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
#   ✓ Required OPSX_*_MODEL environment variables
#   ✓ openspec on PATH
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
[opsx-plan 14:30:00] === add-unit-tests direct OpenCode execution (round 1) ===
[opsx-plan 14:30:01]   exec[implement]: opencode run --agent opsx-implementer <input> (timeout 90m, log .opsx-plan/logs/...)
[opsx-plan 14:31:00]   done: add-unit-tests
...
```

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
opsx-plan compile <source.md> -o <output.toml> [--force]
```
Compile a markdown plan into a runnable TOML manifest. Requires
`OPSX_CONTROLLER_MODEL`. Refuses to overwrite an existing output unless
`--force` is passed.

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

### `opsx-plan doctor`

```
opsx-plan doctor [plan.toml]
```
Run preflight checks without dispatching stages. Returns count of failures.

### `opsx-plan approve`

```
opsx-plan approve [plan.toml] <change-id> [<change-id>...]
opsx-plan approve --all
```
Approve `pause_before` changes. Accepts phase prefixes (e.g. `P3`).

### `opsx-plan accept`

```
opsx-plan accept [plan.toml] <change-id> [<change-id>...]
opsx-plan accept --all
```
Accept orchestrator-created changes for driving. Re-verifies created artifacts
before accepting.

### `opsx-plan reset`

```
opsx-plan reset [plan.toml] <change-id> [<change-id>...]
opsx-plan reset --failed
```
Reset failed changes to pending. Accepts phase prefixes.

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
```
Emit plan-run efficiency metrics from telemetry and state.

### `opsx-plan dashboard`

```
opsx-plan dashboard [plan.toml] [--output <path>]
                    [--run-id <id>] [--change <id>]
```
Generate a static HTML efficiency dashboard from telemetry.

### `opsx-plan run-one`

```
opsx-plan run-one <change-id> [--budget-usd N]
```
Run a single authored OpenSpec change directly through the OpenCode
implement-review-archive loop.

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
| `-o`, `--output` | string | **required** | Output TOML path |
| `--force` | flag | `false` | Overwrite existing output |

### `opsx-plan approve` / `accept` / `reset` flags

| Flag | Applies to | Description |
|---|---|---|
| `--all` | `approve` | Approve all changes awaiting approval |
| `--all` | `accept` | Accept all changes awaiting acceptance |
| `--failed` | `reset` | Reset all failed changes to pending |

### `opsx-run` flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `--repo <path>` | string | `.` | Host project root |
| `--budget-usd N` | float | `0` (disabled) | Spend budget in USD |

### Global flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `--repo <path>` | string | `.` | Host project root |
