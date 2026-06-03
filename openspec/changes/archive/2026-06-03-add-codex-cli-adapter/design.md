## Context

opsx-controller currently has two adapters — `adapters/opencode/` and `adapters/claude-code/` — each mapping the core three-phase workflow (implement→review→archive) onto a specific AI coding client's native primitives. The adapters share a common entrypoint pattern (slash command or skill), three phase subagents, an install script, and a durable JSON state file.

OpenAI Codex CLI uses a different set of primitives: skills for reusable workflows, TOML-based custom agent definitions, `spawn_agent`/`wait_agent` tools for subagent orchestration, sandbox modes for permission control, and a plugin marketplace for distribution.

The adapter must preserve the core contract — same state schema, same phase protocol, same strict review gate, same stop conditions — while using Codex-native mechanisms for all surface-level concerns (entrypoint, agent definitions, install paths).

## Goals / Non-Goals

**Goals:**
- Create a complete Codex CLI adapter matching the existing adapter pattern (skill entrypoint + 3 phase agents + install script + plugin manifest)
- Preserve the full core contract: state schema v3, single-line JSON phase output, zero-finding review gate, max-rounds=5 / no-progress-streak=2 stop conditions
- Use Codex-native mechanisms everywhere: skills for entrypoint, TOML agent definitions, `spawn_agent` for dispatch, `sandbox_mode` for permissions
- Support global install, project install, and plugin-based marketplace distribution
- Handle the Codex sandbox constraint: state files go in `.opsx-controller/` (not `.codex/`) because Codex protects `.codex/` from agent writes

**Non-Goals:**
- Modifying the core contract or state schema (adapter-only change)
- Modifying the OpenCode or Claude Code adapters
- Building a Codex-compatible binary or npm package (file-based configuration only)
- CSV batch processing or `spawn_agents_on_csv` integration (out of scope for initial adapter)
- Custom slash commands (Codex slash commands are hardcoded; a skill is sufficient)

## Decisions

### D1: Prompt-driven controller (not tool-driven)

The controller skill instructs the main Codex agent to use `spawn_agent` and `wait_agent` rather than executing those tools directly. Rationale: Codex skills don't execute tools themselves — they are loaded as instructions into the main agent's context. The skill body provides the orchestration rules and the main agent carries them out.

Alternatives considered:
- **Skill with tool declarations**: Codex skills can't declare required tools. The main agent's toolset is determined by its config, not by loaded skills.
- **Custom controller agent**: A named `opsx-controller` agent could be spawned, but then the user would need to spawn the controller, which would then spawn subagents — adding an extra nesting layer. The skill-as-controller approach keeps the user interaction surface simple.

### D2: TOML agent definitions with `developer_instructions`

Phase agents use TOML files with `developer_instructions` fields containing the same markdown-prose instruction bodies as the OpenCode/Claude Code agents. Rationale: Codex custom agents are defined as TOML files in `.codex/agents/`, not as Markdown frontmatter files.

Field mapping:
| OpenCode/Claude Code | Codex CLI TOML |
|---|---|
| `name` (frontmatter) | `name` |
| `description` (frontmatter) | `description` |
| `tools: Read, Edit, ...` (frontmatter) | `sandbox_mode` (read-only/workspace-write/danger-full-access) |
| `model: inherit` | `model` (explicit: gpt-5.4) |
| `effort: high/xhigh` | `model_reasoning_effort` (high/medium/low) |
| Body markdown | `developer_instructions` (triple-quoted TOML string) |

Alternatives considered:
- **Skills instead of agents for phase runners**: Skills are designed for reusable workflows, not for subagent dispatch targets. Codex subagents must be defined as TOML agent files.
- **Attempting to use `model: inherit`**: Codex TOML requires explicit model names. Inherited models are not supported for custom agents.

### D3: State path `.opsx-controller/` instead of `.codex/opsx-controller/`

Codex sandbox protects `.codex/` and `.git/` from writes even in `workspace-write` mode. The controller, implementer, and archiver agents all need read/write access to the state file. Placing it at `.opsx-controller/<change>.json` (project root) avoids the protected path entirely.

Alternatives considered:
- **`.codex/opsx-controller/` with `danger-full-access` everywhere**: This would work but is unnecessarily broad for the implementer and reviewer phases. The reviewer should remain `read-only`.
- **`$HOME/.codex/opsx-controller/`**: Mixes per-project state with global config, breaking the pattern of other adapters.

### D4: Sandbox modes map to permission scopes

| Phase Agent | Sandbox Mode | Rationale |
|---|---|---|
| `opsx-implementer` | `workspace-write` | Needs to read/write source files and tasks, but not commit or access outside workspace |
| `opsx-reviewer` | `read-only` | Only reads files and runs validation. Matches the existing tool restrictions (Read, Glob, Grep, Bash) |
| `opsx-archiver` | `danger-full-access` | Must run `git commit`, move directories, and access git history. Cannot function under workspace-write |

### D5: Model bindings

All three phase agents use `gpt-5.4` with `model_reasoning_effort = "high"`. Rationale: Codex does not support per-agent model providers (unlike OpenCode which uses `deepseek/deepseek-v4-pro` for implementer and `github-copilot/gpt-5.4` for reviewer). All agents use the OpenAI model tier. The reviewer compensates for the lack of an "xhigh" reasoning level by using explicit strictness in its instructions.

## Risks / Trade-offs

- **[R1] Prompt-driven orchestration is less deterministic than tool-driven**: The controller skill relies on the main agent interpreting its instructions correctly. If the agent fails to spawn subagents in order or misparses the JSON output, the loop stalls. Mitigation: The SKILL.md provides explicit step-by-step instructions with exact tool call specifications and the single-line JSON contract is simple enough to parse reliably.
- **[R2] Model tier gap for reviewer**: OpenCode's reviewer uses a different model (`github-copilot/gpt-5.4` with `variant: xhigh`). Codex's reviewer uses `gpt-5.4` with `model_reasoning_effort = "high"` (no xhigh equivalent). Mitigation: The reviewer's classification rules are explicit and mechanical — strict counting of critical/warning/note findings against explicit criteria. Model tier affects thoroughness but not the gating mechanism.
- **[R3] State path divergence from other adapters**: `.opsx-controller/` at project root differs from `.opencode/opsx-controller/` and `.claude/opsx-controller/`. Mitigation: This is a necessary accommodation for Codex's sandbox model. The state schema and contract are identical — only the file path differs. The install script documents this clearly.
- **[R4] Codex subagent depth limit**: `agents.max_depth` defaults to 1, meaning subagents cannot spawn grandchildren. The opsx-controller only uses one level (controller → phase agents), so the default is sufficient. If a user has overridden `max_depth` to 0, the controller would fail; the install script can note this requirement.
- **[R5] No custom slash commands**: Users must invoke the skill as `$opsx-drive <change>` or via `/skills` browsing rather than `/opsx-drive`. Mitigation: The skill's description is written to trigger implicit invocation when users mention working on an OpenSpec change, and explicit invocation via `$opsx-drive` is documented.
