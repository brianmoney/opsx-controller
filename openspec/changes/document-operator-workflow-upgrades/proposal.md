## Why

The operator workflow upgrades are meant to remove the daily friction of using
`opsx-plan`, but the operator-facing documentation still reflects the older
flow where every command names a plan path explicitly, preflight checks are
implicit, and plan completion stops short of a documented branch-and-PR
handoff.

Once the Phase 1 through Phase 3 changes are complete, operators need one
current guide that explains the upgraded flow end to end: activate a plan,
preflight with `doctor`, run with optional time or cost budgets, clear manual
gates in batches, inspect logs, understand notifications, and finish on a
delivery branch that can open a pull request. Without that documentation,
operators would have to reconstruct the intended workflow from CLI help, plan
TOML examples, and multiple scattered READMEs.

## What Changes

- Add a dedicated operator workflow document for `opsx-plan` covering the
  activate-then-run workflow, `doctor`, plan-scoped branch and pull-request
  delivery configuration, budget flags, batched gate controls, log access, and
  notification hooks.
- Include a worked end-to-end example from `compile` through pull-request
  creation, using the final command names, config keys, default values, and
  fail-closed behaviors introduced by the earlier workflow-upgrade changes.
- Document the default-off behavior and one-run overrides for the new operator
  features, including explicit callouts for branch and PR suppression,
  budgeting, and active-plan resolution precedence.
- Update `orchestrator/README.md` so the main `opsx-plan` documentation points
  operators to the upgraded workflow and reflects the final command surface.
- Update the repository `README.md` so the root project documentation points to
  the operator workflow guide for plan-level execution.

No runtime behavior, specs, adapters, or installer logic change here. This is a
documentation-only change that depends on the earlier implementation work being
finished so the docs can describe the final interface accurately.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `plan-operator-cli`: Adds end-to-end operator workflow documentation for the
  active-plan, doctor, budgeting, gate, log, and notification features. No
  functional requirements change.
- `plan-git-delivery`: Documents the final branch-delivery and pull-request
  handoff workflow, including default-off behavior, fail-closed guards, and
  per-run overrides. No functional requirements change.

## Impact

- Affected specs: `openspec/specs/plan-operator-cli/spec.md` and
  `openspec/specs/plan-git-delivery/spec.md`.
- New file later: a dedicated `opsx-plan` operator workflow document in the
  repo.
- Modified files later: `orchestrator/README.md` and `README.md`.
- Verification later: documentation review for command/config accuracy and
  `openspec validate document-operator-workflow-upgrades --strict`.
- Out of scope here: adapter-specific tutorials outside OpenCode, marketing
  copy, or any code or spec changes.
