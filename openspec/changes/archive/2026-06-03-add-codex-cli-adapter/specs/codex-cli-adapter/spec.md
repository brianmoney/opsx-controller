## ADDED Requirements

### Requirement: Skill entrypoint for controller orchestration

The adapter SHALL provide a Codex skill at `skills/opsx-drive/SKILL.md` that serves as the user-facing entrypoint for starting or resuming the OpenSpec controller workflow for exactly one change.

The skill SHALL instruct the hosting Codex agent to:
- Parse exactly one change-id argument from user input
- Initialize or resume durable state at `.opsx-controller/<change-id>.json`
- Read repository guidance from `AGENTS.md` if it exists
- Run `openspec status --change "<change>" --json` and `openspec instructions apply --change "<change>" --json`
- Manage a `context_cache` with source signatures derived from repository guidance and OpenSpec context files
- Manage a deduplicated `tracked_change_files` list seeded from accepted change artifacts and current dirty worktree
- Dispatch phase agents (`opsx-implementer`, `opsx-reviewer`, `opsx-archiver`) in strict order using `spawn_agent` and `wait_agent`
- Provide each phase agent a compact plaintext input block with fields: CHANGE, ROUND, STATE_FILE, LATEST_FIX_PROMPT, TASK_COUNTS, CONTEXT_CACHE_STATUS, CONTEXT_CACHE_VALID, CONTEXT_CACHE_SUMMARY
- Parse single-line JSON output from each phase agent
- Loop from review back to implement when review returns any non-zero finding count
- Archive only after a fresh clean review (all finding counts zero, verdict=pass)
- Stop after `max_rounds` (5) failed review rounds or `no_progress_streak` (2) consecutive no-progress rounds
- Persist state after initialization, after every phase result, and before any blocked or completed exit

#### Scenario: Fresh controller run with valid change

- **WHEN** user invokes the skill with a valid change-id and the change exists in `openspec/changes/<id>/` with pending tasks
- **THEN** the controller initializes a fresh state file, seeds context cache, seeds tracked files, dispatches implementer, review succeeds, and archiver completes — resulting in `status=completed` and `phase=done`

#### Scenario: Resume after interrupted run

- **WHEN** user invokes the skill for a change with an existing state file at `phase=implement` and `round=2`
- **THEN** the controller loads existing state, validates context cache signature, and resumes from the current phase with the persisted `latest_fix_prompt`

#### Scenario: Missing change-id argument

- **WHEN** user invokes the skill without providing a change-id
- **THEN** the controller stops and reports that a change-id is required

#### Scenario: Review failure triggers re-implementation

- **WHEN** reviewer returns `verdict=fail` with non-zero `finding_counts` and `round < max_rounds`
- **THEN** the controller persists `latest_fix_prompt`, increments round, sets `phase=implement`, and dispatches implementer again

#### Scenario: Max rounds reached

- **WHEN** reviewer returns `verdict=fail` and `round` equals `max_rounds`
- **THEN** the controller persists `status=blocked` and `last_result=max_rounds_reached` and stops

#### Scenario: Consecutive no-progress stops

- **WHEN** implementer returns `progress_made=false` for 2 consecutive rounds
- **THEN** the controller persists `status=blocked` and stops

### Requirement: Implementer phase agent

The adapter SHALL provide a Codex custom agent at `agents/opsx-implementer.toml` with:
- `name = "opsx-implementer"`
- `sandbox_mode = "workspace-write"`
- `model = "gpt-5.4"`
- `model_reasoning_effort = "high"`
- `developer_instructions` containing the full implement phase workflow

The implementer agent SHALL:
- Parse the input block from the controller
- Read `AGENTS.md` if it exists
- Run live OpenSpec status and instructions
- Read the state file when it exists
- Trust cached context when `CONTEXT_CACHE_VALID=true`
- Always reread the tasks file for the active change
- Treat `LATEST_FIX_PROMPT` as highest-priority fix scope when non-empty
- Implement the next required work, keeping edits minimal and in scope
- Mark completed tasks in the change task file immediately
- Not commit, push, archive, rebase, or create branches
- Return exactly one line of JSON in the success or blocked format

