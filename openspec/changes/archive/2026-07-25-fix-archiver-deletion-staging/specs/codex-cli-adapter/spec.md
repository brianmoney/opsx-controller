## MODIFIED Requirements

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
