---
description: Author one phased OpenSpec implementation-plan markdown document in Claude Code and only claim compilation when the claude-code adapter compiler self-check actually runs.
argument-hint: [planning-request]
disable-model-invocation: true
allowed-tools: Read, Glob, Grep, Bash, Agent(opsx-controller:opsx-plan-author)
---

Author exactly one phased implementation plan markdown document.

Resolved inputs:

- Planning request: `$0`
- Remaining request tokens: `$1 $2 $3 $4 $5 $6 $7 $8 $9`

Entry rules:

- If the planning request is empty, stop and report that
  `/opsx-controller:opsx-plan <what to plan, with source material references>`
  is required.
- Do not author more than one plan document in a single invocation.

Workflow:

1. Read `CLAUDE.md` if it exists.
2. Read `AGENTS.md` if it exists.
3. Read the installed plan-authoring reference:
   `.claude/opsx-controller/plan-authoring.md` (project-first) or
   `~/.claude/opsx-controller/plan-authoring.md` (global fallback). This
   reference contains the full machine-read compile convention, document
   structure, dependency rules, current OpenSpec facts, and plan-quality
   heuristics. If neither the project-level nor the global
   `plan-authoring.md` exists, stop and report that the shared
   plan-authoring guidance is unavailable; do not author from memory, do
   not reproduce a convention, and do not claim the reference was read.
4. Delegate the authoring work to the `opsx-controller:opsx-plan-author`
   agent.
5. Pass the full request as:
   - `PLANNING_REQUEST: $0 $1 $2 $3 $4 $5 $6 $7 $8 $9`
6. Return the agent's final result to the operator.

Command namespaces:

- Upstream OpenSpec provides per-change commands (`/opsx:propose`,
  `/opsx:apply`, `/opsx:archive`).
- opsx-controller provides plan-level commands (`opsx-plan`, `opsx-run`).
  Use `opsx-run <change-id>` for the supported manual single-change loop.
  Do not reference the deleted `opsx-drive` workflow.

The authoring result must distinguish between:

- markdown authored and compile self-check passed (via
  `opsx-plan compile --adapter claude-code <doc> -o /tmp/opsx-plan-selfcheck.toml --force`)
- markdown authored but compile self-check unavailable because `opsx-plan`
  and/or a resolved `controller` model for `claude-code` was not configured

Never present markdown authoring as successful TOML compilation unless the
compile self-check actually ran and succeeded.
