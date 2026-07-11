## Why

The plan git-delivery contract and run-start branch creation work already define where plan-owned commits live and when a completed plan becomes eligible for pull-request delivery. What is still missing is the completion handoff itself: verifying PR prerequisites before a long run starts, pushing the recorded delivery branch, generating an evidence-based PR body, and ensuring reruns do not create duplicate pull requests.

Phase 2 of the operator workflow upgrades plan calls for that final delivery step so a successful plan run ends in a deterministic GitHub pull request instead of stopping at a local branch.

## What Changes

- Extend `plan-git-delivery` with completion-time pull-request delivery requirements for delivery-enabled plans that set `plan.git_delivery.create_pull_request = true`.
- Require `opsx-plan run` to preflight GitHub delivery prerequisites at run start, including `gh` availability and a usable git remote, so long runs fail early when PR delivery would be impossible.
- Require the orchestrator to push the recorded delivery branch and create exactly one pull request against the recorded base ref after all enabled changes are done and fast checks are green.
- Require the pull-request body to be generated from existing plan report evidence, including per-change status, rounds, durations, and estimated cost when telemetry is available.
- Require the plan state to record pull-request delivery outcome, including the opened PR URL, and use that state to keep reruns idempotent.
- Add an `opsx-plan run --no-pr` override that suppresses push and PR creation for that invocation without changing plan configuration.
- Add runtime tests for run-start preflight failures, completion-triggered PR creation, idempotent reruns, `--no-pr`, body generation with and without telemetry, and fail-closed push or `gh` errors.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `plan-git-delivery`: Adds run-start PR-delivery preflight, completion-time push and GitHub PR creation, evidence-based PR body generation, state-backed idempotency, and a `--no-pr` run override.

## Impact

- Affected specs: `openspec/specs/plan-git-delivery/spec.md`.
- Modified files later: `orchestrator/opsx-plan.py` run preflight, completion detection, git push and `gh` invocation, PR body rendering, and state persistence.
- Runtime behavior later: completed delivery-enabled plans can end with a single GitHub pull request created from the recorded delivery branch and report evidence.
- Test coverage later: orchestrator unit tests for PR prerequisite checks, completion gating, duplicate-prevention on rerun, `--no-pr`, evidence-based body rendering, and fail-closed delivery errors.
- Out of scope here: auto-merge, draft-progress PR updates during a run, non-GitHub forges, review automation, or merge-queue integration.
