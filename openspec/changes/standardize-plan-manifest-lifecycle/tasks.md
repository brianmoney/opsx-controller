## 1. Manifest serialization

- [x] 1.1 Extract the `.opsx-plan/` directory and self-ignoring `.gitignore` bootstrap currently inlined in `write_active_plan` into `ensure_opsx_plan_dir(repo)`, and call it from `write_active_plan`
- [x] 1.2 Add `single_change_manifest_path(repo, change_id)` returning `repo/".opsx-plan"/"plans"/f"run-{change_id}.toml"`
- [x] 1.3 Add `render_single_change_manifest(cfg)` emitting one `[plan]` table and one `[[changes]]` table from the dict `build_single_change_config` returns, reusing the existing `_escape_toml_value` helper
- [x] 1.4 Emit `review_created = false` explicitly, since the plan loader defaults it to `True` while `build_single_change_config` sets it to `False`; audit the remaining fields for any other loader-default divergence
- [x] 1.5 Add `write_single_change_manifest(repo, change_id, cfg)` that writes to a temp sibling, loads it through `load_plan`, compares the loaded config against the synthesized config, and only then `os.replace`s it into position
- [x] 1.6 Raise `PlanError` naming the diverging field(s) and unlink the temp file when the round-trip comparison fails

## 2. Wire the manifest into single-change runs

- [x] 2.1 Call `write_single_change_manifest` in `cmd_run_one` after the `require_clean_tracked` guard and before `reconcile`, so a refused run leaves no manifest
- [x] 2.2 Write the manifest from the config before `skip_warning`/`skip_suggestion` are attached, since those are runtime flags rather than plan-schema fields
- [x] 2.3 Print the `opsx-plan report` and `opsx-plan dashboard` invocations targeting the derived manifest when the run finishes
- [x] 2.4 Confirm `cmd_run_one` never calls `write_active_plan`, and that the `opsx-run` `argv[0]` dispatch path needs no change because the write lives inside `cmd_run_one`

## 3. Report and dashboard targeting

- [x] 3.1 Add `--for-change <id>` to the `report` and `dashboard` parsers, mutually exclusive with the positional `plan` argument
- [x] 3.2 Resolve `--for-change` to the derived manifest in `cmd_report` and `cmd_dashboard` before `resolve_plan` runs, when that manifest exists
- [x] 3.3 Fall back to the plan name `run-<id>` with `load_plan` skipped when the manifest is absent but `.opsx-plan/run-<id>.state.json` exists
- [x] 3.4 Exit with an error naming the change id when neither the manifest nor the state file exists, rather than emitting an empty report or dashboard
- [x] 3.5 Leave the existing `--change` filter behavior untouched

## 4. Manifest placement standardization

- [x] 4.1 Make `compile`'s `-o/--output` optional and default it to `openspec/plans/<source-stem>.toml`, keeping the existing overwrite guard that requires `--force`
- [x] 4.2 Confirm the defaulted output is auto-activated on success exactly as an explicit `-o` path is
- [x] 4.3 Extend `discover_template_pairs` to list top-level `openspec/plans/` pairs first and `openspec/plans/archived/` pairs second, without recursing further

## 5. Canonical sample plan

- [x] 5.1 Author `orchestrator/samples/sample-plan.md` as a realistic phased implementation plan whose structure exercises phases, dependency edges, and a gate
- [x] 5.2 Author `orchestrator/samples/sample-plan.toml` as the manifest that plan compiles to, covering the documented `[plan]` and `[[changes]]` field surface with no keys the loader ignores
- [x] 5.3 Add `resolve_sample_plan_pair()` probing `~/.local/lib/opsx-controller/samples` then `<checkout>/orchestrator/samples`, mirroring the `_SCRIPT_ROOT` / `_RUNTIME_ROOTS` pattern at the top of the script, returning `None` when neither exists
- [x] 5.4 Include the canonical pair in `build_compile_prompt` ahead of repository pairs, and proceed without the section when resolution returns `None`
- [x] 5.5 Omit the repository reference section entirely when no repo pairs exist, replacing the current "No `openspec/plans/*.md` template plan pairs were found" fallback text
- [x] 5.6 Add `samples/` deployment to `scripts/install-orchestrator.sh`, replacing it on repeated installs like the runtime libraries
- [x] 5.7 Delete `orchestrator/plan.example.toml` and repoint its four references in `orchestrator/README.md`, `docs/opsx-plan-operator-workflow.md`, `skills/opsx-plan-manifest/SKILL.md`, and `skills/opsx-plan-manifest/references/worked-example.md`
- [x] 5.8 Revise the "Trusting example manifests" failure mode in `skills/opsx-plan-manifest/SKILL.md`, which becomes obsolete for the sample once it is test-verified

