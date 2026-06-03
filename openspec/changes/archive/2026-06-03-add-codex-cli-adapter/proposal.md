## Why

OpenAI Codex CLI is a rapidly growing AI coding agent (88k+ GitHub stars) with a subagent system, skill framework, and plugin marketplace — but no opsx-controller adapter exists for it. Adding a Codex adapter extends opsx-controller's reach to Codex users while following the established adapter pattern.

## What Changes

- Add `adapters/codex-cli/` directory with a complete adapter following the core contract
- **Skill entrypoint**: `skills/opsx-drive/SKILL.md` — prompt-driven controller using `spawn_agent`/`wait_agent` to orchestrate the implement→review→archive loop
- **Phase agents**: Three TOML agent definitions (implementer, reviewer, archiver) under `agents/` with appropriate sandbox modes and model bindings
- **Install script**: `install.sh` supporting `--global`, `--project`, and `--plugin` modes
- **Plugin manifest**: `plugin/.codex-plugin/plugin.json` for Codex marketplace distribution
- **State path divergence**: State files use `.opsx-controller/<change>.json` instead of `.codex/opsx-controller/` because Codex sandbox protects the `.codex/` directory from writes

## Capabilities

### New Capabilities

- `codex-cli-adapter`: A portable adapter that maps the opsx-controller workflow (implement→review→archive) onto Codex CLI's native primitives — skills for the entrypoint, TOML-based custom agents for phase runners, and prompt-driven orchestration via `spawn_agent`/`wait_agent` tools

### Modified Capabilities

<!-- No existing specs to modify — this is the first spec being created -->

## Impact

- New directory: `adapters/codex-cli/` (8 files)
- New directory: `plugin/` under the adapter for marketplace distribution
- No changes to existing adapters, core contract, or state schema
- No dependency on external Codex CLI packages (adapter is file-based configuration)
