## Why

When the direct implement→review→archive loop keeps failing review, the orchestrator retries the *same* implementer model until `max_rounds` is exhausted — there is no way to promote to a stronger model after repeated failures. Manifests already in circulation try to express this with an `escalate_after_review_fails` key that does nothing; `docs/opsx-plan-operator-workflow.md` and the manifest auditor currently have to warn operators away from it. Making the key real turns a documented trap into working behavior.

## What Changes

- Add an optional fifth model role, `implementer_escalation`, resolved through the existing per-adapter precedence ladder and exported as `OPSX_IMPLEMENTER_ESCALATION_MODEL`. Unlike the four required roles, leaving it unresolved is not an error on its own.
- Add a plan-level manifest key `escalate_after_review_fails` (integer, default `0` = disabled). Once N reviews have failed for a change, subsequent implement dispatches use the escalation model instead of the base implementer model. Counting is review failures only — no-progress rounds do not count.
- Escalate by re-setting `OPSX_IMPLEMENTER_MODEL` deterministically before each implement dispatch, so per-plan custom `implement_invoke` strings keep working unchanged and telemetry attributes escalated rounds to the escalation model for free.
- Fail closed at plan load when `escalate_after_review_fails > 0` but no escalation model resolves, matching the existing unresolved-role error shape.
- Surface the new role in `opsx-plan models show`, `models env`, and `models init`.
- Persist escalation status in plan state for observability.
- Remove `escalate_after_review_fails` from the silently-ignored-key documentation and from the manifest auditor's no-op key list, and document it as a real key.

No breaking changes: with the key absent or `0`, resolution, activation, dispatch, and state are byte-for-byte the behavior that exists today.

## Capabilities

### New Capabilities
- `implementer-model-escalation`: when and how the orchestrator promotes the implement stage to a stronger model after repeated review failures — the threshold key, the counting rule, the dispatch-time model swap, fail-closed configuration validation, and the resulting state and telemetry attribution.

### Modified Capabilities
- `adapter-model-configuration`: the role set gains an optional role that participates in the precedence ladder, identifier-syntax validation, process-wide activation, and the operator inspect/seed commands — while the existing "unresolved roles fail closed" requirement stays scoped to the four required roles.

`plan-manifest-lifecycle` is deliberately **not** listed: its round-trip-verification and canonical-sample requirements are already field-agnostic ("differs … in any field", "exercises the documented field surface"), so adding a serialized plan key creates implementation obligations under those requirements without changing them.

## Impact

- `lib/models/types.py` — role tuples and the role→env mapping.
- `lib/models/resolver.py` — `resolve()` and `validate()` iterate the extended role set.
- `orchestrator/opsx-plan.py` — `load_plan`, `build_single_change_config`, `render_single_change_manifest`, `_compare_configs`, `apply_model_env`, `new_change_record`, `run_direct_change`, `build_schema_guidance`, `cmd_models_show` / `cmd_models_env` / `cmd_models_init`.
- Docs: `docs/opsx-plan-operator-workflow.md`, `docs/adapters.md`, `orchestrator/README.md`, `models.example.toml`, `.env.example`, `orchestrator/samples/sample-plan.toml`.
- Skill: `skills/opsx-plan-manifest/{SKILL.md,references/schema.md,scripts/audit_manifest.py}`.
- Tests: `tests/lib/models/test_resolver.py`, `tests/orchestrator/test_opsx_plan.py`.
- Not affected: `lib/install-common.sh` (`OPSX_MODEL_ROLES` drives install-time agent-frontmatter substitution, which bakes a single value; escalation is a dispatch-time swap).
