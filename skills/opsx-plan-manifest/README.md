# opsx-plan-manifest skill package

Turns a markdown implementation plan into a runnable `opsx-plan` TOML manifest,
and audits existing manifests for drift.

This package is laid out so Vercel's `npx skill` can install it directly from
this repository.

```bash
SKILL_BASE_URL="https://github.com/brianmoney/opsx-controller/tree/main" \
  npx skill skills/opsx-plan-manifest
```

Contents:

- `SKILL.md` — main skill entrypoint: the authoring workflow and the reasoning
  behind each decision
- `references/schema.md` — manifest key tables, adapter defaults, dependency
  semantics, and the list of keys the loader silently ignores
- `references/worked-example.md` — a real five-change plan reduced to a
  two-change runnable graph
- `scripts/audit_manifest.py` — checks a manifest for silently-ignored keys and
  for change ids that do not match the repository

## The audit script standalone

Useful outside the skill workflow, on any existing manifest:

```bash
python3 scripts/audit_manifest.py <manifest.toml> --repo <repo-root>
```

Finds the controller via `$KF_OPSX_CONTROLLER`, `$OPSX_CONTROLLER`, or
`~/opsx-controller`; override with `--controller`. Exits 0 clean, 1 with
findings, 2 on a usage or IO error. Needs Python 3.11+ (or `tomli`) and nothing
else.

It complements rather than duplicates `opsx-plan status`: `status` validates the
graph (unique ids, known dependencies, no cycles), while the audit checks the
two things `status` cannot — whether any configured key is being silently
dropped, and whether each change id actually corresponds to unarchived work in
`openspec/`.

## Relationship to `opsx-plan compile`

`opsx-plan compile` converts markdown to TOML by invoking OpenCode with
`$OPSX_CONTROLLER_MODEL`. It reads only the markdown, so it cannot know what has
already shipped or what is blocked on an outside decision. This skill covers
that reconciliation, and works either from scratch or on top of compile's
output.
