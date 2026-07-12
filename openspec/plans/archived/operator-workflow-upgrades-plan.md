---
title: Operator Workflow Upgrades Plan
doc_type: implementation-plan
status: proposed
updated: 2026-07-08
---

# Operator Workflow Upgrades Plan

## Purpose

Remove the day-to-day friction of driving `opsx-plan`: every subcommand today
requires the plan TOML path, known environment gotchas (stale installed
copies, tracked bytecode files) surface only mid-run, gate approvals are
one-at-a-time, and finished plans deliver commits onto whatever branch happens
to be checked out with no pull-request handoff.

This plan makes the active plan implicit, adds a preflight `doctor`, gives
each plan run its own git branch with an optional pull request on completion,
and rounds out run controls with a cost budget, batched gate commands, a log
tail command, and run-event notifications. Orchestrator changes stay
deterministic: branching, PR creation, and budget enforcement are script
responsibilities, never delegated to phase workers.

## Capability Ownership

`plan-operator-cli` is a proposed capability for operator-facing CLI
ergonomics: active-plan resolution, environment preflight, gate batching, log
access, run budgets, and notifications. It is separate from
`plan-driven-opencode-execution` because it governs how operators address and
supervise plans, not the implement-review-archive control loop semantics.

`plan-git-delivery` is a proposed capability for plan-scoped git branch
lifecycle and pull-request delivery. It is separate from `plan-operator-cli`
because it changes where run artifacts land in git history and has its own
fail-closed rules around branch identity and resume safety.

## Phase 1: CLI Ergonomics Foundation

### Change: `add-active-plan-resolution`

**Purpose:** Let operators activate a plan once and omit the plan path from
every subsequent `opsx-plan` invocation.

**Depends on:** None. This may be developed independently because it only
changes CLI argument resolution.

**Capability:** `plan-operator-cli` (proposed; see Capability Ownership).

**Scope:** Store the active plan as a repo-relative TOML path in a pointer
file under `.opsx-plan/`. Add an `opsx-plan use <plan.toml>` command and show
the active plan in `status` output. Make the `plan` positional optional on
`run`, `status`, `approve`, `accept`, `reset`, `report`, and `dashboard`,
resolved in order: explicit argument, then an `OPSX_PLAN` environment
variable, then the pointer file, then a clear error naming the `use` command.
Auto-activate on successful `compile -o` and on any `run` invoked with an
explicit path. Fail closed with the recorded path when the pointer targets a
missing file. Add unit tests for each resolution branch and the staleness
error.

**Out of scope:** Auto-discovering plan TOMLs from the filesystem, multiple
simultaneously active plans, or any change to plan execution semantics.

**Success parameters:** All listed subcommands work without a plan argument
after `use` or `compile`; explicit arguments and `OPSX_PLAN` always override
the pointer; a stale pointer produces an actionable error, never a guess;
existing invocations with explicit paths behave unchanged.

### Change: `add-opsx-plan-doctor-command`

**Purpose:** Surface known environment failure modes before a run instead of
mid-run.

**Depends on:** `add-active-plan-resolution`.

**Capability:** `plan-operator-cli`.

**Scope:** Add an `opsx-plan doctor` command that checks: the installed
orchestrator copy matches the repo copy (content hash comparison against the
`~/.local/bin` install), required `OPSX_*_MODEL` environment variables are
set, `openspec` and the configured adapter client are on `PATH`, the tracked
worktree contains no `__pycache__` or `.pyc` entries, and the tracked tree is
clean. When an active or explicit plan is given, additionally validate the
plan loads and check plan-conditional requirements (for example `gh` and a
git remote when pull-request delivery is enabled). Emit one pass/fail line
per check with a remediation hint, and exit non-zero on any failure. Run the
same checks as non-fatal warnings at `run` start. Add tests with fabricated
environments for each failing check.

**Out of scope:** Auto-fixing failures, running installers, network
connectivity probes, or validating model identifiers against providers.

**Success parameters:** Each known gotcha (stale install, tracked bytecode,
missing env vars, missing CLIs) is detected with a specific remediation
message; `doctor` works with no plan argument by using the active plan when
present; `run` startup warnings never change run outcomes.

## Phase 2: Git Delivery

### Change: `define-plan-git-delivery-contract`

**Purpose:** Specify branch lifecycle, durable state fields, resume guards,
and pull-request triggers before any git-mutating runtime code is written.

**Depends on:** None. This may be developed independently because it is a
specification baseline.

**Capability:** `plan-git-delivery` (proposed; see Capability Ownership).

