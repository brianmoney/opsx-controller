## 1. Manifest serialization

- [ ] 1.1 Extract the `.opsx-plan/` directory and self-ignoring `.gitignore` bootstrap currently inlined in `write_active_plan` into `ensure_opsx_plan_dir(repo)`, and call it from `write_active_plan`
- [ ] 1.2 Add `single_change_manifest_path(repo, change_id)` returning `repo/".opsx-plan"/"plans"/f"run-{change_id}.toml"`
- [ ] 1.3 Add `render_single_change_manifest(cfg)` emitting one `[plan]` table and one `[[changes]]` table from the dict `build_single_change_config` returns, reusing the existing `_escape_toml_value` helper
- [ ] 1.4 Emit `review_created = false` explicitly, since the plan loader defaults it to `True` while `build_single_change_config` sets it to `False`; audit the remaining fields for any other loader-default divergence
- [ ] 1.5 Add `write_single_change_manifest(repo, change_id, cfg)` that writes to a temp sibling, loads it through `load_plan`, compares the loaded config against the synthesized config, and only then `os.replace`s it into position
- [ ] 1.6 Raise `PlanError` naming the diverging field(s) and unlink the temp file when the round-trip comparison fails

## 2. Wire the manifest into single-change runs

- [ ] 2.1 Call `write_single_change_manifest` in `cmd_run_one` after the `require_clean_tracked` guard and before `reconcile`, so a refused run leaves no manifest
- [ ] 2.2 Write the manifest from the config before `skip_warning`/`skip_suggestion` are attached, since those are runtime flags rather than plan-schema fields
- [ ] 2.3 Print the `opsx-plan report` and `opsx-plan dashboard` invocations targeting the derived manifest when the run finishes
- [ ] 2.4 Confirm `cmd_run_one` never calls `write_active_plan`, and that the `opsx-run` `argv[0]` dispatch path needs no change because the write lives inside `cmd_run_one`

## 3. Report and dashboard targeting

- [ ] 3.1 Add `--for-change <id>` to the `report` and `dashboard` parsers, mutually exclusive with the positional `plan` argument
- [ ] 3.2 Resolve `--for-change` to the derived manifest in `cmd_report` and `cmd_dashboard` before `resolve_plan` runs, when that manifest exists
- [ ] 3.3 Fall back to the plan name `run-<id>` with `load_plan` skipped when the manifest is absent but `.opsx-plan/run-<id>.state.json` exists
- [ ] 3.4 Exit with an error naming the change id when neither the manifest nor the state file exists, rather than emitting an empty report or dashboard
- [ ] 3.5 Leave the existing `--change` filter behavior untouched

## 4. Manifest placement standardization

- [ ] 4.1 Make `compile`'s `-o/--output` optional and default it to `openspec/plans/<source-stem>.toml`, keeping the existing overwrite guard that requires `--force`
- [ ] 4.2 Confirm the defaulted output is auto-activated on success exactly as an explicit `-o` path is
- [ ] 4.3 Extend `discover_template_pairs` to list top-level `openspec/plans/` pairs first and `openspec/plans/archived/` pairs second, without recursing further

## 5. Plan archival command

- [ ] 5.1 Add the `archive-plan` subparser and `cmd_archive_plan`, taking a manifest path
- [ ] 5.2 Refuse targets that are missing, already under `openspec/plans/archived/`, or outside `openspec/plans/`, before moving anything
- [ ] 5.3 Move the `.toml` and the sibling `.md` when present into `openspec/plans/archived/`, using `git mv` for tracked files (determined via `git ls-files`) and a plain rename otherwise
- [ ] 5.4 Clear the active-plan pointer when it referenced the archived plan, report that it was cleared, and never repoint it at the archived copy
- [ ] 5.5 Report the moved paths and that the move still needs committing; do not create a commit

## 6. Tests

- [ ] 6.1 Assert `render_single_change_manifest` round-trips through `load_plan` to a config equal to `build_single_change_config`, with an explicit assertion that `review_created` is `False`
- [ ] 6.2 Assert a divergent serialization fails the write, leaves no manifest, and removes the temp file
- [ ] 6.3 Assert `cmd_run_one` writes `.opsx-plan/plans/run-<id>.toml` and leaves the active-plan pointer untouched
- [ ] 6.4 Assert no manifest is written when the dirty-worktree guard rejects the run
- [ ] 6.5 Assert `report --for-change` resolves via the manifest, and via the state-file fallback when the manifest is absent
- [ ] 6.6 Assert `report --for-change` on an unknown change id errors, and that combining `--for-change` with a positional plan is a usage error
- [ ] 6.7 Assert `archive-plan` moves both files, clears a matching pointer, leaves a non-matching pointer intact, and refuses a double archive
- [ ] 6.8 Assert `compile` with no `-o` writes and activates `openspec/plans/<stem>.toml`
- [ ] 6.9 Assert `discover_template_pairs` returns archived pairs and orders non-archived pairs first
- [ ] 6.10 Run `python -m pytest tests/orchestrator/test_opsx_plan.py -q` and confirm the full suite passes

## 7. Documentation

- [ ] 7.1 Update the `opsx-run` and report/dashboard sections of `docs/opsx-plan-operator-workflow.md` to cover derived manifests and `--for-change`
- [ ] 7.2 Document `archive-plan` and the `openspec/plans/` + `openspec/plans/archived/` convention in `orchestrator/README.md`
- [ ] 7.3 Update `README.md` and `orchestrator/README.md` compile examples to the shorter form without `-o`
- [ ] 7.4 Leave the `docs/plans/` default for authored markdown in `claude-code-plan-authoring` unchanged, as scoped in the proposal

## 8. Verification

- [ ] 8.1 Re-run `scripts/install-orchestrator.sh` so `opsx-plan`/`opsx-run` execute the updated source, and confirm no tracked `.pyc` files shadow it
- [ ] 8.2 Run `opsx-run <change-id>` end to end, then confirm the derived manifest exists, the active-plan pointer is unchanged, and `opsx-plan report` by path and by `--for-change` agree
- [ ] 8.3 Generate a dashboard with `opsx-plan dashboard --for-change <change-id>` and confirm the HTML renders
- [ ] 8.4 Exercise the fallback path against an existing pre-change run namespace such as `run-add-adapter-aware-plan-compilation`, whose telemetry is already on disk with no manifest
- [ ] 8.5 Compile a plan with no `-o`, then archive the pair with `archive-plan` and confirm the pointer is cleared
- [ ] 8.6 Reset this repository's stale active-plan pointer, which currently references `openspec/plans/plan-fix-claude.toml` after that file moved to `archived/`
