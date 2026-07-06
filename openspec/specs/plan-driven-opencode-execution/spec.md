## Requirements

### Requirement: `opsx-plan` directly dispatches OpenCode phase workers

For plan runs using the OpenCode adapter, `opsx-plan` SHALL execute each ready accepted change as a sequence of bounded implement, review, and archive worker invocations instead of launching `/opsx-drive` as a nested controller.

The orchestrator SHALL:
- invoke at most one phase worker per subprocess
- construct the worker input for the active change, round, and phase
- persist plan-owned phase state before and after each worker run
- write stage-specific logs under `.opsx-plan/logs/`

#### Scenario: Ready change enters direct implement-review-archive flow
- **WHEN** a ready accepted change is selected in an OpenCode-backed plan run
- **THEN** `opsx-plan` dispatches implement, then review, then archive as separate worker invocations without calling `/opsx-drive`

### Requirement: Review failures loop inside `opsx-plan`

When the OpenCode review worker returns any non-zero `critical`, `warning`, or `note` finding count, `opsx-plan` SHALL treat the review as blocking, persist the returned fix prompt, increment the round when retry budget remains, and schedule the next implement worker run itself.

The orchestrator SHALL stop with a blocked or failed status when the retry budget is exhausted or when the no-progress ceiling is reached.

#### Scenario: Review finding triggers another implement round
- **WHEN** the review worker returns `verdict=fail` with a non-zero finding count and the change is below the round ceiling
- **THEN** `opsx-plan` records the fix prompt, advances the change back to implement, and launches a new implement worker run on the next dispatch

#### Scenario: Max rounds stops the change
- **WHEN** the review worker returns `verdict=fail` for a change already at the configured round ceiling
- **THEN** `opsx-plan` marks the change blocked or failed with a reason indicating the review retry budget was exhausted and does not launch another worker

### Requirement: Plan state is authoritative for resumed OpenCode plan execution

For OpenCode-backed direct execution, `.opsx-plan/<plan-name>.state.json` SHALL be the authoritative durable state for the active change's phase, round, latest fix prompt, review result, archive result, and last stage log.

`opsx-plan` SHALL be able to resume or reconcile an interrupted run from its own state and repository evidence without requiring `.opencode/opsx-controller/<change>.json` to exist or remain current.

#### Scenario: Interrupted review resumes from plan state
- **WHEN** an OpenCode-backed plan run is interrupted after implement completed and the persisted plan state says the change is in review
- **THEN** rerunning `opsx-plan` resumes the change from review using the stored plan state instead of invoking `/opsx-drive` recovery

### Requirement: Completion remains evidence-driven

`opsx-plan` SHALL mark an OpenCode-backed change done only after all of the following are true:
- the archive worker returns a machine-readable archived result for the current run
- `openspec/changes/<change>` no longer exists in the worktree
- a dated archive directory exists for the change
- an `archive(<change>):` commit is reachable from `HEAD`
- configured post-archive fast checks pass

The orchestrator SHALL NOT treat worker exit code or prose output alone as successful completion.

#### Scenario: Archive worker success without repository evidence does not complete the change
- **WHEN** the archive worker claims success but the change directory still exists or no archive commit is reachable
- **THEN** `opsx-plan` does not mark the change done and records the archive as failed or unverifiable

### Requirement: `/opsx-drive` remains available for manual single-change control

This change SHALL remove `/opsx-drive` from the `opsx-plan` execution path, but SHALL keep the manual `/opsx-drive <change-id>` controller surface available for operators who want to drive one change outside a plan run.

#### Scenario: Manual `/opsx-drive` use remains supported
- **WHEN** an operator manually invokes `/opsx-drive <change-id>` after this change lands
- **THEN** the single-change controller path still exists even though `opsx-plan` no longer calls it during plan execution

### Requirement: Single-change runner executes without a plan manifest

The OpenCode adapter SHALL provide an `opsx-run <change-id>` command surface that starts or resumes the direct implement, review, and archive worker loop for exactly one existing accepted OpenSpec change without requiring a plan TOML manifest.

The runner SHALL synthesize the minimal one-change orchestration configuration needed by the existing direct OpenCode execution path and SHALL persist durable state under `.opsx-plan/`.

#### Scenario: Operator runs one existing change
- **WHEN** an operator invokes `opsx-run vault-gardening-suggestions` from a repository with an authored `openspec/changes/vault-gardening-suggestions/` change
- **THEN** the runner dispatches the OpenCode implementer, reviewer, and archiver workers through the direct OpenCode loop without reading a plan manifest

#### Scenario: Equivalent script subcommand is available
- **WHEN** an operator invokes the orchestrator script through `opsx-plan run-one vault-gardening-suggestions`
- **THEN** the orchestrator uses the same single-change execution behavior as `opsx-run vault-gardening-suggestions`

### Requirement: Single-change runner preserves direct loop gates

