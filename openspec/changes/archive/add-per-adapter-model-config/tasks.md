## 1. Resolver module

- [x] 1.1 Create `lib/models/__init__.py` and `lib/models/types.py` following the `lib/pricing` conventions; define `ROLES = ("controller", "implementer", "reviewer", "archiver")`, `ROLE_ENV` mapping each role to `OPSX_<ROLE>_MODEL`, and a frozen `ResolvedModel` dataclass carrying `role`, `model`, and `source`
- [x] 1.2 Implement `config_paths(repo)` in `lib/models/resolver.py` returning the repository-local `<repo>/.opsx-plan/models.toml` (when a repo is given) followed by `~/.config/opsx-controller/models.toml`
- [x] 1.3 Implement `resolve(adapter, repo=None, environ=None)` applying the precedence ladder: repo-local adapter table, global adapter table, repo-local defaults, global defaults, ambient `OPSX_<ROLE>_MODEL`, then unresolved; record the source on each resolution
- [x] 1.4 Handle missing files by falling through, handle a missing repository by consulting only the user-global file, and raise an error naming the file when a present file contains invalid TOML
- [x] 1.5 Implement `validate(adapter, resolved)` returning a warning per violating role: an identifier containing `/` under `claude-code`, and an identifier without `/` under `opencode`
- [x] 1.6 Write `models.example.toml` at the repository root with a `[defaults]` table and an `[adapters.<name>]` table for each of `opencode`, `claude-code`, and `codex-cli`

## 2. Orchestrator wiring

- [x] 2.1 Import the resolver in `orchestrator/opsx-plan.py` through the existing `_ensure_runtime_modules` path and add `apply_model_env(cfg)` that resolves all four roles for `cfg["adapter"]` and writes them into `os.environ`
- [x] 2.2 Populate `cfg["models"]` in `load_plan` and call `apply_model_env` at every configuration-construction site, including `build_single_change_config`
- [x] 2.3 Fail closed with an error naming the unresolved role when `apply_model_env` finds a role it cannot resolve
- [x] 2.4 Change `check_controller_model` to resolve the `controller` role against the `opencode` adapter specifically, independent of the active plan's adapter, and keep its fail-closed behavior when unresolved
- [x] 2.5 Add `--model "$OPSX_<ROLE>_MODEL"` to the three `opencode` entries in `ADAPTER_DEFAULTS`, mirroring the existing `claude-code` entries
- [x] 2.6 Verify no changes are needed in `_expand_invoke_token`, `invoke_direct_stage`, `run_logged_command`, `run_stage`'s `{controller_model}` substitution, or the usage-sidecar environment block; record any that do need changing

## 3. Operator CLI

- [x] 3.1 Add a `models` subcommand group to `main()` with `show`, `env`, and `init`
- [x] 3.2 Implement `models show [--adapter]` printing role, resolved model, and source per line, plus `validate` warnings; resolve the adapter from the active plan when `--adapter` is omitted, and require no plan when it is supplied
- [x] 3.3 Implement `models env [--adapter]` emitting shell-quoted `export` statements, exiting non-zero without partial output when any role is unresolved
- [x] 3.4 Implement `models init` writing `~/.config/opsx-controller/models.toml` seeded from the current environment, refusing to overwrite an existing file without a force flag
- [x] 3.5 Confirm `models show` and `models env` work with no git repository and no configuration file present

## 4. Doctor

- [x] 4.1 Replace `_check_required_env_vars` with an adapter-aware check that resolves all four roles for the resolved plan's adapter and fails naming each unresolved role
- [x] 4.2 Report the resolution source alongside each resolved model in the doctor output
- [x] 4.3 Add an identifier-syntax check surfacing `validate` warnings as a failing doctor check with a remediation hint

## 5. Installers

