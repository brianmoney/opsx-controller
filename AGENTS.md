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

- Tests: `python3 -m unittest discover -t . -s tests` and `node
  tests/opencode/test-opsx-usage-emitter.js`, both from the repository root
  (run both suites for orchestrator or adapter changes; both are
  stdlib/runtime only, so no repo `.venv` or `node_modules` is required). A
  new test package needs an `__init__.py` or discovery skips it silently.
- OpenSpec: `openspec validate <change> --strict` for a single change, or
  `openspec validate --all`.

## Maintainer Notes: deploy after every change

This section applies to the maintainer's own machine, after a change has
merged to `main`. It is not part of what a PR needs to satisfy, and a
contributor working in their own checkout should not run these installers.

The commands, agents, and orchestrator that actually run **on the
maintainer's machine** are the **installed** copies under
`~/.config/opencode/` and `~/.local/bin/`, not the files in this repo.
Editing the repo does **not** change the maintainer's runtime behavior until
they re-install. A merged change can look "done" in git while stale code
keeps running locally.

**After merging any change that touches `adapters/`, `orchestrator/`,
`plugins/`, `skills/`, or `scripts/`, the maintainer re-runs the
installer(s) to deploy it:**

```bash
bash adapters/opencode/install.sh --global --verify
bash adapters/claude-code/install.sh --global --verify   # if Claude Code adapter changed
bash adapters/codex-cli/install.sh --global --verify      # if Codex CLI adapter changed
```

Any global installer now deploys the shared `opsx-plan` and `opsx-run`
executables (via `scripts/install-orchestrator.sh`), so a maintainer only
needs to rerun one installer — the installed runtime location is the same
regardless of which adapter deployed it.

The orchestrator's implementation is split between the entrypoint
(`orchestrator/opsx-plan.py`) and the installed `lib/orchestrator/` runtime
package (see `orchestrator/README.md#source-layout`). Because of this, the
reinstall after an `orchestrator/` or `lib/orchestrator/` change is
**required, not merely recommended**: a stale installed runtime is missing
whole modules, not just running outdated logic, and commands like
`opsx-plan report` or `opsx-plan dashboard` fail outright — with a
diagnostic naming the missing package, per `opsx-plan doctor` — rather than
silently serving old behavior.

## Sandbox / filesystem discipline

Workers run headless under a sandbox that auto-rejects `external_directory`
access. Never search parent or external directories (e.g. do not `Glob`
`**/AGENTS.md` or read outside the repo root) — a rejected permission prompt
kills the worker before it can emit its result. Read exact, repo-relative or
`$HOME`-expanded paths and continue past any that do not exist.
