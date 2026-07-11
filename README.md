# opsx-controller

Portable OpenSpec controller workflow with a shared core contract and client-
specific adapters.

The goal is to make one accepted OpenSpec change easy to drive through a strict
implement, review, and archive loop while keeping the workflow flexible enough
to package for different coding clients.

## Layout

- `core/`: client-neutral controller contract, state schema, and phase protocol
- `orchestrator/`: `opsx-plan` deterministic plan-level orchestrator
- `docs/`: operator workflow and benchmarking guides
- `adapters/opencode/`: OpenCode commands, agents, installer, support files,
  and templates
- `adapters/claude-code/`: Claude Code skill, phase agents, installer, support
  files, and templates
- `adapters/codex-cli/`: Codex CLI skill, phase agents, installer, support
  files, templates, and plugin manifest
- `plugins/opsx-controller/`: Claude Code plugin package for `--plugin-dir` and
  marketplace packaging
- `skills/opsx-controller/`: Vercel `npx skill` package for discovery and
  guided use

## Core Workflow

The shared workflow contract is client-neutral:

- supports exactly one OpenSpec change per run
- persists durable per-change state
- loops implement -> review -> implement until review is clean
- treats any critical, warning, or note finding as blocking
- auto-archives only after a fresh zero-finding review
- fails closed when archive scope or phase output is ambiguous

Start with:

- `core/controller-contract.md`
- `core/state-schema.md`
- `core/phase-protocol.md`

## Plan-Level Orchestrator

`opsx-plan` drives multi-change OpenSpec plans from compile through archive,
with preflight checks, budget controls, manual gates, log inspection,
cost-tracking telemetry, and branch/PR delivery. For single changes, `opsx-run`
skips the plan manifest.

- [**Operator Workflow Guide**](docs/opsx-plan-operator-workflow.md) — the
  full operator-facing workflow: activation, `doctor`, budgets, gates, logs,
  notifications, and branch/PR delivery.
- [**Model Efficiency Workflow**](core/model-efficiency-workflow.md) — how to
  benchmark model choices using telemetry, reports, and dashboards.
- [`orchestrator/README.md`](orchestrator/README.md) — technical reference:
  manifest schema, execution model, retry policy, and adapter invocation.

## OpenCode Adapter

What it contains:

- `adapters/opencode/commands/opsx-drive.md`: main slash command entrypoint
- `adapters/opencode/agents/opsx-controller.md`: controller/orchestrator
- `adapters/opencode/agents/opsx-implementer.md`: implementation round agent
- `adapters/opencode/agents/opsx-reviewer.md`: strict reviewer agent
- `adapters/opencode/agents/opsx-archiver.md`: non-interactive archiver agent
- `adapters/opencode/commands/opsx-review.md`: review prompt used by the
  controller's strict review phase
- `adapters/opencode/commands/opsx-archive-no-prompt.md`: deprecated archive
  helper stub that fails closed and points users to `/opsx-drive`
- `adapters/opencode/commands/opsx-verify-auto.md`: legacy verifier helper
- `adapters/opencode/support/opsx-controller-state-README.md`: state contract
- `adapters/opencode/templates/project/`: host-project setup snippets
- `adapters/opencode/install.sh`: OpenCode installer

Requirements:

- OpenCode
- OpenSpec CLI available in the shell
- a host project that already uses OpenSpec
- repo-specific guidance in the host project's `AGENTS.md`
- global OpenSpec phase prompts already installed as OpenCode slash commands:
  - `/opsx-apply`
  - `/opsx-verify`
  - `/opsx-archive`

Install globally:

```bash
set -a
source .env
set +a
bash adapters/opencode/install.sh --global
```

Install into one project:

```bash
set -a
source .env
set +a
bash adapters/opencode/install.sh --project /path/to/project
```

Required model environment variables for the OpenCode agents:

```bash
cp .env.example .env
set -a
source .env
set +a
```

- `OPSX_CONTROLLER_MODEL`
- `OPSX_IMPLEMENTER_MODEL`
- `OPSX_REVIEWER_MODEL`
- `OPSX_ARCHIVER_MODEL`

Project install behavior:

- copies commands into `<project>/.opencode/commands/`
- copies agents into `<project>/.opencode/agents/`
- installs the controller state contract at
  `<project>/.opencode/opsx-controller/README.md`
- ensures `<project>/.opencode/.gitignore` ignores `opsx-controller/*.json`
- creates `<project>/.opencode/opencode.json` with the watcher-ignore snippet
  only when the project does not already have any OpenCode config

If the project already has `opencode.json`, `opencode.jsonc`, or
`.opencode/opencode.json`, merge
`adapters/opencode/templates/project/opencode.json.snippet.json` manually.

The OpenCode adapter installer resolves the agent `model` values from those env
vars and writes concrete `provider/model` values into the installed Markdown
agent files. Re-run the installer after changing any `OPSX_*_MODEL` value.

Usage from the host project root:

```text
/opsx-drive <change-id>
```

If you want the host repo instructions to advertise the controller path, merge
`adapters/opencode/templates/project/AGENTS.snippet.md` into that project's
`AGENTS.md`.

## Claude Code Adapter

