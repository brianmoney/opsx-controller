# opsx-controller

Portable OpenCode workflow files for driving one OpenSpec change through an
implement, review, and archive loop with durable controller state.

This package extracts the reusable `.opencode` workflow pieces so you can keep
them in their own repository and install them either:

- globally for OpenCode under `~/.config/opencode/`
- per project under `<project>/.opencode/`

## What this package contains

- `commands/opsx-drive.md`: main slash command entrypoint
- `agents/opsx-controller.md`: controller/orchestrator
- `agents/opsx-implementer.md`: implementation round agent
- `agents/opsx-reviewer.md`: strict reviewer agent
- `agents/opsx-archiver.md`: non-interactive archiver agent
- `commands/opsx-archive-no-prompt.md`: legacy archive helper retained for
  automation compatibility
- `commands/opsx-verify-auto.md`: legacy machine-readable verifier helper
- `support/opsx-controller-state-README.md`: state contract used by the
  controller
- `templates/project/`: small snippets for host-project setup
- `scripts/install.sh`: installs the workflow globally or into one project

## Requirements

- OpenCode
- OpenSpec CLI available in the shell
- A host project that already uses OpenSpec
- Repo-specific guidance in the host project's `AGENTS.md`
- Global phase prompts already installed as OpenCode slash commands:
  - `/opsx-apply`
  - `/opsx-review`
  - `/opsx-verify`
  - `/opsx-archive`

The extracted workflow intentionally keeps phase behavior anchored in those
global `/opsx-*` prompts. This repo owns orchestration, durable state, and the
machine-readable contracts between phases.

The agents resolve upstream phase prompt files from either of these locations:

- `~/.config/opencode/commands/*.md`
- `~/.config/opencode/command/*.md`

That fallback exists because both layouts are seen in the wild.

## Install

Install globally:

```bash
bash scripts/install.sh --global
```

Install into one project:

```bash
bash scripts/install.sh --project /path/to/project
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
`.opencode/opencode.json`, merge `templates/project/opencode.json.snippet.json`
manually.

## Usage

After install, restart OpenCode so it reloads commands and agents.

From the host project root:

```text
/opsx-drive <change-id>
```

Behavior:

- supports exactly one OpenSpec change per run
- persists state at `.opencode/opsx-controller/<change-id>.json`
- loops implement -> review -> implement until review is clean
- treats any critical, warning, or note finding as blocking
- auto-archives after a zero-finding review
- resumes blocked or interrupted runs from the saved state file

## Host-project expectations

This package is reusable, but not completely repo-agnostic. The host project is
still expected to supply:

- repo-specific validation and Git rules in `AGENTS.md`
- the relevant OpenSpec change under `openspec/changes/<change-id>/`
- any project-specific checks that the global `/opsx-verify` and
  `/opsx-archive` prompts rely on

If you want the host repo instructions to advertise the controller path, merge
`templates/project/AGENTS.snippet.md` into that project's `AGENTS.md`.

## Maintenance

- Treat `agents/` and `commands/` in this repo as the source of truth.
- Use `scripts/install.sh` to copy them into global or project OpenCode paths.
- Keep the portable prompts generic; put repo-specific rules in the host
  project's `AGENTS.md` and in the global `/opsx-*` phase prompts.
