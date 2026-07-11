## 1. Run-Start Delivery Preconditions

- [x] 1.1 Verify at `opsx-plan run` start that pull-request delivery checks `gh` availability and a usable git remote when `plan.git_delivery.create_pull_request = true`.
- [x] 1.2 Define how `--no-pr` suppresses PR-delivery preflight and completion-time PR actions for that invocation only.

## 2. Completion-Time PR Delivery

- [x] 2.1 Push the recorded delivery branch only after all enabled changes are done, archive verification is complete, and fast checks are green.
- [x] 2.2 Create a GitHub pull request against the recorded base ref from the recorded delivery branch after a successful push.
- [x] 2.3 Generate the pull-request body from existing plan report evidence, including per-change status, rounds, durations, and estimated cost when available.
- [x] 2.4 Fail closed when push or `gh pr create` cannot complete and leave plan delivery state unambiguous.

## 3. Durable State And Idempotency

- [x] 3.1 Record pull-request delivery outcome in plan state, including the opened PR URL.
- [x] 3.2 Reuse recorded PR-delivery state on rerun so a completed plan does not open a duplicate PR.

## 4. Verification

- [x] 4.1 Add unit tests for run-start PR preflight failure, completion-triggered PR creation, idempotent rerun, `--no-pr`, and body generation with and without telemetry.
- [x] 4.2 Add unit tests proving push and `gh` failures fail closed without claiming PR delivery succeeded.
- [x] 4.3 Run `python3 -m unittest tests/orchestrator/test_opsx_plan.py`.
- [x] 4.4 Run `openspec validate open-pr-on-plan-completion --strict`.
- [x] 4.5 Re-run `bash adapters/opencode/install.sh --global --verify` because the orchestrator install target changes.
