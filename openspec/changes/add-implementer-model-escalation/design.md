## Context

The direct implement→review→archive loop lives in `run_direct_change` (`orchestrator/opsx-plan.py:2984-3150`). Model selection is entirely indirect: `apply_model_env` (`:625-649`) exports four `OPSX_<ROLE>_MODEL` variables once per process, and each stage's invoke string (`ADAPTER_DEFAULTS`, `:76-110`) references the matching variable, which `invoke_direct_stage` (`:2654-2684`) expands fail-closed at dispatch.

Only `apply_review_result` (`:2825-2888`) advances `r["round"]`, and only on a failed review. `apply_implement_result` (`:2740-2822`) tracks `no_progress_streak` separately against `no_progress_limit`. So round and review-failure count are already in lockstep.

`escalate_after_review_fails` is a key operators already write. `load_plan` builds `cfg` from an explicit `.get()` allowlist (`:275-332`), so unknown keys are dropped with no warning — which is why the key silently does nothing today, and why `docs/opsx-plan-operator-workflow.md:308-321`, `skills/opsx-plan-manifest/references/schema.md:166`, and `scripts/audit_manifest.py` all carry warnings about it.

Constraints: the orchestrator is a single dependency-free Python script; tests are stdlib `unittest`; the runtime executes from installed copies, so `scripts/install-orchestrator.sh` must be re-run before any live verification.

## Goals / Non-Goals

**Goals:**
- Promote the implement stage to a stronger model after N failed reviews, configured per plan.
- Configure the escalation model through the same per-adapter precedence ladder as every other role.
- Preserve today's behavior byte-for-byte when the feature is off.
- Work with per-plan custom `implement_invoke` overrides without requiring operators to write a second invoke string.
- Retire the "this key does nothing" documentation and auditor entry.

**Non-Goals:**
- Escalating the reviewer or archiver roles. Only implement escalates.
- De-escalating back to the base model after a subsequent success.
- Counting no-progress rounds toward the threshold — `no_progress_limit` remains the control for that failure mode.
- A per-change threshold override. Plan-level only, matching `max_rounds` and `no_progress_limit`.
- Changing the worker input block contract (`build_worker_input`, `:1089-1104`). The implementer is not told it has been escalated.
- Escalation for the deprecated nested `/opsx-drive` path, which has its own `attempts` counter.

## Decisions

### Optional fifth role rather than a fifth required role

`ROLES` in `lib/models/types.py:9` drives resolution, validation, activation, and the operator commands. Adding `implementer_escalation` to that tuple would get all of it for free — but `apply_model_env` raises `PlanError` when *any* role in `ROLES` is unresolved, so every existing installation would break the moment the tuple grew.

Chosen: split the tuple.

```python
ROLES: tuple[str, ...] = ("controller", "implementer", "reviewer", "archiver")
OPTIONAL_ROLES: tuple[str, ...] = ("implementer_escalation",)
ALL_ROLES: tuple[str, ...] = ROLES + OPTIONAL_ROLES
ROLE_ENV: dict[str, str] = {role: f"OPSX_{role.upper()}_MODEL" for role in ALL_ROLES}
```

`resolve()` and `validate()` iterate `ALL_ROLES`; `apply_model_env`'s hard-fail list stays `ROLES`. The existing `f"OPSX_{role.upper()}_MODEL"` derivation yields `OPSX_IMPLEMENTER_ESCALATION_MODEL` with no special case — which is why the role is named `implementer_escalation` and not, say, `escalation`.

*Alternative rejected:* a standalone config key outside the role system (e.g. `[plan].escalation_model`). It would bypass the per-adapter precedence ladder and identifier-syntax validation, and would not appear in `models show`.

### Escalate by re-setting `OPSX_IMPLEMENTER_MODEL`, not by a second invoke template

Chosen: before each implement dispatch, set `os.environ[ROLE_ENV["implementer"]]` to the escalation model or the base model, whichever applies.

Rationale:
- Works with any `implement_invoke`, including per-plan overrides, with no new config surface.
- Telemetry is correct for free: `_record_stage_telemetry` (`:2098`) re-expands `cfg["implement_invoke"]` against `os.environ` via `_best_effort_expand_invoke`, which will now resolve to the escalated model.
- No save/restore machinery is needed. Unlike the usage-sidecar env block (`:3039-3048`), the value is written from scratch on every implement dispatch and no other stage reads it, so there is no state to leak. This is the one deliberate exception to the "activation is process-wide" rule, and the spec delta calls it out explicitly.

