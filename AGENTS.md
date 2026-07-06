# AGENTS.md

Repo-root guidance for automated agents (opsx implementer, reviewer, archiver)
and any coding assistant working in this repository.

## Project

`opsx-controller` is a portable OpenSpec controller workflow: a client-neutral
core contract (`core/`) plus client-specific adapters (`adapters/opencode/`,
`adapters/claude-code/`, `adapters/codex-cli/`) and the plan orchestrator
(`orchestrator/opsx-plan.py`). Start from `core/controller-contract.md`,
`core/state-schema.md`, and `core/phase-protocol.md`.

## Validation

- Tests: `python3 -m unittest tests/orchestrator/test_opsx_plan.py`
  (run the full suite for orchestrator changes; there is no repo `.venv`).
- OpenSpec: `openspec validate <change> --strict` for a single change, or
  `openspec validate --all`.

## Deploy after every change (required)

The commands, agents, and orchestrator that actually run are the **installed**
copies under `~/.config/opencode/` and `~/.local/bin/`, not the files in this
repo. Editing the repo does **not** change runtime behavior until you
re-install. A change can look "done" in git while stale code keeps running.

**After completing any change that touches `adapters/`, `orchestrator/`,
`plugins/`, or `skills/`, re-run the installer(s) to deploy it:**

```bash
bash adapters/opencode/install.sh --global --verify
bash adapters/claude-code/install.sh --global --verify   # if Claude Code adapter changed
bash adapters/codex-cli/install.sh --global --verify      # if Codex CLI adapter changed
```

Treat the re-install as part of "done": no change is complete until the
corresponding installer has been run and its `--verify` output is clean.

## Sandbox / filesystem discipline

Workers run headless under a sandbox that auto-rejects `external_directory`
access. Never search parent or external directories (e.g. do not `Glob`
`**/AGENTS.md` or read outside the repo root) — a rejected permission prompt
kills the worker before it can emit its result. Read exact, repo-relative or
`$HOME`-expanded paths and continue past any that do not exist.
