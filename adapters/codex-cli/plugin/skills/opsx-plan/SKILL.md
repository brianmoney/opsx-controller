---
description: Author one phased OpenSpec implementation-plan markdown document and only claim compilation when a compile adapter self-check actually runs.
argument-hint: [planning-request]
disable-model-invocation: true
---

Author exactly one phased implementation plan markdown document.

Resolved inputs:

- Planning request: `$0`
- Remaining request tokens: `$1 $2 $3 $4 $5 $6 $7 $8 $9`

Entry rules:

- If the planning request is empty, stop and report that
  `$opsx-plan <what to plan, with source material references>` is required.
- Do not author more than one plan document in a single invocation.

Before writing, read:

1. `AGENTS.md` if it exists.
2. Any source material referenced in the request (review findings, design
   notes, attached files).
3. The installed plan-authoring reference:
   `.codex/opsx-controller/plan-authoring.md` (project-first) or
   `~/.codex/opsx-controller/plan-authoring.md` (global fallback). This
   reference contains the full machine-read compile convention, document
   structure, dependency rules, current OpenSpec facts, and plan-quality
   heuristics. Follow it exactly.
4. If neither the project-level nor the global `plan-authoring.md` exists,
   stop and report that the shared plan-authoring guidance is unavailable;
   do not author from memory, do not reproduce a convention, and do not
   claim the reference was read.
5. The existing capability list under `openspec/specs/`.
6. Existing change ids under `openspec/changes/` and
   `openspec/changes/archive/`.

Resolve the output path: use a path named in the request; otherwise write to
`docs/plans/<kebab-case-topic>-plan.md`. If the output file already exists,
stop and report it unless the request explicitly says to replace or revise it.

Compile self-check:

Codex plan-run is unsupported — there is no Codex adapter for plan execution.
The authored plan must be run via `opsx-plan run` using a supported adapter
(OpenCode or Claude Code), or individual changes must be applied manually
using upstream `$openspec-apply`.

- When `opsx-plan` is available on PATH and a supported adapter
  (OpenCode or Claude Code) has a resolved controller model, run
  `opsx-plan compile --adapter <adapter> <doc> -o /tmp/opsx-plan-selfcheck.toml --force`,
  verify success, and fix the source document if compilation exposes
  malformed structure or dependencies.
- When no compile adapter is available, report honestly: state that the
  markdown plan was authored but not compiled, explain that Codex has no
  plan-run adapter, and name the missing configuration.

Self-verification:

Re-scan every `**Depends on:**` paragraph against the compile convention in
the plan-authoring reference. Confirm no unintended backticked slug or
`Phase N` reference appears.

Final response requirements:

- Do not repeat the document body.
- Report: the output path, phase and change counts, proposed capabilities,
  the compile self-check result (or that no compile adapter was available),
  and the suggested manual `pause_before` gates.
- If no compile adapter was available, explicitly say the markdown was
  authored but not compiled, and that Codex plan-run is unsupported.
- Remind the operator to review the compiled DAG with
  `opsx-plan run <plan> --dry-run` before any unattended run using a
  supported adapter.