**Scope:** Add OpenSpec requirements covering: plan-level configuration keys
for enabling branch delivery, branch naming, pull-request creation, and base
ref; the state fields recording base ref, branch name, and delivery status;
the rule that branch creation requires a clean tracked tree; the fail-closed
resume guard requiring `HEAD` to be on the recorded branch before any stage
dispatch; interaction with archive-commit reachability verification; and the
completion condition under which a pull request is created. Document that
branching and PR creation are orchestrator responsibilities and remain
forbidden to phase workers.

**Out of scope:** Implementing runtime branch creation or PR commands,
per-change branches, stacked PRs, or merge automation.

**Success parameters:** OpenSpec validation passes; the contract names every
new config key and state field; resume-guard and fail-closed behaviors have
explicit scenarios; the contract states how existing non-branching plans
remain unaffected by default.

### Change: `create-plan-branch-on-run-start`

**Purpose:** Isolate each plan run's commits on a dedicated branch created
and guarded by the orchestrator.

**Depends on:** `define-plan-git-delivery-contract`.

**Capability:** `plan-git-delivery`.

**Scope:** When branch delivery is enabled in the plan TOML, create and check
out the configured branch (default derived from the plan name) from the
configured base ref at first run start, requiring a clean tracked tree, and
record base ref and branch in plan state. On every subsequent run, refuse to
dispatch any stage unless `HEAD` is on the recorded branch, with an error
naming the expected and actual branch. Support a `--no-branch` run flag for
one-off override and keep the feature fully off by default. Add tests for
first-run creation, resume on the correct branch, refusal on the wrong
branch, refusal on a dirty tree, and default-off behavior.

**Out of scope:** Pushing to remotes, creating pull requests, deleting or
rebasing branches, or per-change branching.

**Success parameters:** Plans without the config keys behave exactly as
today; a resumed run on the wrong branch fails closed before any worker
dispatch; archive-commit reachability verification still passes on the plan
branch; state records enough to reconstruct base and branch after
interruption.

### Change: `open-pr-on-plan-completion`

**Purpose:** Deliver a finished plan as a pull request with a body generated
from run evidence.

**Depends on:** `create-plan-branch-on-run-start`.

**Capability:** `plan-git-delivery`.

**Scope:** When pull-request delivery is enabled and every enabled change in
the plan is done with fast checks green, push the plan branch and create a
pull request against the configured base using the `gh` CLI. Generate the PR
body from existing report aggregation data: per-change status, rounds,
durations, and estimated cost when available. Check `gh` availability and
remote configuration at run start, not at completion, so long runs cannot
finish undeliverable. Record PR creation status and URL in plan state, make
PR creation idempotent across re-runs, and support a `--no-pr` flag. Add
tests for completion detection, preflight failure, idempotent re-run, and
body generation with and without telemetry.

**Out of scope:** Auto-merging, PR review automation, draft-PR progress
updates during the run, or supporting forges other than GitHub via `gh`.

**Success parameters:** A completed branch-delivery run produces exactly one
PR with an evidence-based body; missing `gh` or remote fails the run at
start with a clear message; re-running a completed plan does not open a
duplicate PR; plans without PR config never touch the network.

## Phase 3: Run Controls And Attention

### Change: `add-cost-budget-run-flag`

**Purpose:** Let operators cap unattended runs by estimated spend rather than
only wall-clock time.

**Depends on:** None. This may be developed independently because stage cost
estimation already exists in telemetry.

**Capability:** `plan-operator-cli`.

**Scope:** Add a `--budget-usd` flag to `run` that accumulates estimated
stage costs from the run's telemetry records and stops dispatching new
stages once the cap is reached, mirroring `--budget-minutes` stop semantics.
Treat stages with unresolved cost conservatively by tracking known-cost and
unknown-cost stage counts, reporting both at stop time. Never interrupt a
stage in flight. Add tests for cap reached, cap not reached, and runs with
mixed resolved and unresolved costs.

**Out of scope:** Provider billing reconciliation, hard-killing in-flight
workers, or per-change cost caps.

**Success parameters:** A run stops cleanly at the configured spend cap with
state resumable as after any budget stop; unresolved costs are reported, not
silently counted as zero; behavior without the flag is unchanged.

### Change: `batch-gate-and-reset-commands`

**Purpose:** Make human gates cheap to clear when supervising multi-change
plans.

**Depends on:** `add-active-plan-resolution`.

**Capability:** `plan-operator-cli`.

**Scope:** Add `accept --all` to accept every change currently awaiting
acceptance, `approve --all` for every change awaiting approval, and
`reset --failed` to reset every failed change to pending. Each batch command
prints exactly which changes it affected. Extend `status` so every blocked
change prints the exact next command to unblock it, using the active-plan
short form. Add tests for batch operations on empty, partial, and full gate
sets.

**Out of scope:** Auto-accepting without operator invocation, changing gate
semantics, or removing single-change forms.

