## Context

`cmd_run_one` calls `build_single_change_config`, which synthesizes an in-memory config named `run-<change-id>` and returns it. Nothing is serialized. Every downstream artifact is nonetheless keyed by that name under `.opsx-plan/` — `run-<id>.state.json`, `telemetry/run-<id>.jsonl`, `usage/run-<id>/`, `workers/run-<id>/`.

Meanwhile `cmd_report` and `cmd_dashboard` both follow `resolve_plan` → `load_plan` → and then consume **exactly one field**, `cfg["name"]`; `aggregate(repo, plan_name, run_id)` takes the name as a plain string and re-derives the telemetry and state paths itself. The plan TOML is therefore a gatekeeper that contributes nothing but a name, and single-change runs never produce one.

Constraints that shape the design:

- The project is stdlib-only and targets Python 3.11+. `tomllib` parses but cannot write, so emitting a manifest means writing a serializer.
- `openspec/plans/` is the only plan directory hardcoded in the runtime (`discover_template_pairs`), and is what the README and `plan-operator-cli` scenarios use. `compile`'s `-o` is required with no default, which is why four different output locations appear across the docs.
- `.opsx-plan/` is already gitignored and self-ignoring (the directory writes its own `.gitignore` containing `*`).
- `opsx-plan` and `opsx-run` are the same source file installed under two names; `opsx-run` is dispatched by `argv[0]` and hand-parses its arguments before calling `cmd_run_one` directly.

## Goals / Non-Goals

**Goals:**
- Make `report` and `dashboard` work for single-change runs without the operator hand-constructing anything.
- Guarantee the emitted manifest describes the configuration that actually ran, permanently — not just at implementation time.
- Give `.toml` manifests one canonical home and one supported way to retire.
- Keep runs that predate this change reportable.

**Non-Goals:**
- Constraining where authored `.md` plans live before compilation. The `docs/plans/` default in `claude-code-plan-authoring` is untouched.
- Making `run-one` adapter-selectable. It stays pinned to OpenCode; the manifest records that pinning rather than opening it up.
- Auto-committing archived plans, or teaching the change archiver about plan files.
- Any change to the aggregator, telemetry schema, or report/dashboard output content.

## Decisions

### Generated manifests live under `.opsx-plan/plans/`, not `openspec/plans/`

`openspec/plans/` means *authored* plans — human-written `.md` and the `.toml` compiled from it, both tracked in git. A single-change manifest is derived: reproducible from the change id plus adapter defaults, regenerated on every run, and meaningless without the sibling state and telemetry it names. Putting it in `.opsx-plan/plans/run-<id>.toml` places it beside the run artifacts it belongs to and inherits the existing gitignore, so `opsx-run` never dirties the working tree.

*Alternatives considered:* `openspec/plans/run-<id>.toml` gives one location for every `.toml` and tracks manifests as durable evidence, but makes every `opsx-run` leave an untracked file to commit or clean up, and dilutes the "authored plan" meaning of that directory. `openspec/plans/generated/` plus a gitignore entry keeps one visual tree but adds a directory whose contents are excluded from the tree's own convention — the split without the clarity.

### The manifest is verified by round-trip, not by inspection

`write_single_change_manifest` follows the pattern `cmd_compile` already uses — write to a temp sibling, load it through `load_plan`, `os.replace` on success — and adds an equality assertion between the reloaded config and the synthesized one, failing with `PlanError` if they diverge.

This is not belt-and-braces. `load_plan` and `build_single_change_config` agree on every default except one: `load_plan` defaults `review_created` to `True`, while `build_single_change_config` sets it to `False`. A serializer that omits the field produces a manifest that reads back as a *different* run configuration. The round-trip assertion converts that whole class of drift — including future divergence as either function gains fields — from a silent behavior change into a loud failure at write time.

*Alternative considered:* hand-maintaining the field list and covering it with a unit test. That catches today's divergence but not tomorrow's, because the test asserts what the author already knew to check.

### `run-one` does not touch the active-plan pointer

`compile` auto-activates its output, so mirroring that would make bare `opsx-plan report` work after a run. It would also mean a single-change manifest silently becomes the target of the next bare `opsx-plan run` or `status`, clobbering whatever plan the operator had active. Running `status` against a plan whose change has been archived is destructive to that plan's state, which makes the failure mode worse than the convenience is worth.

