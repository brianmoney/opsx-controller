## Why

`opsx-run <change-id>` (and `opsx-plan run-one`) writes every run artifact — state, telemetry, usage sidecars, worker state — under the synthetic plan name `run-<change-id>`, but never writes a plan manifest. `opsx-plan report` and `opsx-plan dashboard` resolve a plan TOML before doing anything, so single-change runs are unreportable: the operator gets `no plan specified`, or worse, a silent report of whatever unrelated plan happens to be active. The telemetry needed for a report is already on disk; the only thing missing is a manifest to name it.

Separately, compiled `.toml` plans have no enforced home. `compile` requires `-o` with no default, so four different target directories appear across the docs, and completed plans reach `openspec/plans/archived/` only by manual `git mv` — a flow that has already left this repository with an active-plan pointer aimed at a file that was moved out from under it.

## What Changes

- `run-one` writes a durable, self-verified single-change manifest to `.opsx-plan/plans/run-<change-id>.toml` before dispatching workers, making `report` and `dashboard` work for single-change runs.
- The generated manifest is validated by round-tripping it through the existing plan loader and asserting the reloaded configuration equals the synthesized one, so the manifest provably describes the run that executed.
- `run-one` does **not** become the active plan; it prints the `report`/`dashboard` commands instead, so a bare `opsx-plan run`/`status` is never silently repointed at a single-change manifest.
- `report` and `dashboard` gain `--for-change <id>`, resolving the generated manifest when present and falling back to the `run-<id>` plan name when it is absent (so runs predating this change stay reportable).
- `compile` gains a default output of `openspec/plans/<source-stem>.toml`, making `-o` optional and establishing one canonical home for authored plan manifests.
- Repository template-plan discovery also looks in `openspec/plans/archived/`, so the archived pairs the README advertises as the canonical examples actually reach the compile prompt.
- New `opsx-plan archive-plan <plan.toml>` moves a completed `.md`+`.toml` pair into `openspec/plans/archived/` and clears the active-plan pointer when it referenced the moved plan.

Not breaking: `-o` remains accepted, existing manifests and plan paths keep working, and no existing command changes its default behavior.

## Capabilities

### New Capabilities
- `plan-manifest-lifecycle`: where plan `.toml` manifests come from, where they live, and how they retire — the canonical directory for authored manifests, the derived-manifest location for single-change runs, and the supported archival path for completed plans.

### Modified Capabilities
- `plan-driven-opencode-execution`: the single-change runner still requires no manifest as *input*, but now emits one as a durable byproduct; template-plan discovery for compile prompts extends to archived pairs.
- `plan-operator-cli`: `compile` accepts an omitted `-o` and activates the defaulted output; new `archive-plan` subcommand; the pointer-clearing behavior is defined as an explicit reported operator action, distinct from the existing prohibition on silently self-healing a stale pointer at resolution time.
- `plan-run-observability`: `report` and `dashboard` can target a single-change run by change id rather than by manifest path.

## Impact

- `orchestrator/opsx-plan.py`: `build_single_change_config`, `cmd_run_one`, `cmd_report`, `cmd_dashboard`, `cmd_compile`, `discover_template_pairs`, `write_active_plan`, argparse wiring, plus a new manifest serializer and `cmd_archive_plan`. The `opsx-run` argv[0] dispatch path needs no change.
- `tests/orchestrator/test_opsx_plan.py`: new coverage for serialization round-trip, manifest emission, `--for-change`, `archive-plan`, default compile output, and archived-pair discovery.
- Docs: `README.md`, `orchestrator/README.md`, `docs/opsx-plan-operator-workflow.md`.
- No new dependencies; the manifest writer is hand-rolled because `tomllib` is read-only and the project is stdlib-only.
- Runtime effect requires re-running `scripts/install-orchestrator.sh`, since `opsx-plan`/`opsx-run` execute from installed copies.
- Out of scope: the `docs/plans/` default for *authored* markdown in `claude-code-plan-authoring` stays as is; pre-compile `.md` location is unconstrained.
