---
description: Author one phased OpenSpec implementation-plan markdown document in Claude Code and only claim compilation when the OpenCode-backed compiler self-check actually runs.
argument-hint: [planning-request]
disable-model-invocation: true
allowed-tools: Read, Glob, Grep, Bash, Agent(opsx-controller:opsx-plan-author)
---

Author exactly one phased implementation plan markdown document.

Resolved inputs:

- Planning request: `$0`
- Remaining request tokens: `$1 $2 $3 $4 $5 $6 $7 $8 $9`

Input rules:

- If the planning request is empty, stop and report that
  `/opsx-controller:opsx-plan <what to plan, with source material references>`
  is required.
- Do not author more than one plan document in a single invocation.

Workflow:

1. Read `CLAUDE.md` if it exists.
2. Read `AGENTS.md` if it exists.
3. Delegate the authoring work to the `opsx-controller:opsx-plan-author` agent.
4. Pass the full request as:
   - `PLANNING_REQUEST: $0 $1 $2 $3 $4 $5 $6 $7 $8 $9`
5. Return the agent's final result to the operator.

The authoring result must distinguish between:

- markdown authored and OpenCode-backed compile self-check passed
- markdown authored but compile self-check unavailable because `opsx-plan`
  and/or `OPSX_CONTROLLER_MODEL` was not configured

Never present markdown authoring as successful TOML compilation unless the
compile self-check actually ran and succeeded.