*Alternative rejected:* an `implement_escalated_invoke` config key. It doubles the invoke surface, and an operator who overrode `implement_invoke` would have to remember to override both or silently lose their customization on escalated rounds.

*Alternative rejected:* rewriting the `--model` token in the argv after `shlex.split`. Fragile against `--model=X` forms, invoke strings that omit the flag, and adapters that pass the model some other way.

### Threshold semantics: `(round - 1) >= N`

`round` starts at 1 and only `apply_review_result` increments it, so `round - 1` *is* the number of failed reviews. Deriving the count avoids a second counter that could drift from `round`.

The threshold is nonetheless surfaced explicitly in state (`escalation.active`, `escalation.activated_round`, `escalation.model`) rather than left implicit, so an operator reading a state file can tell an escalated run from an un-escalated one without recomputing the arithmetic. `merge_defaults` (`:788-794`) migrates pre-existing state files.

Note the off-by-one in the archived manifest that inspired the key: it set `escalate_after_review_fails = 2` with the comment "promote after 3 failed reviews". This design escalates after exactly N, i.e. `N = 2` first escalates in round 3. The documentation must state the round mapping explicitly.

### Fail closed at plan-configuration time

`escalate_after_review_fails > 0` with an unresolved `implementer_escalation` role raises `PlanError` from `apply_model_env`, in the same shape as the existing unresolved-role error.

The alternative — warn and run un-escalated — was rejected by the user: it would reintroduce exactly the silently-ignored-key failure mode this change exists to remove. Failing at configuration time rather than at round N+1 also means the operator finds out in seconds instead of after an hour of burned rounds.

### `lib/install-common.sh` is deliberately untouched

`OPSX_MODEL_ROLES` (`lib/install-common.sh:69`) drives install-time `{env:OPSX_<ROLE>_MODEL}` substitution into agent frontmatter, which bakes a single value per agent file. Escalation is a dispatch-time swap through the CLI `--model` flag, so there is nothing for the installer to bake.

This rests on the `--model` flag winning over the installed agent's frontmatter. For `claude-code` the agents declare `model: inherit`, so the flag is unambiguously authoritative. For `opencode`, `adapters/opencode/agents/opsx-implementer.md:5` carries a baked value — the spec `plan-driven-opencode-execution` already asserts the flag is authoritative ("Explicit model argument drives telemetry attribution", "Model change applies without reinstalling"), but this is the one assumption unit tests cannot prove and live e2e verification must confirm.

## Risks / Trade-offs

- **The OpenCode `--model` flag might not override baked agent frontmatter** → the whole mechanism is inert for that adapter. Unit tests assert the dispatched argv, not the model the client actually uses, so this must be confirmed by a live run inspecting `.opsx-plan/telemetry/<plan>.jsonl` before the change is archived. If it fails, escalation needs an adapter-specific mechanism and this design must be revisited.
- **Leaderboard grouping splits escalated runs** → `lib/metrics/aggregator.py:900-940` groups by `(implementer_model, reviewer_model, archiver_model)`; a change whose rounds span two implementer models will now produce rows that no longer describe a single coherent configuration. Not addressed here; flagged for inspection against real report output, with a follow-up change if the attribution misleads.
- **Cost surprise** → escalation silently moves spend to a more expensive model mid-run. Mitigated by the existing `--budget-usd` pre-dispatch check (`:3008-3024`), which is evaluated before every stage and therefore already covers escalated rounds.
- **Forgetting `_SERIALIZED_PLAN_KEYS`** → `_compare_configs` (`:562-570`) hardcodes the round-trip key list, and omitting the new key makes `opsx-run` derived manifests diverge and abort. Covered by a dedicated round-trip test rather than by review attention.
- **Two spellings entering the codebase** → the user's original request said `IMPLEMENTOR`; the repo says `IMPLEMENTER` everywhere. Settled on `IMPLEMENTER`. No alias is provided, so a manifest written against the other spelling fails closed at load rather than silently running un-escalated.

## Migration Plan

No migration is required. With `escalate_after_review_fails` absent or `0` — the default, and the value the manifest-free single-change runner always synthesizes — resolution, activation, dispatch, and state are unchanged. Existing state files gain disabled escalation fields through `merge_defaults` on load, with no operator reset.

Rollback is reverting the change; no persisted artifact acquires a shape that older code cannot read, because `merge_defaults` and the `.get()`-allowlist loader both tolerate unknown extra keys.

## Open Questions

- Does the leaderboard need to record the escalated model explicitly, or is per-stage telemetry attribution sufficient? Deferred until the live run produces real report output.