Instead `run-one` prints the exact `report` and `dashboard` invocations on completion, and `--for-change` removes the need to type the path at all. The pointer stays under the operator's explicit control.

### `--for-change` degrades to name-only resolution

When `--for-change <id>` is given, the manifest at `.opsx-plan/plans/run-<id>.toml` is used if present. If it is absent but `.opsx-plan/run-<id>.state.json` exists, the plan name `run-<id>` is passed to the aggregator directly and `load_plan` is skipped entirely. Skipping the loader is legitimate rather than a shortcut: `cfg["name"]` is the only field these commands consume. This keeps every run performed before this change reportable, which matters because those runs' telemetry is already on disk.

A separate flag rather than overloading the positional `plan` argument: one argument meaning either "path to a TOML" or "a change id" is ambiguous exactly when a change id collides with a relative path, and produces confusing errors when it guesses wrong.

### `archive-plan` clears the pointer, and this does not contradict fail-closed resolution

`plan-operator-cli` already requires that a stale pointer fails closed and that the orchestrator never "silently clear[s] the stale pointer." That requirement governs *resolution* time: `status`/`run` encountering a dangling pointer must stop, not self-heal by guessing.

`archive-plan` operates at a different moment and under different authority. The operator explicitly named the plan being retired, the command reports that it cleared the pointer, and it acts *before* the pointer becomes stale rather than papering over staleness afterward. Clearing rather than repointing at the archived copy is deliberate: repointing would leave the operator one bare `opsx-plan status` away from mutating an archived plan's state.

The command uses `git mv` for tracked files, guarded by `git ls-files`, and a plain rename for untracked ones; it does not commit.

### Template discovery extends one level into `archived/`

`discover_template_pairs` globs `openspec/plans/*.md` non-recursively. In this repository that returns nothing, because the only pairs live in `archived/` — so the compile prompt falls back to "no template pair was found" while the README points readers at those very pairs as the canonical examples. Listing top-level pairs first and then `archived/` fixes the contradiction while keeping active plans as the primary examples. Deliberately one level, not `rglob`, so an unrelated nested directory can't start feeding the compile prompt.

## Risks / Trade-offs

- **A hand-rolled TOML serializer is a new correctness surface.** → Its output shape is fixed (one `[plan]` table, one `[[changes]]` table, no user-authored free text beyond the change id), it reuses the existing `_escape_toml_value` helper, and nothing is written unless `load_plan` accepts it and the round-trip comparison passes.
- **Manifests regenerate on every run and overwrite silently.** → Intended: they are derived artifacts, and a stale manifest describing a superseded adapter default would be worse than an overwritten one. They live in gitignored space, so nothing is lost.
- **`--for-change` name-only fallback bypasses `load_plan`.** → Guarded by requiring the state file to exist, so a typo'd change id errors clearly rather than producing an empty report.
- **`compile` defaulting its output could surprise scripts that relied on `-o` being mandatory.** → `-o` continues to work unchanged; only omitting it is new, and omitting it previously was an argparse error, so no existing invocation changes meaning.
- **`archive-plan` moving tracked files leaves the repository dirty.** → The command reports what moved and that it needs committing; it does not commit on the operator's behalf, matching how the rest of the CLI treats git state.
- **Edits do not take effect until reinstalled.** → `opsx-plan`/`opsx-run` run from installed copies under `~/.local/bin`; verification starts by re-running `scripts/install-orchestrator.sh`.

## Migration Plan

No data migration. The change is additive: existing manifests, plan paths, `-o` invocations, and the active-plan pointer semantics all keep working. Rollback is reverting the source file and reinstalling; generated manifests left under `.opsx-plan/plans/` are inert and gitignored, and `report`/`dashboard` by explicit path continue to work with or without them.

## Open Questions

None blocking. One point deferred by choice: authored `.md` plans still default to `docs/plans/` per `claude-code-plan-authoring` while compiled `.toml` standardizes on `openspec/plans/`, so an agent-authored plan is not adjacent to its compiled output. Extending archived-pair discovery addresses the practical consequence (template examples reaching the compile prompt); unifying the two directories is a separate decision about authoring ergonomics.
