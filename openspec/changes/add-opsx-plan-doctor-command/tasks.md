## 1. CLI Surface And Resolution

- [ ] 1.1 Add an `opsx-plan doctor [plan]` subcommand that uses the same explicit/env/active-plan resolution contract as other operator-facing plan commands.
- [ ] 1.2 Preserve support for running `doctor` without a plan argument when only plan-independent checks are needed and no active plan is set.
- [ ] 1.3 Ensure `doctor` exits non-zero when any preflight check fails.

## 2. Core Preflight Checks

- [ ] 2.1 Compare the repo orchestrator copy against the installed `~/.local/bin` copy and report stale-install mismatches with an install remediation hint.
- [ ] 2.2 Check that required `OPSX_*_MODEL` environment variables are set and report any missing variables explicitly.
- [ ] 2.3 Check that `openspec` and the configured adapter client executable are on `PATH`.
- [ ] 2.4 Check that the tracked tree contains no tracked `__pycache__` directories or `.pyc` files.
- [ ] 2.5 Check that the tracked tree is clean before the run starts.

## 3. Plan-Aware Preflight Checks And Run Warnings

- [ ] 3.1 When a plan is provided or resolved, validate that the plan loads successfully before reporting plan-conditional checks as passing.
- [ ] 3.2 When the resolved plan enables pull-request delivery, require `gh` on `PATH` and at least one configured git remote.
- [ ] 3.3 Reuse the same check set at `opsx-plan run` start as warning-only output that never changes the run outcome.
- [ ] 3.4 Emit one human-readable pass/fail line per `doctor` check and include a remediation hint on failures.

## 4. Verification

- [ ] 4.1 Add unit tests for each failing check class using fabricated repository and environment state.
- [ ] 4.2 Add unit tests covering `doctor` with no plan, with an active plan, and with an explicit plan argument.
- [ ] 4.3 Add unit tests proving run-start preflight output is warning-only and does not block dispatch.
- [ ] 4.4 Run `python3 -m unittest tests/orchestrator/test_opsx_plan.py`.
- [ ] 4.5 Run `openspec validate add-opsx-plan-doctor-command --strict`.
- [ ] 4.6 Re-run `bash adapters/opencode/install.sh --global --verify` because the orchestrator install target changes.
