## Context

`_REQUIRED_MODEL_ENV_VARS` (`orchestrator/opsx-plan.py:103`) names four process-global variables that every adapter shares. Three adapters need three different identifier syntaxes for the same logical role, so at most one adapter is correctly configured at any time.

The current wiring has three distinct consumption paths:

| Path | Mechanism | Site |
|---|---|---|
| OpenCode direct dispatch | agent frontmatter, baked at install time | `install_agent`, `lib/install-common.sh:67-83` |
| Claude Code direct dispatch | `$OPSX_*_MODEL` expanded at dispatch time | `_expand_invoke_token`, `opsx-plan.py:2327` |
| Codex | hardcoded `model = "gpt-5.4"` | `adapters/codex-cli/agents/*.toml:4` |

Two constraints shape the design.

**Telemetry ordering.** `run_direct_change` restores `os.environ` at `opsx-plan.py:2740`, before `_record_stage_telemetry` runs. The telemetry fallback attribution re-expands `$OPSX_*_MODEL` out of the invoke string (`_best_effort_expand_invoke`, `:1280`, used at `:1810`). Any design that scopes the model environment narrowly around the subprocess call silently breaks model attribution and therefore cost estimation.

**Nested subagent dispatch.** In the legacy drive path, `opencode run "/opsx-drive {change}"` starts `opsx-controller`, which spawns the phase agents through OpenCode's own `task` tool (`adapters/opencode/agents/opsx-controller.md:19-23`). The orchestrator never sees that spawn and cannot pass `--model` to it, so those subagents can only be configured through installed frontmatter. `{env:...}` is an opsx-controller installer placeholder, not OpenCode-native syntax — `core/model-efficiency-workflow.md:96-101` confirms there is no runtime resolution.

That second constraint is the reason this change also deprecates `/opsx-drive`. Direct dispatch has been the default for both `opencode` and `claude-code` since `ADAPTER_DEFAULTS` began supplying all three stage invokes; the nested path is the only remaining consumer of install-time model baking. Deprecating it lets dispatch-time `--model` become the single mechanism.

## Goals / Non-Goals

**Goals:**
- One source of truth for model selection, keyed by adapter and role.
- Automatic activation: the active plan's adapter selects the model set, with no operator switch step.
- Model changes take effect on the next run, with no installer re-run, on every direct-dispatch path.
- Catch adapter/identifier mismatches at `doctor` time rather than at dispatch time.
- Preserve existing `.env`-based setups until an operator opts in.
- Keep telemetry model attribution and cost estimation working unchanged.

**Non-Goals:**
- Named model profiles or tiers (for example `cheap` / `premium`). Adapter keying resolves the reported problem; profiles can layer on later without changing the file format.
- A `[models]` table in the plan manifest. Adapter keying makes per-plan model configuration unnecessary, and per-plan models would not help install-time consumers.
- Adding direct stage invokes to the `codex-cli` adapter. Codex remains a nested-controller adapter; only its model source changes.
- Removing `/opsx-drive`. This change deprecates it and warns; removal is a later change.
- Automatic migration of `.env` into `models.toml`. `opsx-plan models init` makes it a single explicit command.

## Decisions

### Configuration lives in a user-global TOML, with a repo-local override

`~/.config/opsx-controller/models.toml` is primary; `<repo>/.opsx-plan/models.toml` overrides it when present.

`opsx-plan` runs inside arbitrary target repositories, so a checkout-local `.env` is the wrong home for a machine-wide preference. `.opsx-plan/` is already gitignored by `write_active_plan`, which makes the repo-local override machine-local by construction.

*Alternative considered:* a repo-root `models.toml` beside `.env`. Rejected — it only applies when the working directory is the controller checkout, which is not where plans usually run.

*Alternative considered:* a `[models]` table in each plan manifest. Rejected — it repeats the model set in every plan and does nothing for installer-time consumers.

### Precedence puts the configuration file above ambient environment

Per role, highest first:

1. `[adapters.<adapter>].<role>` — repo-local file
2. `[adapters.<adapter>].<role>` — user-global file
3. `[defaults].<role>` — repo-local, then user-global
4. ambient `OPSX_<ROLE>_MODEL`
5. unresolved → fail closed

Ambient environment sits *below* the file deliberately. It is the shared value this change exists to replace, so letting a stale sourced `.env` win would reintroduce exactly the bug being fixed. Placing it last still means every existing setup keeps working byte-for-byte until the operator creates a `models.toml`.

This inverts the usual "environment overrides config" convention, which is a real cost. It is mitigated by `models show` reporting the resolution source for every role, so a surprising value is one command away from being explained.

### Resolve once at plan load; export for the process lifetime

`load_plan` attaches `cfg["models"]`, and a new `apply_model_env(cfg)` writes the four variables into `os.environ` immediately after every configuration construction — the `load_plan` call sites, `build_single_change_config` (`:313`), and the compile path.

`opsx-plan` handles exactly one plan per invocation, so the resolved model set is constant for the process. Setting it once means no save/restore, and therefore no telemetry-ordering hazard: `_best_effort_expand_invoke` at `:1810` still sees the correct values long after the sidecar environment has been restored.

The payoff is that nothing downstream changes. `_expand_invoke_token`, `invoke_direct_stage`, `run_logged_command`, `run_stage`'s `{controller_model}` substitution (`:3754`), and `check_controller_model` (`:3965`) all keep working against `os.environ` exactly as they do today.

