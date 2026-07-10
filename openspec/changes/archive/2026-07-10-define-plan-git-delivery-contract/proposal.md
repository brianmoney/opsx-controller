## Why

`opsx-plan` currently delivers completed work onto whatever branch happens to be checked out when the run executes. That is convenient for local experimentation, but it is not a safe contract for unattended plan supervision: branch identity is implicit, resuming on the wrong branch is easy, and there is no specification-level handoff from a completed plan run to a pull request.

Phase 2 of the operator workflow upgrades plan needs a fail-closed baseline before any git-mutating runtime code lands. This change defines that baseline for a new `plan-git-delivery` capability.

## What Changes

- Add a new `plan-git-delivery` capability spec describing opt-in branch delivery for whole-plan runs.
- Define plan configuration keys under `plan.git_delivery` for enabling delivery, selecting or deriving the delivery branch, naming the base ref, and enabling pull-request creation.
- Define durable state fields under `git_delivery` for the recorded base ref, recorded branch name, and delivery status.
- Require branch creation to fail closed unless the tracked worktree is clean.
- Require resumed runs to fail closed before any stage dispatch unless `HEAD` is on the recorded delivery branch.
- Specify that archive-commit reachability verification remains valid on the recorded delivery branch and does not require the archive commit to also appear on the base ref.
- Specify that pull-request creation is an orchestrator responsibility triggered only after all enabled changes are done and fast checks are green, and remains forbidden to phase workers.

## Capabilities

### New Capabilities

- `plan-git-delivery`: Defines opt-in git branch lifecycle and pull-request delivery for `opsx-plan` runs.

### Modified Capabilities

- None.

## Impact

- Affected specs: `openspec/specs/plan-git-delivery/spec.md` (new capability).
- Affected runtime later: `orchestrator/opsx-plan.py` branch creation, resume guards, completion delivery, and state persistence.
- Affected state later: `.opsx-plan/<plan-name>.state.json` gains a `git_delivery` object with recorded branch-delivery identity.
- No runtime git mutation is implemented by this change; it only establishes the contract that later implementation changes must satisfy.
