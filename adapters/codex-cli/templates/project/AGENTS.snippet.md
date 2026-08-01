## OpenSpec Controller Workflow

The shared plan-authoring reference is the single source of truth for
compilable plan documents. Read it first when you need the full machine-read
convention: `.codex/opsx-controller/plan-authoring.md` (project-first)
or `~/.codex/opsx-controller/plan-authoring.md` (global fallback).

### Upstream OpenSpec (per-change)

Upstream OpenSpec provides per-change commands:

- `$openspec-propose` — propose requirements and spec deltas for one change
- `$openspec-apply` — implement a single accepted change
- `$openspec-archive` — archive a completed change

These operate on individual OpenSpec change artifacts.

### opsx-controller (plan-level)

The controller provides plan-level orchestration:

- `opsx-plan` — compile, run, and report on multi-change implementation plans.
  The CLI entrypoint is installed at `~/.local/bin/opsx-plan`.
- Plan-run is **unsupported** on Codex CLI. To work through a single change
  manually, use upstream `$openspec-apply` followed by review and
  `$openspec-archive`.

Durable controller state lives under `.codex/opsx-controller/<change-id>.json`.
The controller uses the fixed agents `opsx-implementer`, `opsx-reviewer`, and
`opsx-archiver`.
After editing `.codex/agents/`, restart Codex CLI so the updated workflow
is loaded.

### Plan Authoring

Use `$opsx-plan <planning request>` to author a phased implementation-plan
markdown document. The skill reads the shared reference, validates the
request, and reports whether a compile adapter was available for
self-checking. Because Codex plan-run is unsupported, plan execution is
performed via `opsx-plan run` using a supported adapter (OpenCode or Claude
Code) or by applying individual changes manually. Detailed plan conventions
(structure, dependency rules, compile semantics) live in the
plan-authoring reference, not in the skill body.
