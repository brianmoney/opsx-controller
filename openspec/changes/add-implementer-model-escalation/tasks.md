## 1. Optional model role

- [x] 1.1 In `lib/models/types.py`, split the role tuple into `ROLES` (unchanged four) and `OPTIONAL_ROLES = ("implementer_escalation",)`, add `ALL_ROLES = ROLES + OPTIONAL_ROLES`, and build `ROLE_ENV` from `ALL_ROLES` so it yields `OPSX_IMPLEMENTER_ESCALATION_MODEL`
- [x] 1.2 In `lib/models/resolver.py`, change the loops in `resolve()` (line ~101) and `validate()` (line ~148) to iterate `ALL_ROLES`, leaving the precedence ladder and unresolved handling unchanged
- [x] 1.3 Add resolver tests in `tests/lib/models/test_resolver.py`: escalation resolves from each of the four precedence layers; unresolved everywhere yields `source == "unresolved"` without raising; `validate()` reports adapter-syntax violations for the escalation identifier

## 2. Threshold configuration

- [x] 2.1 In `load_plan` (`orchestrator/opsx-plan.py` ~:275-309), parse `"escalate_after_review_fails": int(plan.get("escalate_after_review_fails", 0))` and raise `PlanError` naming the key on a negative value
- [x] 2.2 In `build_single_change_config` (~:379-402), mirror the default as `0`
- [x] 2.3 In `render_single_change_manifest` (~:460-467), serialize the key as an integer field
- [x] 2.4 Add the key to `_SERIALIZED_PLAN_KEYS` in `_compare_configs` (~:562-570) — without this the derived-manifest round trip diverges and `opsx-run` aborts
- [x] 2.5 Add a round-trip test near `SingleChangeConfigTests`: render → `load_plan` → `_compare_configs` with a non-zero threshold reports no divergence
- [x] 2.6 Add a loader test asserting the default is `0` when the key is absent and that a negative value raises `PlanError`

## 3. Fail-closed activation

- [x] 3.1 In `apply_model_env` (~:625-649), keep the hard-fail list as `ROLES`; export the escalation variable only when the role resolves, and leave it unset otherwise
- [x] 3.2 In `apply_model_env`, add the gate: when `cfg.get("escalate_after_review_fails", 0) > 0` and the escalation role is unresolved, raise `PlanError` naming `implementer_escalation` and pointing at `opsx-plan models show` / `models init`
- [x] 3.3 Add activation tests near `ModelResolutionWiringTests` (tests ~L1938): succeeds with escalation unresolved and threshold `0`; raises naming the role when threshold `> 0` and unresolved; exports the variable when resolved

## 4. Dispatch-time escalation

- [x] 4.1 Add a helper computing whether escalation is active for a dispatch: `threshold > 0 and (round - 1) >= threshold`
- [x] 4.2 In `run_direct_change` (~:2984-3150), before the implement dispatch, set `os.environ[ROLE_ENV["implementer"]]` to the escalation model when active and to the base implementer model when not — unconditionally on every implement dispatch, with no save/restore
- [x] 4.3 Add `"escalation": {"active": False, "activated_round": 0, "model": ""}` to `new_change_record` (~:761-785) and populate it at dispatch; confirm `merge_defaults` migrates pre-existing state files without a reset
- [x] 4.4 Add threshold tests near `SingleChangeRunnerTests` (tests ~L1321): with threshold `2`, the model seen by the implement dispatch is the base model in rounds 1-2 and the escalation model in round 3+; threshold `0` never escalates; escalation stays active in later rounds; review and archive dispatches are unaffected
- [x] 4.5 Add a test that a no-progress round does not advance escalation, and that a custom `implement_invoke` override still escalates
- [x] 4.6 Add a telemetry test near `DirectStageTelemetryTests` (tests ~L2643): an escalated implement round writes `model.model_id` equal to the escalation model

## 5. Operator surface

- [x] 5.1 `cmd_models_show` (~:7342) — iterate `ALL_ROLES` so the escalation row is printed, showing `(unresolved)` when absent
- [x] 5.2 `cmd_models_env` (~:7370-7380) — keep the non-zero-exit check scoped to `ROLES`; emit the escalation export only when resolved
- [x] 5.3 `cmd_models_init` (~:7402) — seed from `ALL_ROLES`
- [x] 5.4 Add tests for the three command behaviors, including that `models env` exits zero with an unresolved escalation role
- [x] 5.5 Add the key to `build_schema_guidance()` (~:4477-4512) so compiled manifests can set it, stating the default and the round mapping

## 6. Documentation and the retired trap

- [x] 6.1 `docs/opsx-plan-operator-workflow.md` — add `escalate_after_review_fails` to the key table (~:190-221) with the explicit round mapping (`N = 2` first escalates in round 3); **remove** the warning block at ~:308-321; document `OPSX_IMPLEMENTER_ESCALATION_MODEL` in "Model Configuration" (~:102-135)
- [x] 6.2 `skills/opsx-plan-manifest/references/schema.md` — add the key row (~:42-77) and **remove** the no-op row at ~:166
- [x] 6.3 `skills/opsx-plan-manifest/SKILL.md` (~:55-58) and `scripts/audit_manifest.py` (~:10) — drop the key from the behavioral-no-op list
- [x] 6.4 `models.example.toml` and `.env.example` — document the optional `implementer_escalation` role / `OPSX_IMPLEMENTER_ESCALATION_MODEL`
- [x] 6.5 `docs/adapters.md` (~:41-61) and `orchestrator/README.md` retry/failure policy (~:248-286) — describe escalation alongside `max_rounds` and `no_progress_limit`
- [x] 6.6 `orchestrator/samples/sample-plan.toml` (and its paired `.md`) — set the key so the canonical sample keeps covering the documented field surface, as `plan-manifest-lifecycle` requires

## 7. Verification

- [x] 7.1 Run `python3 -m unittest discover -t . -s tests` and `node tests/opencode/test-opsx-usage-emitter.js` — both clean
- [x] 7.2 Re-run `scripts/install-orchestrator.sh` so the runtime uses the updated copies rather than the working tree
- [ ] 7.3 Live e2e: run a small plan with `escalate_after_review_fails = 1` and a deliberately weak base implementer model; confirm from `.opsx-plan/logs/<cid>.implement.r2.*.log` and `.opsx-plan/telemetry/<plan>.jsonl` that round 2 dispatched the escalation model
- [ ] 7.4 From the same run, confirm the OpenCode `--model` flag actually overrides the model baked into `adapters/opencode/agents/opsx-implementer.md` — if it does not, stop and revisit the design, since escalation would be inert for that adapter
- [ ] 7.5 Inspect `opsx-plan report` output from the e2e run and record whether the leaderboard grouping in `lib/metrics/aggregator.py` (~:900-940) misattributes a run that spans two implementer models; open a follow-up change if it does