- [x] 5.1 Replace `require_model_env`/`require_model_envs` in `lib/install-common.sh` with `load_model_env <adapter>`, which evaluates `python3 "$OPSX_CONTROLLER_ROOT/orchestrator/opsx-plan.py" models env --adapter <adapter>` and exits non-zero with actionable guidance on failure
- [x] 5.2 Generalize `install_agent`'s four hardcoded `{env:OPSX_*_MODEL}` substitutions into a loop over the role list so it works for both `.md` and `.toml` agent files
- [x] 5.3 Update `adapters/opencode/install.sh` to call `load_model_env opencode` at both the global and project install sites
- [x] 5.4 Add `lib/models` to the runtime library copy in `install_orchestrator` so the installed orchestrator can import the resolver
- [x] 5.5 Change `model = "gpt-5.4"` to the matching `{env:OPSX_<ROLE>_MODEL}` placeholder in the three `adapters/codex-cli/agents/*.toml` files
- [x] 5.6 Update `adapters/codex-cli/install.sh` to call `load_model_env codex-cli` and install its agents through the substituting path
- [x] 5.7 Confirm `adapters/claude-code/install.sh` needs no change, since its agents stay `model: inherit` and take the model from the dispatch-time `--model` argument

## 6. Deprecate `/opsx-drive`

- [x] 6.1 Emit a deprecation warning naming the nested-controller path and pointing at direct dispatch when a resolved plan takes the nested-controller path, without failing the run
- [x] 6.2 Confirm no deprecation warning is emitted for direct-dispatch plans
- [x] 6.3 Mark the `/opsx-drive` surface deprecated in the OpenCode command (`adapters/opencode/commands/opsx-drive.md`) and in the Claude Code, Codex, and plugin skill files, each naming `opsx-run <change-id>` as the supported replacement
- [x] 6.4 Update `README.md`, `orchestrator/README.md`, and `skills/opsx-controller/references/adapters.md` to describe `/opsx-drive` as deprecated

## 7. Tests

- [x] 7.1 Add `tests/lib/models/__init__.py` and `tests/lib/models/test_resolver.py` covering the full precedence ladder and source attribution for each rung
- [x] 7.2 Test missing-file fallthrough, no-repository resolution, and the malformed-TOML error naming the file
- [x] 7.3 Test both `validate` rules, including the multiple-violation case
- [x] 7.4 Add orchestrator tests that `load_plan` populates `cfg["models"]` per adapter and that two adapters resolve different identifiers for the same role from one configuration file
- [x] 7.5 Add an orchestrator test that after `apply_model_env`, `invoke_direct_stage` expands `$OPSX_IMPLEMENTER_MODEL` to the adapter-specific value
- [x] 7.6 Add a test that the model environment is still populated when telemetry attribution runs, after the usage-sidecar environment has been restored
- [x] 7.7 Add a test that a nested-controller plan triggers the deprecation warning and a direct-dispatch plan does not
- [x] 7.8 Run the full suite and confirm no regressions in `tests/orchestrator/test_opsx_plan.py`

## 8. Documentation

- [x] 8.1 Rewrite the "Model selection is install-time, not per-run" section of `core/model-efficiency-workflow.md`, including the comparison workflow that currently instructs operators to re-run the installer between model sets
- [x] 8.2 Update `README.md` model configuration guidance to lead with `models.toml` and `opsx-plan models init`
- [x] 8.3 Update `docs/opsx-plan-operator-workflow.md`, including the section documenting the provider-prefix gotcha, which is now caught by `doctor`
- [x] 8.4 Update `skills/opsx-plan-manifest/references/schema.md`, correcting the stale claim that only `opencode` supplies direct-stage defaults
- [x] 8.5 Annotate `.env.example` as the legacy fallback and point at `models.example.toml`

## 9. Verification

- [x] 9.1 Re-run the adapter installers and confirm installed OpenCode and Codex agent files carry concrete resolved identifiers with no `{env:` residue
- [x] 9.2 Confirm `opsx-plan models show --adapter opencode` and `--adapter claude-code` return different identifiers from one configuration file with no environment edits between them
- [x] 9.3 Confirm `opsx-plan doctor` fails with the provider-prefix warning for a `claude-code` plan whose implementer resolves to a provider-prefixed identifier
- [ ] 9.4 Run a single change per adapter with `opsx-plan run-one`, confirming the stage log header shows the expanded per-adapter `--model` and that `.opsx-plan/telemetry/<plan>.jsonl` records the matching `model.model_id` and a resolved cost — **not run**: requires a live adapter-CLI dispatch (real API cost, real archive commit); left for the operator to run manually, e.g. `opsx-plan run-one <change-id>` per adapter
- [x] 9.5 Confirm back-compatibility: with no `models.toml` present and `.env` sourced, a plan run behaves as it does today
- [x] 9.6 Re-run the installer after the final orchestrator edit and confirm `opsx-plan doctor` reports the installed copy as current
