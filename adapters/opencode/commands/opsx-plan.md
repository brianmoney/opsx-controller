---
description: Author a phased OpenSpec implementation plan document that `opsx-plan compile` can convert into a runnable plan manifest
agent: build
---

Author exactly one phased implementation plan markdown document for this
repository.

Resolved inputs:

- Planning request: `$ARGUMENTS`

Entry rules:

- If the planning request is empty, stop and report that
  `/opsx-plan <what to plan, with source material references>` is required.
- Resolve the output path: use a path named in the request; otherwise write to
  `docs/plans/<kebab-case-topic>-plan.md`.
- If the output file already exists, stop and report it, unless the request
  explicitly says to replace or revise it.
- Do not author more than one plan document in a single invocation.
- Declare the plan's blocking threshold in frontmatter as
  `acceptance: poc | phase | release`. Use `poc` only for a bounded proof of
  concept, `phase` when phase gates are the acceptance unit, and `release`
  when release qualification is in scope. Do not omit it for a release plan;
  the default is `poc`.
- Declare `human_gates` in the frontmatter when the plan contains an
  operator-gated change. The value is a list of exact change slugs, and every
  listed slug must also appear as a `### Change:` heading in this document.
  Omit the field, or declare an empty list, when no change is operator-gated.
  A change listed in `human_gates` is a stop for the model-level
  `opsx-orchestrator` run; it is not a compiled-manifest `pause_before` gate,
  so do not omit it on the assumption that `## Suggested Manual Gates` covers
  it.
- Do not edit the plan to record a run-level gate decision. An explicit
  `HUMAN_GATES` value passed to a model-level run replaces the frontmatter
  list for that run and is a runtime input, not an authoring edit.
- Size each change for one implement-review-archive loop before implementation,
  and record the sizing basis in the change's scope/success parameters. Do not
  leave an oversized change for corrective rounds to discover.
- Declare `split_authorizations` only when the plan preauthorizes an exact named
  replacement for an oversized change. Every entry must name one exact parent,
  two or more exact child slugs, each child's dependencies, an obligation map
  assigning every parent obligation to exactly one child, and an aggregate exit
  proving the parent's original success parameters after all children archive.
  Child slugs must not collide with active or archived changes, and the split
  must leave the phase, acceptance tier, gates, and human boundary unchanged.
  Omit the field when no exact replacement is authorized.
- `split_authorizations` is model-level plan data read by
  `opsx-orchestrator`. It adds no compiled-manifest key, `opsx-plan compile`
  does not read it, and it must not be expressed as conditional prose or a
  conditional DAG. When an entry is selected, the tracked plan contains only
  the chosen nodes.

Before writing, read:

1. `AGENTS.md` if it exists.
2. Any source material referenced in the request (review findings, design
   notes, attached files).
3. The installed plan-authoring reference:
   `.opencode/opsx-controller/plan-authoring.md` (project-first) or
   `~/.config/opencode/opsx-controller/plan-authoring.md` (global fallback).
   This reference contains the full machine-read compile convention,
   document structure, dependency rules, current OpenSpec facts, and
   plan-quality heuristics. Follow it exactly — a mis-stated dependency
   line becomes a wrong edge in an unattended automation DAG.
4. If neither the project-level nor the global `plan-authoring.md` exists,
   stop and report that the shared plan-authoring guidance is unavailable;
   do not author from memory, do not reproduce a convention, and do not
   claim the reference was read.
5. The existing capability list under `openspec/specs/` so capability
   references are real and proposed capabilities are genuinely new.
6. Existing change ids under `openspec/changes/` and
   `openspec/changes/archive/` so new slugs do not collide.

Compile self-check:

- If `opsx-plan` is available on PATH, run
  `opsx-plan compile <doc> -o /tmp/opsx-plan-selfcheck.toml --force` and
  verify the compile succeeds and produces the expected changes; fix any
  missing dependencies or malformed entries and rerun until clean.
- If the compiler is not available, state that in the final response so the
  operator runs it manually.

Self-verification before the final response:

1. Re-scan every `**Depends on:**` paragraph against the compile convention
   in the plan-authoring reference. Confirm no unintended backticked slug
   or `Phase N` reference appears in any of them.
2. Confirm the frontmatter declares `acceptance` and that it matches the
   plan's stated gates: `poc` for a bounded proof of concept, `phase` for
   phase-gated delivery, `release` for release qualification.
3. Verify every `human_gates` entry against the document: each slug must match
   a `### Change:` heading exactly. A slug that matches no heading is an
   authoring defect — fix the entry or drop the gate before reporting the plan
   as complete, and never leave an unknown slug for the orchestrator to fail on
   at preflight.
4. Verify every `split_authorizations` entry: each parent and child slug must
   match an exact `### Change:` heading or a declared dependency, no child collides
   with an active or archived change, every parent obligation appears exactly
   once in a child's `obligation_map`, the dependency remap preserves every
   predecessor and successor, and `aggregate_exit` proves the parent's original
   success parameters. A defective entry is an authoring defect; fix it or drop
   the authorization.
5. Report whether the compile self-check ran and succeeded or why it was
   unavailable.

Final response requirements:

- Do not repeat the document body.
- Report: the output path, the declared `acceptance` tier, phase and change
  counts, proposed capabilities, the declared `human_gates` (or that the plan
  declares none), the declared `split_authorizations` (or that the plan
  declares none), the compile self-check result (or that it was unavailable),
  and the suggested manual `pause_before` gates from the document's
  `## Suggested Manual Gates` section.
- If compile self-checking was unavailable, explicitly say the markdown was
  authored but not compiled.
- Remind the operator to review the compiled DAG with
  `opsx-plan run <plan> --dry-run` before any unattended run.
