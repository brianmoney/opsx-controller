## OpenSpec Controller Workflow

The shared plan-authoring reference is the single source of truth for
compilable plan documents. Read it first when you need the full machine-read
convention: `.claude/opsx-controller/plan-authoring.md` (project-first)
or `~/.claude/opsx-controller/plan-authoring.md` (global fallback).

### Upstream OpenSpec (per-change)

Upstream OpenSpec provides per-change commands:

- `/opsx:propose` — propose requirements and spec deltas for one change
- `/opsx:apply` — implement a single accepted change
- `/opsx:archive` — archive a completed change

These operate on individual OpenSpec change artifacts.

### opsx-controller (plan-level)

The controller provides plan-level orchestration:

- `opsx-plan` — compile, run, and report on multi-change implementation plans.
  The CLI entrypoint is installed at `~/.local/bin/opsx-plan`.
- `opsx-run` — manual single-change implement-review-archive loop for Claude
  Code. Supports exactly one change per run.

Durable controller state lives under `.claude/opsx-controller/<change-id>.json`.
The controller uses the fixed agents `opsx-implementer`, `opsx-reviewer`, and
`opsx-archiver`.
After editing `.claude/agents/`, restart Claude Code so the updated workflow
is loaded.

### Plan Authoring

Use `/opsx-plan <planning request>` to author a phased implementation-plan
markdown document. The skill reads the shared reference, validates the
request, delegates to the `opsx-plan-author` agent, and reports whether
Claude Code-backed compile self-checking ran. Detailed plan conventions
(structure, dependency rules, compile semantics) live in the
plan-authoring reference, not in the skill or agent body.
