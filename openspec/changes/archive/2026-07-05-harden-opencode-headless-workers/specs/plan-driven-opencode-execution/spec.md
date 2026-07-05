## ADDED Requirements

### Requirement: OpenCode direct workers avoid interactive guidance discovery

For OpenCode-backed direct execution, phase worker instructions and permissions SHALL keep worker startup non-interactive when optional repository guidance files are absent.

The OpenCode worker agent definitions SHALL:
- treat repo-root `AGENTS.md` as optional guidance
- continue when repo-root `AGENTS.md` does not exist
- forbid parent-directory or external-directory searches for missing repo guidance
- deny broad external-directory access by default
- preserve explicit access to installed OpenCode prompt files under `~/.config/opencode`

#### Scenario: Missing repo guidance does not request external access
- **WHEN** an OpenCode direct phase worker starts in a repository without `AGENTS.md`
- **THEN** the worker instructions require it to continue without searching parent directories or requesting broad external-directory permission

#### Scenario: Global prompt reads remain allowed
- **WHEN** an OpenCode direct phase worker needs to read installed global OpenCode prompts from `~/.config/opencode`
- **THEN** the worker permissions allow those reads without granting broad external-directory access

### Requirement: Permission-rejected worker transcripts are reported actionably

For OpenCode-backed direct execution, `opsx-plan` SHALL distinguish worker transcripts that ended before final JSON because of an OpenCode permission rejection from generic malformed worker output.

When a stage log contains no parseable final JSON object and includes permission-rejection markers, the orchestrator SHALL mark the stage output invalid with an error reason that identifies a permission denial before JSON output.

#### Scenario: External-directory prompt is auto-rejected
- **WHEN** a worker log has no final JSON object and contains an auto-rejected `external_directory` permission request
- **THEN** `opsx-plan` records the stage as invalid output with a reason indicating that permission was denied before JSON output

#### Scenario: Valid final JSON remains authoritative
- **WHEN** a worker log contains noisy transcript lines and a valid final JSON object line
- **THEN** `opsx-plan` parses the JSON payload and does not report a permission-denial parse error