#### Scenario: Successful implementation with progress

- **WHEN** implementer receives valid input and tasks remain
- **THEN** implementer returns `{"status":"implemented","progress_made":true,...}` with `completed_tasks`, `files_touched`, `known_change_files`, and a `summary`

#### Scenario: Blocked implementation

- **WHEN** implementer encounters ambiguous or blocking conditions
- **THEN** implementer returns `{"status":"blocked","progress_made":false,...}` with a `reason` field

#### Scenario: Single-line JSON output only

- **WHEN** implementer completes its work
- **THEN** the final response is exactly one line of JSON with no markdown, headings, code fences, or commentary

### Requirement: Reviewer phase agent

The adapter SHALL provide a Codex custom agent at `agents/opsx-reviewer.toml` with:
- `name = "opsx-reviewer"`
- `sandbox_mode = "read-only"`
- `model = "gpt-5.4"`
- `model_reasoning_effort = "high"`
- `developer_instructions` containing the full review phase workflow with strict classification rules

The reviewer agent SHALL:
- Parse the input block
- Read repository guidance files
- Run live OpenSpec status, instructions, and `openspec validate <change> --strict`
- Trust cached context for stable background understanding when valid
- Reread verification-critical artifacts for the active round
- Classify findings: missing/incorrect work as `critical`, partial coverage/missing tests as `warning`, minor notes as `note`
- Return `verdict=pass` only when all three counts are zero
- Include a concise fix prompt when `verdict=fail`
- Return exactly one line of JSON

#### Scenario: Clean review passes

- **WHEN** all implementation matches specs, tasks are complete, and validation passes
- **THEN** reviewer returns `{"status":"reviewed","verdict":"pass","finding_counts":{"critical":0,"warning":0,"note":0},"fix_prompt":"","next_phase":"archive"}`

#### Scenario: Review finds warnings

- **WHEN** implementation is correct but test coverage is incomplete (a warning)
- **THEN** reviewer returns `{"status":"reviewed","verdict":"fail","finding_counts":{"critical":0,"warning":1,"note":0},"fix_prompt":"...","next_phase":"implement"}`

#### Scenario: Review finds critical issues

- **WHEN** implementation is materially incorrect or missing required work
- **THEN** reviewer returns `verdict=fail` with `critical > 0` and a fix prompt

### Requirement: Archiver phase agent

The adapter SHALL provide a Codex custom agent at `agents/opsx-archiver.toml` with:
- `name = "opsx-archiver"`
- `sandbox_mode = "danger-full-access"`
- `model = "gpt-5.4"`
- `model_reasoning_effort = "high"`
- `developer_instructions` containing the full archive phase workflow

The archiver agent SHALL:
- Parse the input block
- Read repository guidance and state file for `tracked_change_files`
- Validate archive readiness non-interactively
- Determine explicit archive commit scope before mutating files
- Fail closed if scope is ambiguous (return blocked JSON with triage)
- Sync delta specs from `openspec/changes/<change>/specs/` to `openspec/specs/` when unambiguous
- Move change to `openspec/changes/archive/YYYY-MM-DD-<change>`
- Inspect staged files before committing — fail if any staged file falls outside explicit archive set
- Create archive commit with exact message `archive(<change>): archive completed OpenSpec change`
- If move succeeds but commit fails, restore the change directory
- Never ask questions, never report success on failure
- Return exactly one line of JSON

#### Scenario: Successful archive

- **WHEN** all checks pass and scope is clean
- **THEN** archiver returns `{"status":"archived","archive_path":"openspec/changes/archive/YYYY-MM-DD-<change>","spec_sync_status":"synced|no-delta","commit":"<sha>"}`

#### Scenario: Ambiguous archive scope

- **WHEN** the archiver cannot determine a narrow explicit staged set
- **THEN** archiver returns `{"status":"blocked","reason":"ambiguous archive commit scope","triage":{...}}` without mutating any files

#### Scenario: Commit failure with restore

- **WHEN** the move succeeds but `git commit` fails
- **THEN** archiver restores the change directory and returns a blocked result

