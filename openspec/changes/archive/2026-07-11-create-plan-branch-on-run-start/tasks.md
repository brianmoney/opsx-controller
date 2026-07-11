## 1. Run-Start Branch Creation

- [x] 1.1 Create and check out the plan delivery branch at first run start when `plan.git_delivery.enabled = true` and no branch has yet been recorded in state.
- [x] 1.2 Resolve the branch name from `plan.git_delivery.branch` when configured, otherwise derive it deterministically from `plan.name`.
- [x] 1.3 Resolve the base ref from `plan.git_delivery.base_ref` when configured, otherwise from the current symbolic `HEAD` branch at first run start.
- [x] 1.4 Persist the resolved `git_delivery.base_ref` and `git_delivery.branch_name` in plan state and mark the delivery status as branch-ready only after branch creation and checkout succeed.

## 2. Resume Safety And Override Rules

- [x] 2.1 Reuse the recorded branch and base identity on later runs instead of recomputing configured defaults.
- [x] 2.2 Refuse to dispatch any stage when a delivery-enabled state records a branch and the current `HEAD` is not on that exact branch.
- [x] 2.3 Add `opsx-plan run --no-branch` as a first-run-only override that suppresses branch creation when no branch has yet been recorded.
- [x] 2.4 Fail closed when `--no-branch` is supplied for a plan state that already records a delivery branch.

## 3. Verification

- [x] 3.1 Add unit tests for first-run branch creation with explicit branch and base-ref configuration.
- [x] 3.2 Add unit tests for first-run branch creation with derived branch and default base-ref resolution.
- [x] 3.3 Add unit tests for resume on the recorded branch and refusal on the wrong branch.
- [x] 3.4 Add unit tests proving dirty tracked trees block first-run branch creation.
- [x] 3.5 Add unit tests for `--no-branch` on an unrecorded first run and for rejection after branch identity is already recorded.
- [x] 3.6 Add unit tests proving plans without git-delivery configuration behave exactly as they do today.
- [x] 3.7 Run `python3 -m unittest tests/orchestrator/test_opsx_plan.py`.
- [x] 3.8 Run `openspec validate create-plan-branch-on-run-start --strict`.
- [x] 3.9 Re-run `bash adapters/opencode/install.sh --global --verify` because the orchestrator install target changes.
