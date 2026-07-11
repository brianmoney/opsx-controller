## 1. Author The Operator Workflow Guide

- [x] 1.1 Add a dedicated `opsx-plan` operator workflow document that covers
      plan activation, command resolution precedence, `doctor`, plan run,
      budgets, batched gate controls, log access, notifications, and branch/PR
      delivery.
- [x] 1.2 Include a worked end-to-end example from `opsx-plan compile` through
      successful pull-request creation on a delivery-enabled plan.
- [x] 1.3 Document every new config key, command, and flag introduced by the
      operator-workflow-upgrade series with its default behavior.
- [x] 1.4 Call out all fail-closed behaviors and one-run overrides explicitly,
      including stale active-plan pointers, doctor failures, wrong-branch
      resume refusal, `--no-branch`, `--no-pr`, and budget stop semantics.

## 2. Update Existing Entry Points

- [x] 2.1 Update `orchestrator/README.md` so its plan-level execution guidance
      reflects the activate-then-run workflow and links to the dedicated
      operator workflow document.
- [x] 2.2 Update the repository `README.md` so the root documentation points
      operators to the `opsx-plan` workflow guide.

## 3. Verification

- [x] 3.1 Verify all documented command names, flags, config keys, file paths,
      and precedence rules match the final `opsx-plan` implementation.
- [x] 3.2 Verify the documentation states which features are off by default and
      what overrides are available for each.
- [x] 3.3 Run `openspec validate document-operator-workflow-upgrades --strict`.