### Requirement: Install script

The adapter SHALL provide an install script at `install.sh` supporting three modes:
- `--global`: Copies skill to `$HOME/.agents/skills/opsx-drive/`, agents to `$HOME/.codex/agents/`, and support files to `$HOME/.codex/opsx-controller/`
- `--project <path>`: Copies skill to `<path>/.agents/skills/opsx-drive/`, agents to `<path>/.codex/agents/`, support files to `<path>/.codex/opsx-controller/`, and creates/updates `.codex/.gitignore` to ignore `opsx-controller/*.json`
- `--plugin`: Creates plugin bundle directory structure with `.codex-plugin/plugin.json` and copies skill and agent files

The install script SHALL fail with a usage message when no valid mode is provided.

#### Scenario: Global install succeeds

- **WHEN** user runs `bash install.sh --global` from the adapter directory
- **THEN** skill files appear in `$HOME/.agents/skills/opsx-drive/`, agent TOML files in `$HOME/.codex/agents/`, and support README in `$HOME/.codex/opsx-controller/`

#### Scenario: Project install succeeds

- **WHEN** user runs `bash install.sh --project /path/to/project`
- **THEN** files appear in the project's `.agents/skills/`, `.codex/agents/`, and `.codex/opsx-controller/` directories

#### Scenario: Plugin install creates bundle

- **WHEN** user runs `bash install.sh --plugin`
- **THEN** a self-contained plugin directory is created with `.codex-plugin/plugin.json`, `skills/`, and `agents/`

#### Scenario: No mode specified

- **WHEN** user runs `bash install.sh` with no arguments
- **THEN** script prints usage message and exits with non-zero code

### Requirement: Plugin manifest for marketplace distribution

The adapter SHALL provide a plugin manifest at `plugin/.codex-plugin/plugin.json` conforming to the Codex plugin specification with:
- `name`, `version`, `description`, `author`
- `skills` pointing to `./skills/`
- `interface` with `displayName`, `shortDescription`, `category`, and `capabilities`

#### Scenario: Plugin manifest is valid

- **WHEN** Codex loads the plugin manifest
- **THEN** the `opsx-drive` skill is registered and discoverable in the Codex plugin/skill browser

### Requirement: Durable state file

The adapter SHALL use the identical state schema v3 as defined in the core contract, persisted to `.opsx-controller/<change-id>.json` at the project root.

State file location differs from other adapters (`.opencode/opsx-controller/` and `.claude/opsx-controller/`) because Codex sandbox protects the `.codex/` directory from agent writes.

The state file SHALL contain all required fields: `version`, `change`, `schema`, `status`, `phase`, `round`, `max_rounds`, `no_progress_streak`, `latest_fix_prompt`, `last_result`, `task_counts`, `tracked_change_files`, `context_cache`, `last_review`, `archive`, and `history`.

#### Scenario: State survives interrupted runs

- **WHEN** the controller writes state to `.opsx-controller/<change>.json` and the session is interrupted
- **THEN** the state file persists on disk with valid JSON and can be loaded on resume

#### Scenario: Malformed state file stops controller

- **WHEN** the state file exists but contains malformed JSON or is for a different change
- **THEN** the controller stops and reports that the operator must fix or remove the broken state file

### Requirement: Core contract preservation

The adapter SHALL preserve the full core contract without modification:
- Three-phase loop: implement → review → archive
- Strict review gate: any critical, warning, or note finding is blocking
- Bounded rounds: max 5 failed review rounds
- No-progress streak: stop after 2 consecutive no-progress implementations
- Single-line JSON output from all phase agents
- Fail-closed on ambiguous conditions
- Archive only after fresh clean review

#### Scenario: Workflow matches other adapters

- **WHEN** the Codex adapter drives a change through the full loop
- **THEN** the sequence of phase transitions, state updates, and stop conditions is identical to the OpenCode and Claude Code adapters

#### Scenario: Review gate treats all finding types as blocking

- **WHEN** reviewer returns `finding_counts: {critical:0, warning:0, note:1}`
- **THEN** the controller treats this as a review failure and loops back to implement
