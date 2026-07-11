## Why

The `plan-git-delivery` contract now defines when branch delivery is enabled, what state must be recorded, and how resumed runs fail closed on the wrong branch. What it does not yet define is the first runtime step that actually creates the plan-owned branch, checks it out, and makes later resume checks meaningful.

Phase 2 of the operator workflow upgrades plan calls for that first runtime behavior: create the delivery branch at run start from a deterministic base ref, record the resulting branch identity in plan state, and keep existing plans unchanged unless they opt in.

## What Changes

- Add plan-git-delivery requirements for first-run branch creation and checkout before any stage dispatch.
- Define deterministic resolution for the delivery branch identity: `plan.git_delivery.branch` when configured, otherwise a default branch derived from `plan.name`; `plan.git_delivery.base_ref` when configured, otherwise the symbolic `HEAD` branch at first run start.
- Require the orchestrator to persist the resolved base ref and branch name once, then reuse that recorded identity on later runs instead of recomputing it.
- Add an `opsx-plan run --no-branch` override that suppresses first-run branch creation for a one-off run while keeping the feature off by default for all non-configured plans.
- Require `--no-branch` to fail closed once a plan state has already recorded a delivery branch, so operators cannot bypass the resume guard.
- Add runtime tests for first-run creation, correct-branch resume, wrong-branch refusal, dirty-tree refusal, `--no-branch` override behavior, and default-off behavior.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `plan-git-delivery`: Adds deterministic run-start branch creation, state reuse rules, and the constrained `--no-branch` run override.

## Impact

- Affected specs: `openspec/specs/plan-git-delivery/spec.md`.
- Modified files later: `orchestrator/opsx-plan.py` run-start git-delivery setup, run argument parsing, state persistence, and branch-guard checks.
- Runtime behavior later: delivery-enabled plans can claim a dedicated branch at first run start, then fail closed on later runs unless the recorded branch is checked out.
- Test coverage later: orchestrator unit tests for branch creation from configured and derived identity, state reuse on resume, wrong-branch refusal, dirty-tree refusal, `--no-branch` first-run override, and default-off runs.
- Out of scope here: pushing to remotes, creating pull requests, rebasing or deleting branches, or allowing `--no-branch` to override an already recorded branch identity.
