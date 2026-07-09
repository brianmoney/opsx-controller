## 1. Active Plan Pointer

- [x] 1.1 Define the active-plan pointer path under `.opsx-plan/` and store only repo-relative TOML paths.
- [x] 1.2 Add helpers to normalize, validate, read, and write active-plan paths relative to the repository root.
- [x] 1.3 Ensure stale pointers fail closed with an error that includes the recorded missing path.

## 2. `use` Command

- [x] 2.1 Add an `opsx-plan use <plan.toml>` subcommand.
- [x] 2.2 Validate the target plan using the existing plan loader before writing the pointer.
- [x] 2.3 Print the activated repo-relative plan path on success.

## 3. Optional Plan Argument Resolution

- [x] 3.1 Make the `plan` positional optional for `run`, `status`, `approve`, `accept`, `reset`, `report`, and `dashboard`.
- [x] 3.2 Resolve plan identity in order: explicit argument, `OPSX_PLAN`, pointer file, then actionable missing-plan error naming `opsx-plan use <plan.toml>`.
- [x] 3.3 Preserve existing behavior when an explicit plan path is supplied.
- [x] 3.4 Keep commands that still require a source or output path, such as `compile`, semantically unchanged except for activation after success.

## 4. Auto-Activation And Status Output

- [x] 4.1 Auto-activate the output plan after successful `opsx-plan compile -o <plan.toml>`.
- [x] 4.2 Auto-activate a plan after `opsx-plan run <plan.toml>` is invoked with an explicit path.
- [x] 4.3 Show the active plan in `opsx-plan status` output.

## 5. Tests And Verification

- [x] 5.1 Add unit tests for explicit argument, `OPSX_PLAN`, pointer file, and no-plan error resolution branches.
- [x] 5.2 Add unit tests proving explicit arguments and `OPSX_PLAN` override the pointer.
- [x] 5.3 Add unit tests for `use`, compile auto-activation, run explicit-path auto-activation, status rendering, and stale pointer errors.
- [x] 5.4 Run `python3 -m unittest tests/orchestrator/test_opsx_plan.py`.
- [x] 5.5 Run `openspec validate add-active-plan-resolution --strict`.
- [x] 5.6 Re-run `bash adapters/opencode/install.sh --global --verify` because the orchestrator install target changes.
