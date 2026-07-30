# opsx-plan manifest schema

Current as of controller commit `562962b` (July 2026). If the installed
controller looks newer, re-derive rather than trusting this file — see
[Re-deriving from source](#re-deriving-from-source).

## Contents

- [Re-deriving from source](#re-deriving-from-source)
- [`[plan]` table](#plan-table)
- [`[[changes]]` entries](#changes-entries)
- [`[plan.git_delivery]`](#plangit_delivery)
- [Adapter defaults](#adapter-defaults)
- [Dependency semantics](#dependency-semantics)
- [Template substitution](#template-substitution)
- [Keys that do nothing](#keys-that-do-nothing)

## Re-deriving from source

Two places in `<controller>/orchestrator/opsx-plan.py` are authoritative:

- `load_plan()` — builds the config with one explicit `.get()` per key it
  reads. Anything not looked up here is ignored.
- `build_schema_guidance()` — emits the key tables as markdown, and is what
  `compile` feeds to the model.

```bash
# The keys the loader actually reads
sed -n '/^def load_plan/,/^def /p' $KF_OPSX_CONTROLLER/orchestrator/opsx-plan.py \
  | grep -oE '(plan|c)\.get\(\s*"[a-z_]+"'
```

## `[plan]` table

All optional; defaults shown.

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string | filename stem | Plan display name; also names the state file |
| `adapter` | string | `"opencode"` | `opencode`, `claude-code`, or `codex-cli` |
| `timeout_minutes` | float | `90` | Per-change stage timeout |
| `max_attempts` | int | `2` | Legacy drive retry ceiling |
| `max_rounds` | int | `5` | Implement–review loop ceiling |
| `no_progress_limit` | int | `2` | Consecutive no-progress rounds before failing |
| `escalate_after_review_fails` | int | `0` | Promote implement to escalation model after N failed reviews (round N+1) |
| `fast_checks` | list[str] | `[]` | Post-archive commands; all must pass |
| `check_timeout_minutes` | float | `15` | Timeout per fast check |
| `require_clean_tracked` | bool | `true` | Refuse to start on a dirty tracked tree |
| `notify_cmd` | string | `""` | Command invoked for run-event notifications |
| `plan_doc` | string | `""` | Source markdown, substituted as `{plan_doc}` |
| `create_invoke` | string | `""` | Authoring command for changes that do not exist |
| `create_timeout_minutes` | float | `30` | Create stage timeout |
| `create_max_attempts` | int | `2` | Create retry ceiling |
| `review_created` | bool | `true` | Require `opsx-plan accept` before driving a created change |
| `created_check` | string | `"openspec validate {change} --strict"` | Post-create validation |
| `invoke` | string | adapter default | Legacy single-command controller invocation |
| `state_file` | string | adapter default | Controller state file path |
| `implement_invoke` | string | adapter default | Direct implement command |
| `review_invoke` | string | adapter default | Direct review command |
| `archive_invoke` | string | adapter default | Direct archive command |
| `git_delivery` | table | `{}` | See below |

An unknown `adapter` is only an error when `invoke` and `state_file` are not
both supplied; with both, any adapter name works.

## `[[changes]]` entries

| Key | Type | Default | Description |
|---|---|---|---|
| `id` | string | **required** | Unique change slug; must match `openspec/changes/<id>/` |
| `phase` | int | none | Display grouping only — carries no ordering semantics |
| `depends_on` | list[str] | `[]` | Ids that must complete first; the only ordering mechanism |
| `pause_before` | bool | `false` | Wait for `opsx-plan approve` before running |
| `enabled` | bool | `true` | `false` defers the change; shows as `skipped` |
| `timeout_minutes` | float | plan-level | Per-change override |
| `max_attempts` | int | plan-level | Per-change override |
| `create_invoke` | string | plan-level | Per-change authoring override |
| `create_max_attempts` | int | plan-level | Per-change override |

## `[plan.git_delivery]`

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Enable branch/PR delivery |
| `branch` | string | `"opsx/<name>"` | Delivery branch, derived from plan name if unset |
| `base_ref` | string | current branch | Base ref for the delivery branch |
| `create_pull_request` | bool | `false` | Push and open a PR after all changes complete |

`create_pull_request = true` requires `enabled = true`. Setting it without
`enabled` is a plan-load error — the one unknown-key-adjacent case that does
fail loudly.

## Adapter defaults

From `ADAPTER_DEFAULTS`:

| Adapter | `invoke` | `state_file` |
|---|---|---|
| `opencode` | `opencode run "/opsx-drive {change}"` | `.opencode/opsx-controller/{change}.json` |
| `claude-code` | `claude -p "/opsx-drive {change}"` | `.claude/opsx-controller/{change}.json` |
| `codex-cli` | `codex exec "$opsx-drive {change}"` | `.opsx-controller/{change}.json` |

Both `opencode` and `claude-code` define `implement_invoke` / `review_invoke`
/ `archive_invoke`, so a plan using either adapter takes the direct
implement-review-archive path with no manifest changes:

- `opencode run --agent opsx-{implementer,reviewer,archiver} --model
  "$OPSX_{IMPLEMENTER,REVIEWER,ARCHIVER}_MODEL"`
- `claude -p --agent opsx-{implementer,reviewer,archiver} --model
  "$OPSX_{IMPLEMENTER,REVIEWER,ARCHIVER}_MODEL" --permission-mode
  bypassPermissions --output-format json`

`codex-cli` defines neither and falls back to the single-command `invoke`
path (`/opsx-drive`), so a `codex-cli` plan runs the legacy nested-controller
loop rather than the direct stage loop unless an operator hand-writes all
three stage invokes in `[plan]`. The `$OPSX_*_MODEL` references in both sets
of defaults are resolved once per adapter when the plan loads, from
`~/.config/opsx-controller/models.toml` — see
`docs/opsx-plan-operator-workflow.md`'s Model Configuration section.

`/opsx-drive` is **deprecated**; `opsx-plan` logs a warning when a resolved
plan takes the nested-controller path.

For direct plan runs, `.opsx-plan/<plan-name>.state.json` is the
authoritative durable state — not `.opencode/opsx-controller/<change>.json`
or `.claude/opsx-controller/<change>.json`, which belong to manual
`/opsx-drive` invocations.

## Dependency semantics

- `depends_on` lists canonical change ids; each must appear as its own
  `[[changes]]` entry.
- No self-loops. The loader validates presence and acyclicity, then topologically
  sorts into `cfg["order"]`.
- `phase` does **not** create edges. Changes in the same phase are unordered
  relative to each other unless an explicit `depends_on` says otherwise. If
  sequence matters, state the edge.

## Template substitution

Invoke strings are formatted with exactly three names:

| Placeholder | Source |
|---|---|
| `{change}` | current change id |
| `{plan_doc}` | `[plan].plan_doc` |
| `{controller_model}` | `$OPSX_CONTROLLER_MODEL` |

Any other `{...}` raises at format time. Commands are split with `shlex.split`,
so quote inner arguments the way a shell would expect.

## Keys that do nothing

The loader ignores unrecognized keys silently. Two categories:

**Inert documentation** — written by `opsx-plan compile` for human readers:
`title`, `doc_type`, `status`, `owner`, `updated`, `source`, `purpose`,
`planning_principles`, `current_risks`, `phase_count`, `change_count`,
`schema_version`, and per-change `scope`, `out_of_scope`, `capabilities`,
`success_parameters`, `review`, `pause_reason`, `proposed_capability`. Harmless.

**Keys that look behavioral and are not** — the dangerous category, because they
read as working configuration:

| Key | Seen in | Reality |
|---|---|---|
| `depends_on_phase` | archived KF hardening plan | Phase-level dependencies are not implemented; use `depends_on` |
| `deferred_reason` | archived KF hardening plan | Not read; use a TOML comment |

`scripts/audit_manifest.py` separates these two categories and reports only the
second as findings.
