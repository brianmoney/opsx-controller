## 1. Batch Command Surface

- [x] 1.1 Add `opsx-plan approve --all` using the same explicit/env/active-plan resolution contract as existing operator-facing plan commands.
- [x] 1.2 Add `opsx-plan accept --all` using the same plan-resolution behavior.
- [x] 1.3 Add `opsx-plan reset --failed` using the same plan-resolution behavior.
- [x] 1.4 Preserve existing single-change `approve`, `accept`, and `reset` forms unchanged.

## 2. Batch State Transitions And Reporting

- [x] 2.1 Make `approve --all` affect only changes currently awaiting approval.
- [x] 2.2 Make `accept --all` affect only changes currently awaiting acceptance.
- [x] 2.3 Make `reset --failed` affect only changes currently in a failed state and return each of them to pending.
- [x] 2.4 Print the exact change IDs affected by each batch command, including a clear no-op message when no changes match.

## 3. Status Guidance

- [x] 3.1 Extend `opsx-plan status` so every blocked change prints the exact next command needed to unblock it.
- [x] 3.2 Use active-plan short-form commands in status guidance when the command can omit an explicit plan path.
- [x] 3.3 Ensure the guidance reflects the actual blocking state for approval, acceptance, and failed-reset cases without changing existing gate semantics.

## 4. Verification

- [x] 4.1 Add unit tests for `approve --all` on empty, partial, and full awaiting-approval sets.
- [x] 4.2 Add unit tests for `accept --all` on empty, partial, and full awaiting-acceptance sets.
- [x] 4.3 Add unit tests for `reset --failed` on empty, partial, and full failed-change sets.
- [x] 4.4 Add unit tests for `status` output that includes copy-pasteable next commands for blocked changes.
- [x] 4.5 Run `python3 -m unittest tests/orchestrator/test_opsx_plan.py`.
- [x] 4.6 Run `openspec validate batch-gate-and-reset-commands --strict`.
- [x] 4.7 Re-run `bash adapters/opencode/install.sh --global --verify` because the orchestrator install target changes.
