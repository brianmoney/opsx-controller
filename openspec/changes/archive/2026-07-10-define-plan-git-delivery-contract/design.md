## Context

`opsx-plan` already owns plan-level sequencing, state, and archive verification. Git delivery belongs at the same boundary because branch choice, pull-request timing, and resume safety are properties of the whole plan run rather than any single OpenSpec change.

Today there is no durable plan-level git identity. A run can start on one branch, resume on another, and still dispatch workers unless the operator notices. That is the failure mode this contract is meant to close before implementation begins.

## Goals / Non-Goals

**Goals:**

- Define an opt-in plan configuration surface for branch delivery and pull-request delivery.
- Record enough durable state to prove which branch a plan run owns after first branch creation.
- Make branch creation and resume behavior fail closed when the tracked tree or current `HEAD` does not match the recorded contract.
- Preserve evidence-driven archive verification when delivery happens on a plan branch instead of the operator's ambient branch.
- Keep all branch mutation, pushing, and pull-request creation owned by the orchestrator rather than delegated to phase workers.

**Non-Goals:**

- Do not implement branch creation, checkout, push, or pull-request commands.
- Do not define per-change branches, stacked pull requests, merge automation, or remote-forge abstractions beyond GitHub CLI pull-request creation.
- Do not change implement, review, or archive worker semantics beyond forbidding git-delivery mutations by workers.

## Decisions

### 1. Use grouped `plan.git_delivery.*` configuration keys

The contract defines these plan configuration keys:

- `plan.git_delivery.enabled` (boolean, default `false`): turns plan-level branch delivery on.
- `plan.git_delivery.branch` (string, optional): explicit delivery branch name for the whole plan.
- `plan.git_delivery.base_ref` (string, optional): git ref from which the delivery branch is created and against which a pull request is targeted.
- `plan.git_delivery.create_pull_request` (boolean, default `false`): allows pull-request creation after successful plan completion.

When `enabled` is `false` or the `git_delivery` table is absent, the entire capability is off and the existing non-branching behavior remains unchanged.

**Rationale:** The keys are related and should be validated together rather than spread across unrelated top-level plan fields.

### 2. Persist the orchestrator's branch identity in durable state

The contract defines a `git_delivery` object in plan state with these fields:

- `git_delivery.base_ref` (string or `null`): the base ref actually selected for the run.
- `git_delivery.branch_name` (string or `null`): the delivery branch actually selected for the run.
- `git_delivery.delivery_status` (string): lifecycle status. Supported values are `"disabled"`, `"branch_pending"`, `"branch_ready"`, `"ready_for_pr"`, and `"pr_opened"`.

The persisted branch and base values are authoritative for resuming the same plan run after interruption.

**Rationale:** Resume safety depends on comparing current repository state against recorded run identity, not recomputing the branch contract from ambient git state alone.

### 3. Require a clean tracked tree before branch creation

The first run that would create or check out a delivery branch must refuse to proceed when tracked files are dirty.

The contract intentionally scopes this requirement to tracked-tree cleanliness because that matches the existing `require_clean_tracked` model and avoids inventing a second cleanliness concept here.

**Rationale:** Creating the delivery branch from a dirty tracked tree would make the branch base ambiguous and could mix unrelated operator changes into the plan-owned branch.

### 4. Guard every resumed dispatch with the recorded branch

Once state records `git_delivery.branch_name`, every later run must verify that the current symbolic `HEAD` matches that exact branch before dispatching implement, review, or archive.

If the current branch differs, the run fails closed and reports both the expected recorded branch and the actual branch or detached-`HEAD` state.

**Rationale:** This is the key protection against resuming a plan on the wrong branch and silently delivering archive commits somewhere unexpected.

### 5. Keep archive verification anchored to the delivery branch `HEAD`

Existing archive verification checks for an `archive(<change>):` commit reachable from `HEAD`. Under branch delivery, that requirement remains correct after the resume guard because `HEAD` is required to be the recorded delivery branch.

The contract does not require the archive commit to already be reachable from `git_delivery.base_ref` before the plan is complete.

**Rationale:** A plan-owned branch is supposed to diverge from its base until later delivery. Requiring immediate base-ref reachability would make branch delivery self-contradictory.

### 6. Pull-request creation is evidence-driven and orchestrator-only

When `plan.git_delivery.create_pull_request` is `true`, pull-request creation becomes eligible only after:

- branch delivery is enabled,
- all enabled changes in the plan are marked done,
- archive verification has succeeded for those changes, and
- configured fast checks are green for the completed plan state.

Implement, review, and archive workers must not create, checkout, push, or retarget git branches, and must not create pull requests. Any such delivery actions belong to the orchestrator.

**Rationale:** Delivery is a cross-change concern with plan-wide evidence gates. Workers operate on one phase of one change and should not own cross-run git side effects.

## Risks / Trade-offs

- [Risk] The chosen `delivery_status` vocabulary could prove slightly too coarse for later implementation details. -> Mitigation: keep the enum minimal but explicit around the branch-ready and PR-ready boundaries this contract needs.
- [Risk] Different adapters may expose branch-delivery controls differently. -> Mitigation: keep this spec at the orchestrator contract level and leave client-specific invocation surfaces to adapter changes.
- [Risk] Operators may expect pull requests to open as soon as the last archive finishes, even if fast checks fail afterward. -> Mitigation: make fast-check success an explicit prerequisite in the contract.

## Migration Plan

No migration is required for existing plans. Plans without `plan.git_delivery.enabled = true` continue to run without branch creation, branch resume guards, or pull-request delivery state.

When later implementation lands, old state files that lack a `git_delivery` object are treated as equivalent to `delivery_status = "disabled"` unless the active plan explicitly enables git delivery.