*Alternative considered:* a context manager scoped around dispatch, mirroring the sidecar block at `:2712-2745`. Rejected — it is strictly more code and it breaks telemetry attribution unless the scope is widened to enclose the telemetry write, at which point it is a worse version of process-lifetime export.

*Alternative considered:* threading resolved models through call signatures instead of the environment. Rejected — the invoke strings are operator-authored and reference `$OPSX_*_MODEL` by name, so the environment is the actual interface.

### `compile` resolves against `opencode` specifically

`check_controller_model` feeds `run_opencode_for_compile` (`:4162`), which shells out to the `opencode` binary regardless of the active plan's adapter. It therefore resolves the controller role against adapter `opencode`, not against `cfg["adapter"]`. Resolving against the plan's adapter would hand an Anthropic alias to OpenCode under a `claude-code` plan.

### OpenCode stage invokes gain `--model`, mirroring Claude Code

`ADAPTER_DEFAULTS["opencode"]` stage invokes become `opencode run --agent opsx-<role> --model "$OPSX_<ROLE>_MODEL"`. `opencode run --model` is already proven by `run_opencode_for_compile`.

This needs no new expansion machinery — `_expand_invoke_token` handles those tokens today for the Claude Code defaults. It also improves telemetry: `_extract_invocation_model` (`:1285`) prefers an explicit `--model` argument over parsing installed agent frontmatter, so attribution stops depending on reading files out of the adapter's agent directory.

### `/opsx-drive` is deprecated rather than removed

`plan-driven-opencode-execution` currently requires that `/opsx-drive` remain available for manual single-change control. That requirement is replaced by a deprecation requirement: the surface keeps working, the documentation marks it deprecated and points at `opsx-run <change-id>` as the supported single-change path, and `opsx-plan` logs a deprecation warning when a plan resolves to the nested-controller path.

Warning at plan resolution rather than failing keeps existing manifests running. Deprecating rather than removing keeps the change reviewable — removal touches the skill and command surfaces of all three adapters and belongs in its own change.

### Install-time baking is retained, but resolver-backed

While `/opsx-drive` still functions, OpenCode nested subagents can only get their model from installed frontmatter. `install_agent` keeps substituting, but the values come from the resolver rather than from raw environment variables, so there is still exactly one source of truth. Its four hardcoded substitutions become a loop over the role list, which also makes it reusable for Codex's `.toml` agents.

The installers reach the resolver by invoking the orchestrator out of the source tree via `OPSX_CONTROLLER_ROOT`, not off `PATH` — only the OpenCode installer installs `opsx-plan`, so a `PATH` dependency would make the Codex installer depend on the OpenCode one.

### `lib/models` follows the `lib/pricing` pattern

A resolver module beside `lib/metrics` and `lib/pricing` inherits the existing runtime plumbing: `_ensure_runtime_modules` (`:40-55`) already puts that root on `sys.path`, and the OpenCode installer already copies `lib/` subpackages to `~/.local/lib/opsx-controller`. The only new plumbing is adding `lib/models` to that copy list.

## Risks / Trade-offs

**Configuration silently overriding a sourced `.env`** → Inverts the conventional precedence. Mitigated by `models show` printing the resolution source per role, by `doctor` reporting source alongside each resolved model, and by the file only taking effect once an operator creates it.

**`opencode run --model` may not accept every identifier that frontmatter accepts** → Only `run_opencode_for_compile` exercises that flag today. Verification runs a real single-change plan per adapter and checks the expanded command in the stage log, before the docs declare install-time baking obsolete.

**Installers gain a Python dependency at model-resolution time** → The OpenCode installer already installs a Python orchestrator requiring 3.11+, so the interpreter is a pre-existing requirement. The resolver must work with no git repository and no configuration file present, since installers run in both conditions; `load_model_env` fails with actionable guidance rather than a traceback.

**Deprecating `/opsx-drive` while it remains the only path for OpenCode nested subagents** → Install-time baking is retained for exactly this reason, so no currently-working configuration breaks. The coupling is recorded here so the eventual removal change knows to drop `install_agent`'s substitution at the same time.

**Codex agents move from a hardcoded model to a resolved one** → An operator with no `models.toml` and no `OPSX_*_MODEL` exported currently gets a working Codex install and would now get an install-time failure. `[defaults]` in `models.example.toml` plus the `models init` command cover this, and the failure is loud and immediate rather than silent.

**Stale installed orchestrator** → `~/.local/bin/opsx-plan` is a copy, not a symlink, so orchestrator edits do not take effect until the installer runs again. `doctor`'s content-hash check (`:3496`) already covers this; verification re-runs the installer before end-to-end checks.

## Migration Plan

1. Land the resolver, the orchestrator wiring, and the CLI with no configuration file present. Every existing setup continues to resolve through the ambient-environment fallback.
2. Operators run `opsx-plan models init` to seed `~/.config/opsx-controller/models.toml` from their current environment, then edit per-adapter overrides.
3. Operators re-run the adapter installers so installed frontmatter picks up resolver-sourced values.
4. `.env.example` is retained and annotated as the legacy fallback.

Rollback is deleting `models.toml`: resolution falls back to ambient environment variables and behavior matches the current release.
