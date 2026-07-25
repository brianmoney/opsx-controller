## Why

Model selection is four process-global environment variables — `OPSX_CONTROLLER_MODEL`, `OPSX_IMPLEMENTER_MODEL`, `OPSX_REVIEWER_MODEL`, `OPSX_ARCHIVER_MODEL` — shared by every adapter. But model identifiers are adapter-specific: OpenCode requires `provider/model` (`deepseek/deepseek-v4-pro`), Claude Code requires a bare Anthropic alias (`claude-sonnet-5`), and Codex requires a bare OpenAI id (`gpt-5.4`). Only one adapter can be correctly configured at a time, and the failure is silent — `doctor` checks only that the variables are non-empty, so a provider-prefixed identifier passes preflight and then fails at Claude Code dispatch.

Two mechanisms compound the problem: OpenCode bakes its models into installed agent frontmatter at install time (changing a model requires re-running the installer), while Claude Code expands the variables at dispatch time and Codex ignores them entirely. `.env` is also checkout-local, yet `opsx-plan` runs inside arbitrary target repositories.

## What Changes

- Introduce a user-global `~/.config/opsx-controller/models.toml` keyed by adapter and role, with an optional machine-local `<repo>/.opsx-plan/models.toml` override.
- Add a `lib/models` resolver that resolves `(adapter, role)` to a model identifier through a defined precedence ladder and reports the resolution source.
- Resolve models once at plan load and export the four `OPSX_*_MODEL` variables for the process lifetime, so the active plan's adapter automatically selects the correct model set with no manual switching.
- Add adapter-aware identifier validation: reject a provider-prefixed identifier under `claude-code` and a bare identifier under `opencode`, at `doctor` time instead of at dispatch time.
- Add an `opsx-plan models` command surface (`show`, `env`, `init`).
- Give the OpenCode adapter's direct stage invokes an explicit `--model` argument, matching the Claude Code adapter, so model changes take effect on the next run without re-running the installer.
- Make Codex agent model selection configurable instead of hardcoded to `gpt-5.4`.
- Point the adapter installers at the resolver instead of raw environment variables, so installed artifacts and dispatched commands share one source of truth.
- **BREAKING**: When `models.toml` exists, it takes precedence over ambient `OPSX_*_MODEL` environment variables. Ambient variables remain the fallback only while no configuration file is present, so existing `.env` setups keep working until an operator opts in.
- **BREAKING**: Officially deprecate `/opsx-drive`. The nested-controller path is superseded by direct dispatch; the surface remains functional for one release but is documented as deprecated, and `opsx-plan` emits a deprecation warning when a plan resolves to the nested-controller path.

## Capabilities

### New Capabilities
- `adapter-model-configuration`: Per-adapter, per-role model storage, resolution precedence, identifier validation, process-lifetime activation, and the `opsx-plan models` command surface.

### Modified Capabilities
- `plan-driven-opencode-execution`: OpenCode direct stage invokes carry an explicit `--model` argument; `opsx-plan compile` resolves its controller model through the resolver against the `opencode` adapter; the requirement that `/opsx-drive` remains available for manual single-change control becomes a deprecation requirement.
- `plan-driven-claude-code-execution`: Claude Code stage models are resolved per adapter rather than read from shared ambient environment variables.
- `plan-operator-cli`: `doctor`'s model check becomes adapter-aware and reports resolution source and identifier-shape violations; the new `models` subcommand is added to the operator surface.
- `codex-cli-adapter`: Codex phase agents take a resolved model instead of a hardcoded `gpt-5.4`.

## Impact

**New code**: `lib/models/{__init__,types,resolver}.py`, `models.example.toml`, `tests/lib/models/test_resolver.py`.

**Modified code**: `orchestrator/opsx-plan.py` (`ADAPTER_DEFAULTS`, `load_plan`, `build_single_change_config`, `check_controller_model`, `_check_required_env_vars`, `main()` subcommand table, new `apply_model_env`); `lib/install-common.sh` (`require_model_envs` → resolver-backed `load_model_env`; `install_agent` substitution generalized over roles); `adapters/opencode/install.sh` (including the runtime-lib copy list); `adapters/codex-cli/install.sh`; `adapters/codex-cli/agents/*.toml`.

**Unchanged by design**: `_expand_invoke_token`, `invoke_direct_stage`, `run_logged_command`, the usage-sidecar environment block, and the plan manifest schema. Resolving at plan load means every existing downstream consumer of `$OPSX_*_MODEL` keeps working without modification, including the telemetry fallback that re-expands the invoke string after the sidecar environment is restored.

**Docs**: `core/model-efficiency-workflow.md` (its "model selection is install-time, not per-run" guidance becomes wrong for direct dispatch), `README.md`, `docs/opsx-plan-operator-workflow.md`, `skills/opsx-plan-manifest/references/schema.md`, and the `/opsx-drive` skill and command surfaces across all three adapters.

**Dependencies**: None added. The resolver uses `tomllib`, already required by `opsx-plan`.
