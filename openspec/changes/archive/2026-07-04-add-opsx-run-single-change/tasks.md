## 1. Single-Change Runner

- [x] 1.1 Add a helper in `orchestrator/opsx-plan.py` that builds a minimal one-change OpenCode direct-execution config from a change id.
- [x] 1.2 Add a single-change command path that validates exactly one authored OpenSpec change, enforces tracked-clean startup rules, reconciles state, and calls `run_direct_change()`.
- [x] 1.3 Support both `opsx-plan run-one <change-id>` and executable-name dispatch for `opsx-run <change-id>`.

## 2. Installation and Documentation

- [x] 2.1 Update the OpenCode installer to install the orchestrator script as both `opsx-plan` and `opsx-run`.
- [x] 2.2 Document `opsx-run <change-id>` usage, state location, and limitations in the orchestrator README.

## 3. Verification

- [x] 3.1 Add unit tests for single-change config generation and missing/incomplete change rejection.
- [x] 3.2 Add unit tests proving `opsx-run` uses the existing implement-review-archive loop, including review failure retry.
- [x] 3.3 Run the orchestrator test suite and strict OpenSpec validation for `add-opsx-run-single-change`.
