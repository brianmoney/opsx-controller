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
2. Report whether the compile self-check ran and succeeded or why it was
   unavailable.

Final response requirements:

- Do not repeat the document body.
- Report: the output path, phase and change counts, proposed capabilities,
  the compile self-check result (or that it was unavailable), and the
  suggested manual `pause_before` gates from the document's
  `## Suggested Manual Gates` section.
- If compile self-checking was unavailable, explicitly say the markdown was
  authored but not compiled.
- Remind the operator to review the compiled DAG with
  `opsx-plan run <plan> --dry-run` before any unattended run.