The single-change runner SHALL preserve the existing direct OpenCode review retry, no-progress, archive verification, and post-archive fast-check behavior used by OpenCode-backed plan execution.

The runner SHALL NOT mark the change done based only on worker process exit status or prose output.

#### Scenario: Review failure loops back to implementation
- **WHEN** the reviewer returns `verdict=fail` with any non-zero finding count and retry budget remains during an `opsx-run` execution
- **THEN** the runner records the fix prompt, increments the round, and dispatches the implementer again before another review attempt

#### Scenario: Clean review archives and verifies completion
- **WHEN** the reviewer returns `verdict=pass` with zero finding counts during an `opsx-run` execution
- **THEN** the runner dispatches the archiver and marks the change done only after archive worker output, repository archive evidence, archive commit evidence, and configured fast checks agree

### Requirement: Single-change runner fails closed for invalid inputs

The single-change runner SHALL accept exactly one change id and SHALL fail before worker dispatch if the change is missing, not authored, or if tracked files are dirty when tracked-clean enforcement is enabled.

The runner SHALL NOT create missing OpenSpec change artifacts.

#### Scenario: Missing change is rejected
- **WHEN** an operator invokes `opsx-run missing-change` and `openspec/changes/missing-change/` is absent or lacks required authored artifacts
- **THEN** the runner exits with a clear error and does not dispatch implementer, reviewer, or archiver workers

#### Scenario: Extra positional arguments are rejected
- **WHEN** an operator invokes `opsx-run first-change second-change`
- **THEN** the runner exits with a usage error and does not dispatch any worker

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

### Requirement: `opsx-plan` compiles markdown implementation plans

The orchestrator SHALL provide an `opsx-plan compile <source.md> -o <output.toml>` command that converts a markdown implementation-plan document into a TOML manifest accepted by the existing `opsx-plan` plan loader.

The compile command SHALL refuse to overwrite an existing output path unless the operator passes `--force`.

#### Scenario: Operator compiles a markdown plan
- **WHEN** an operator runs `opsx-plan compile openspec/plans/example-plan.md -o openspec/plans/example-plan.toml`
- **THEN** the command creates a TOML manifest at the requested output path that can be loaded by `opsx-plan status` and `opsx-plan run`

#### Scenario: Existing output requires force
- **WHEN** the requested output file already exists and the operator does not pass `--force`
- **THEN** `opsx-plan compile` exits with a clear error and leaves the existing output file unchanged

### Requirement: Plan compilation invokes OpenCode with the controller model

`opsx-plan compile` SHALL invoke OpenCode to perform the markdown-to-TOML transformation and SHALL select the model from the `OPSX_CONTROLLER_MODEL` environment variable.

If `OPSX_CONTROLLER_MODEL` is unset or empty, the command SHALL fail before invoking OpenCode and explain that the controller model must be configured.

#### Scenario: Controller model is passed to OpenCode
- **WHEN** `OPSX_CONTROLLER_MODEL` is set and an operator runs `opsx-plan compile`
- **THEN** the spawned OpenCode command includes the configured controller model for the transformation request

#### Scenario: Missing controller model fails closed
- **WHEN** `OPSX_CONTROLLER_MODEL` is unset or empty
- **THEN** `opsx-plan compile` exits with a configuration error before spawning OpenCode

### Requirement: Compile prompts include source and reference context

The compile command SHALL provide the OpenCode invocation with a self-contained prompt that includes the source markdown plan, the expected TOML manifest shape, dependency and phase interpretation rules, current adapter defaults, and representative markdown/TOML template plan references when available in the repository.

The prompt SHALL instruct the model to emit only the compiled TOML manifest and not to include prose outside the TOML payload.

#### Scenario: Prompt contains template plans and schema guidance
- **WHEN** `opsx-plan compile` builds the OpenCode prompt for a source markdown plan
- **THEN** the prompt includes the source plan content, manifest field guidance for `[plan]` and `[[changes]]`, dependency-resolution guidance, and at least one available repository template plan pair or an explicit note that no template pair was found

#### Scenario: Prompt forbids prose output
- **WHEN** the prompt is sent to OpenCode
- **THEN** it instructs the model to return TOML only so the result can be validated and written without manual cleanup

### Requirement: Compiled manifests are validated before write success

`opsx-plan compile` SHALL parse the generated TOML and validate it with the same plan-loading path used by `opsx-plan status` and `opsx-plan run` before reporting success.

If validation fails, the command SHALL exit with a clear error and SHALL NOT replace an existing output file.

#### Scenario: Valid generated TOML is written atomically
- **WHEN** OpenCode returns TOML that parses successfully and passes the existing plan loader validation
- **THEN** `opsx-plan compile` writes the output manifest atomically and reports the output path

#### Scenario: Invalid generated TOML is rejected
- **WHEN** OpenCode returns malformed TOML or a manifest with invalid dependency references
- **THEN** `opsx-plan compile` exits with a validation error and does not report the manifest as compiled
