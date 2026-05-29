# opsx-controller

Portable OpenSpec controller workflow with a shared core contract and client-
specific adapters.

The goal is to make one accepted OpenSpec change easy to drive through a strict
implement, review, and archive loop while keeping the workflow flexible enough
to package for different coding clients.

## Layout

- `core/`: client-neutral controller contract, state schema, and phase protocol
- `adapters/opencode/`: OpenCode commands, agents, installer, support files,
  and templates
- `adapters/claude-code/`: Claude Code skill, phase agents, installer, support
  files, and templates
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

## OpenCode Adapter

What it contains:

- `adapters/opencode/commands/opsx-drive.md`: main slash command entrypoint
- `adapters/opencode/agents/opsx-controller.md`: controller/orchestrator
- `adapters/opencode/agents/opsx-implementer.md`: implementation round agent
- `adapters/opencode/agents/opsx-reviewer.md`: strict reviewer agent
- `adapters/opencode/agents/opsx-archiver.md`: non-interactive archiver agent
- `adapters/opencode/commands/opsx-archive-no-prompt.md`: legacy archive helper
- `adapters/opencode/commands/opsx-verify-auto.md`: legacy verifier helper
- `adapters/opencode/support/opsx-controller-state-README.md`: state contract
- `adapters/opencode/templates/project/`: host-project setup snippets
- `adapters/opencode/install.sh`: OpenCode installer

Requirements:

- OpenCode
- OpenSpec CLI available in the shell
- a host project that already uses OpenSpec
- repo-specific guidance in the host project's `AGENTS.md`
- global phase prompts already installed as OpenCode slash commands:
  - `/opsx-apply`
  - `/opsx-review`
  - `/opsx-verify`
  - `/opsx-archive`

Install globally:

```bash
bash adapters/opencode/install.sh --global
```

Install into one project:

```bash
bash adapters/opencode/install.sh --project /path/to/project
```

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