## 6. Plan archival command

- [x] 6.1 Add the `archive-plan` subparser and `cmd_archive_plan`, taking a manifest path
- [x] 6.2 Refuse targets that are missing, already under `openspec/plans/archived/`, or outside `openspec/plans/`, before moving anything
- [x] 6.3 Move the `.toml` and the sibling `.md` when present into `openspec/plans/archived/`, using `git mv` for tracked files (determined via `git ls-files`) and a plain rename otherwise
- [x] 6.4 Clear the active-plan pointer when it referenced the archived plan, report that it was cleared, and never repoint it at the archived copy
- [x] 6.5 Report the moved paths and that the move still needs committing; do not create a commit

## 7. Tests

- [x] 7.1 Assert `render_single_change_manifest` round-trips through `load_plan` to a config equal to `build_single_change_config`, with an explicit assertion that `review_created` is `False`
- [x] 7.2 Assert a divergent serialization fails the write, leaves no manifest, and removes the temp file
- [x] 7.3 Assert `cmd_run_one` writes `.opsx-plan/plans/run-<id>.toml` and leaves the active-plan pointer untouched
- [x] 7.4 Assert no manifest is written when the dirty-worktree guard rejects the run
- [x] 7.5 Assert `report --for-change` resolves via the manifest, and via the state-file fallback when the manifest is absent
- [x] 7.6 Assert `report --for-change` on an unknown change id errors, and that combining `--for-change` with a positional plan is a usage error
- [x] 7.7 Assert `archive-plan` moves both files, clears a matching pointer, leaves a non-matching pointer intact, and refuses a double archive
- [x] 7.8 Assert `compile` with no `-o` writes and activates `openspec/plans/<stem>.toml`
- [x] 7.9 Assert `discover_template_pairs` returns archived pairs and orders non-archived pairs first
- [x] 7.10 Assert the shipped `sample-plan.toml` loads through `load_plan` and yields the changes, dependency edges, and gates `sample-plan.md` describes
- [x] 7.11 Assert the sample exercises the documented `[plan]` and `[[changes]]` field surface and carries no keys the loader ignores, so loader drift fails the suite
- [x] 7.12 Assert `build_compile_prompt` includes the canonical pair in a repository with no plans, and emits no "no pairs found" text
- [x] 7.13 Assert the canonical pair is ordered ahead of repository pairs, and that an unresolvable sample degrades without raising
- [x] 7.14 Run `python -m pytest tests/orchestrator/test_opsx_plan.py -q` and confirm the full suite passes

## 8. Documentation

- [x] 8.1 Update the `opsx-run` and report/dashboard sections of `docs/opsx-plan-operator-workflow.md` to cover derived manifests and `--for-change`
- [x] 8.2 Document `archive-plan` and the `openspec/plans/` + `openspec/plans/archived/` convention in `orchestrator/README.md`
- [x] 8.3 Update `README.md` and `orchestrator/README.md` compile examples to the shorter form without `-o`
- [x] 8.4 Leave the `docs/plans/` default for authored markdown in `claude-code-plan-authoring` unchanged, as scoped in the proposal

## 9. Verification

- [x] 9.1 Re-run `scripts/install-orchestrator.sh` so `opsx-plan`/`opsx-run` execute the updated source, confirm no tracked `.pyc` files shadow it, and confirm `~/.local/lib/opsx-controller/samples/` now exists
- [x] 9.2 Build a compile prompt from an installed run against a repository with no plans and confirm the canonical sample is present and the "no pairs found" text is gone
- [x] 9.3 Run `opsx-run <change-id>` end to end, then confirm the derived manifest exists, the active-plan pointer is unchanged, and `opsx-plan report` by path and by `--for-change` agree
- [x] 9.4 Generate a dashboard with `opsx-plan dashboard --for-change <change-id>` and confirm the HTML renders
- [x] 9.5 Exercise the fallback path against an existing pre-change run namespace such as `run-add-adapter-aware-plan-compilation`, whose telemetry is already on disk with no manifest
- [x] 9.6 Compile a plan with no `-o`, then archive the pair with `archive-plan` and confirm the pointer is cleared
- [x] 9.7 Reset this repository's stale active-plan pointer, which currently references `openspec/plans/plan-fix-claude.toml` after that file moved to `archived/`