**Success parameters:** One command clears each gate class; batch commands
are explicit about what they changed; `status` output gives copy-pasteable
next steps for every blocked change.

### Change: `add-plan-logs-command`

**Purpose:** Answer "what is it doing right now" without globbing
`.opsx-plan/logs/` by hand.

**Depends on:** `add-active-plan-resolution`.

**Capability:** `plan-operator-cli`.

**Scope:** Add an `opsx-plan logs` command that, for the active or explicit
plan, prints the most recent stage log path and its tail by default, supports
selecting a change id and stage, listing available logs, and a follow mode
for a run in progress. Resolve logs from recorded state metadata first and
fall back to log-directory ordering. Add tests for latest-log selection,
change and stage filters, and missing-log handling.

**Out of scope:** Log rotation, parsing, colorized rendering, or a
long-running monitoring UI.

**Success parameters:** During and after a run, one command reaches the
relevant log; filters select deterministically; missing logs produce a clear
message rather than an empty tail.

### Change: `add-run-event-notifications`

**Purpose:** Notify operators when a long run needs attention or finishes.

**Depends on:** `open-pr-on-plan-completion`.

**Capability:** `plan-operator-cli`.

**Scope:** Add an optional `notify_cmd` plan setting invoked with a single
JSON event argument on: change done, change failed, change awaiting
acceptance or approval, plan complete, and pull request opened. Include plan
name, change id when applicable, event type, timestamp, and a short summary
in the payload. Notification failures are logged and never affect run
outcomes. Add tests for event emission points, payload shape, and
failure-isolation.

**Out of scope:** Bundled integrations for specific services, retry queues
for failed notifications, or inbound remote control.

**Success parameters:** Each listed event invokes the hook exactly once with
a documented payload; a crashing hook cannot fail a stage or the run; runs
without `notify_cmd` are unaffected.

## Phase 4: Documentation

### Change: `document-operator-workflow-upgrades`

**Purpose:** Document the upgraded operator workflow end to end once command
names, config keys, and file locations are final.

**Depends on:** `add-opsx-plan-doctor-command`, `open-pr-on-plan-completion`,
`add-cost-budget-run-flag`, `batch-gate-and-reset-commands`,
`add-plan-logs-command`, and `add-run-event-notifications`.

**Capability:** `plan-operator-cli`.

**Scope:** Update the orchestrator README and root README with the activate-
then-run workflow, the doctor preflight, branch and PR delivery configuration
with its fail-closed rules, budget flags, gate batching, log access, and
notification hooks. Include a worked end-to-end example from `compile`
through PR creation and an explicit section on default-off behaviors and
overrides.

**Out of scope:** Adapter-specific tutorials for clients other than OpenCode
or marketing material.

**Success parameters:** A new operator can run a plan from activation to PR
using only the documentation; every new config key and flag is documented
with its default; fail-closed behaviors and their overrides are explicit.

## Recommended Sequence

1. Implement `add-active-plan-resolution` first; nearly every later
   operator-facing change reads plan identity through it.
2. Implement `add-opsx-plan-doctor-command` to de-risk unattended runs before
   git delivery lands.
3. Implement `define-plan-git-delivery-contract`, review it at a manual gate,
   then `create-plan-branch-on-run-start` and `open-pr-on-plan-completion` in
   order.
4. Implement `add-cost-budget-run-flag`, `batch-gate-and-reset-commands`, and
   `add-plan-logs-command` in any order; they are independent of git
   delivery.
5. Implement `add-run-event-notifications` after PR delivery so the event
   vocabulary is final.
6. Finish with `document-operator-workflow-upgrades`.

## Overall Completion Criteria

The series is complete when an operator can compile a plan, activate it, run
`doctor` cleanly, and drive the whole plan with bare `opsx-plan run` on a
dedicated branch that ends in a pull request with an evidence-based body —
with spend caps, one-command gate clearing, one-command log access, and
notifications at every attention point, and with every new behavior off by
default for existing plans.

## Explicit Non-Goals

This plan does not add parallel plan execution, per-change branches or
stacked pull requests, auto-merge, hosted notification services, provider
billing reconciliation, auto-fixing doctor findings, or any change to
implement-review-archive control loop semantics beyond branch guards around
stage dispatch.

## Suggested Manual Gates

Add `pause_before = true` to `add-active-plan-resolution` in the compiled
manifest because it introduces the proposed `plan-operator-cli` capability.
Add `pause_before = true` to `define-plan-git-delivery-contract` because it
introduces the proposed `plan-git-delivery` capability and fixes the
fail-closed git semantics everything in Phase 2 builds on. Consider a manual
gate before `open-pr-on-plan-completion` since it is the first change that
pushes to a remote.
