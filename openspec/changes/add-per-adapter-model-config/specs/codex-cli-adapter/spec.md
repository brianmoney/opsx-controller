## MODIFIED Requirements

### Requirement: Implementer phase agent

The adapter SHALL provide a Codex custom agent at `agents/opsx-implementer.toml` with:
- `name = "opsx-implementer"`
- `sandbox_mode = "workspace-write"`
- `model` declared in the source agent file as the `{env:OPSX_IMPLEMENTER_MODEL}` placeholder, substituted at install time with the `implementer` model resolved for the `codex-cli` adapter
- `model_reasoning_effort = "high"`
- `developer_instructions` containing the full implement phase workflow

The installed agent file SHALL contain a concrete model identifier with no unsubstituted placeholder remaining.

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

#### Scenario: Installed agent carries the resolved model

- **WHEN** an operator installs the Codex adapter with a configured `codex-cli` implementer model
- **THEN** the installed `opsx-implementer.toml` declares that model and contains no `{env:` placeholder

### Requirement: Reviewer phase agent

The adapter SHALL provide a Codex custom agent at `agents/opsx-reviewer.toml` with:
- `name = "opsx-reviewer"`
- `sandbox_mode = "read-only"`
- `model` declared in the source agent file as the `{env:OPSX_REVIEWER_MODEL}` placeholder, substituted at install time with the `reviewer` model resolved for the `codex-cli` adapter
- `model_reasoning_effort = "high"`
- `developer_instructions` containing the full review phase workflow with strict classification rules

The installed agent file SHALL contain a concrete model identifier with no unsubstituted placeholder remaining.

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

#### Scenario: Installed agent carries the resolved model

- **WHEN** an operator installs the Codex adapter with a configured `codex-cli` reviewer model
- **THEN** the installed `opsx-reviewer.toml` declares that model and contains no `{env:` placeholder

### Requirement: Archiver phase agent

The adapter SHALL provide a Codex custom agent at `agents/opsx-archiver.toml` with:
- `name = "opsx-archiver"`
- `sandbox_mode = "danger-full-access"`
- `model` declared in the source agent file as the `{env:OPSX_ARCHIVER_MODEL}` placeholder, substituted at install time with the `archiver` model resolved for the `codex-cli` adapter
- `model_reasoning_effort = "high"`
- `developer_instructions` containing the full archive phase workflow

The installed agent file SHALL contain a concrete model identifier with no unsubstituted placeholder remaining.

The archiver agent SHALL:
- Parse the input block
- Read repository guidance and state file for `tracked_change_files`
- Validate archive readiness non-interactively
- Determine explicit archive commit scope before mutating files, including paths under `openspec/changes/<change>/` so the change-directory deletion is committable when tracked
- Fail closed if scope is ambiguous (return blocked JSON with triage)
- Sync delta specs from `openspec/changes/<change>/specs/` to `openspec/specs/` when unambiguous
- Move change to `openspec/changes/archive/YYYY-MM-DD-<change>`
- Check whether the change directory is tracked with `git ls-files -- openspec/changes/<change>`; when it is, stage the change-directory deletion together with the dated archive directory so the move is committed as one rename; when it is not, skip staging the deletion since there is none to stage
- Inspect staged files before committing — fail if any staged file falls outside the explicit archive set, and, when the change directory was tracked, fail if the deletions under `openspec/changes/<change>/` are not staged
- Create archive commit with exact message `archive(<change>): archive completed OpenSpec change`
- If move succeeds but commit fails, restore the change directory
- Never ask questions, never report success on failure
- Return exactly one line of JSON

#### Scenario: Successful archive

- **WHEN** all checks pass and scope is clean
- **THEN** archiver returns `{"status":"archived","archive_path":"openspec/changes/archive/YYYY-MM-DD-<change>","spec_sync_status":"synced|no-delta","commit":"<sha>"}` and the archive commit contains both the change-directory deletions and the dated archive directory

#### Scenario: Ambiguous archive scope

- **WHEN** the archiver cannot determine a narrow explicit staged set
- **THEN** archiver returns `{"status":"blocked","reason":"ambiguous archive commit scope","triage":{...}}` without mutating any files

#### Scenario: Unstaged change-directory deletion blocks the commit

- **WHEN** the change directory was tracked and the pre-commit staged-set inspection finds that the deletions under `openspec/changes/<change>/` are not staged
- **THEN** archiver returns a blocked result and does not create the archive commit

#### Scenario: Untracked change directory does not block the commit

- **WHEN** `git ls-files -- openspec/changes/<change>` lists no files before the move
- **THEN** the archiver skips staging a deletion, the pre-commit inspection does not require one, and the archiver can still reach `status=archived`

#### Scenario: Commit failure with restore

- **WHEN** the move succeeds but `git commit` fails
- **THEN** archiver restores the change directory and returns a blocked result

#### Scenario: Installed agent carries the resolved model

- **WHEN** an operator installs the Codex adapter with a configured `codex-cli` archiver model
- **THEN** the installed `opsx-archiver.toml` declares that model and contains no `{env:` placeholder