What it contains:

- `adapters/claude-code/skills/opsx-drive/SKILL.md`: main Claude Code slash
  command entrypoint
- `adapters/claude-code/agents/opsx-implementer.md`: implementation phase agent
- `adapters/claude-code/agents/opsx-reviewer.md`: strict review phase agent
- `adapters/claude-code/agents/opsx-archiver.md`: archive phase agent
- `adapters/claude-code/support/opsx-controller-state-README.md`: state contract
- `adapters/claude-code/templates/project/`: host-project setup snippets
- `adapters/claude-code/install.sh`: Claude Code installer

Requirements:

- Claude Code
- OpenSpec CLI available in the shell
- a host project that already uses OpenSpec
- repo guidance in `CLAUDE.md`, `AGENTS.md`, or both

Install globally:

```bash
bash adapters/claude-code/install.sh --global
```

Install into one project:

```bash
bash adapters/claude-code/install.sh --project /path/to/project
```

Project install behavior:

- copies skills into `<project>/.claude/skills/`
- copies agents into `<project>/.claude/agents/`
- installs the controller state contract at
  `<project>/.claude/opsx-controller/README.md`
- ensures `<project>/.claude/.gitignore` ignores `opsx-controller/*.json`

Usage from the host project root:

```text
/opsx-drive <change-id>
```

If you want the host repo instructions to advertise the controller path, merge
`adapters/claude-code/templates/project/CLAUDE.snippet.md` into that project's
`CLAUDE.md`.

## Codex CLI Adapter

What it contains:

- `adapters/codex-cli/skills/opsx-drive/SKILL.md`: controller skill with required YAML frontmatter
- `adapters/codex-cli/skills/opsx-drive/agents/openai.yaml`: optional Codex UI metadata
- `adapters/codex-cli/agents/opsx-implementer.toml`: implementation phase agent
- `adapters/codex-cli/agents/opsx-reviewer.toml`: strict review phase agent
- `adapters/codex-cli/agents/opsx-archiver.toml`: archive phase agent
- `adapters/codex-cli/support/opsx-controller-state-README.md`: state contract
- `adapters/codex-cli/templates/project/`: host-project setup snippets
- `adapters/codex-cli/install.sh`: Codex CLI installer
- `adapters/codex-cli/plugin/.codex-plugin/plugin.json`: marketplace manifest
- `adapters/codex-cli/plugin/skills/opsx-drive/`: plugin-scoped controller skill
- `adapters/codex-cli/plugin/agents/`: plugin-scoped phase agents

Requirements:

- OpenAI Codex CLI
- OpenSpec CLI available in the shell
- a host project that already uses OpenSpec
- repo guidance in `AGENTS.md`
- Codex CLI configured with `agents.max_depth >= 1` for subagent dispatch

Install globally:

```bash
bash adapters/codex-cli/install.sh --global
```

Install into one project:

```bash
bash adapters/codex-cli/install.sh --project /path/to/project
```

Project install behavior:

- copies skill into `<project>/.agents/skills/opsx-drive/`
- copies agents into `<project>/.codex/agents/`
- installs the controller state contract at
  `<project>/.codex/opsx-controller/README.md`
- ensures `<project>/.codex/.gitignore` ignores `opsx-controller/*.json`

Usage from the host project root:

```text
$opsx-drive <change-id>
```

State path differs from other adapters: durable state files live at
`.opsx-controller/<change-id>.json` (project root) because Codex sandbox
protects the `.codex/` directory from agent writes.

If you want the host repo instructions to advertise the controller path, merge
`adapters/codex-cli/templates/project/AGENTS.snippet.md` into that project's
`AGENTS.md`.

### Codex Plugin

A self-contained plugin bundle at `adapters/codex-cli/plugin/` is ready for
Codex marketplace distribution.

Create the plugin bundle locally:

```bash
bash adapters/codex-cli/install.sh --plugin
```

The plugin includes the controller skill, phase agents, and a marketplace
manifest (`.codex-plugin/plugin.json`).

## Claude Code Plugin

This repo also includes a shareable Claude plugin at `plugins/opsx-controller/`.

Local development and testing:

```bash
claude --plugin-dir ./plugins/opsx-controller
```

Usage:

```text
/opsx-controller:opsx-drive <change-id>
```

Why use the plugin package:

- namespaced Claude skill for sharing across projects
- self-contained `skills/` and `agents/` layout
- ready to evolve toward marketplace distribution

## Vercel Skill Package

This repo also includes a Vercel skill package at `skills/opsx-controller/`.

Current scope:

- provides a discoverable skill wrapper around the shared workflow contract
- installs with Vercel's `npx skill` flow
- bundles self-contained reference docs for the core workflow and adapter usage

Example:

```bash
SKILL_BASE_URL="https://github.com/brianmoney/opsx-controller/tree/main" \
  npx skill skills/opsx-controller
```

It is a guidance package, not a full cross-client automated installer.

## Portability Notes

This repository is now organized so additional client adapters can be added
without changing the core controller semantics.

To support another coding client, map that client's packaging model onto the
same three phases:

- implement
- review
- archive

Keep the durable state contract, strict review gate, and explicit archive scope
behavior intact.
