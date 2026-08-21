# dsh-adapter Specification

## Purpose

Define the DeepSeek Harness (`dsh`) adapter that packages the OpenSpec
controller workflow for dsh, providing the worker shim, role instruction
files, plan-manifest defaults, and installer that drive a single change
through the implement, review, and archive loop.

## Requirements

### Requirement: Worker shim

The adapter SHALL provide an executable shim at `bin/opsx-dsh-worker` that
accepts `--role implementer`, `--role reviewer`, or `--role archiver` followed
by the controller's worker input block as the final positional argument.

The shim SHALL compose the role's instruction file with the worker input block
into a single prompt and SHALL invoke dsh as
`dsh --profile headless [--patch <model-patch>] <prompt>` with the prompt as
one positional argument. The shim SHALL replace its own process image with dsh
(exec semantics) so that the controller's timeout and process-group signal
handling apply directly to the dsh process, and SHALL NOT write its own
content to stdout after composition.

The shim SHALL locate the role instruction file in the project controller
support directory first and the global controller support directory second,
and SHALL fail closed with a diagnostic naming the missing role file when
neither exists.

#### Scenario: Implementer dispatch through the shim

- **WHEN** a plan's `implement_invoke` is `opsx-dsh-worker --role implementer` and the controller appends the worker input block
- **THEN** the running process is dsh with the `headless` profile and a single positional prompt containing both the implementer instructions and the worker input block

#### Scenario: Shim exec preserves controller timeout handling

- **WHEN** the controller terminates the shim's process group on timeout
- **THEN** the dsh process itself receives the signal because the shim exec'd rather than forked

#### Scenario: Missing role file fails closed

- **WHEN** the shim is invoked with `--role reviewer` and no reviewer instruction file exists in either support directory
- **THEN** the shim exits non-zero with a diagnostic naming the missing file and does not launch dsh

### Requirement: Binary resolution

The shim SHALL resolve the dsh command in this order: the `DSH_BINARY`
environment variable (an executable path or a name looked up on `PATH`), then
`dsh` on `PATH`, then a pinned npx fallback
`npx --yes @deepseek-ai/dsh@0.1.0-rc.7`. When none is resolvable the shim
SHALL exit non-zero with a diagnostic naming the attempted sources.

The pinned fallback version SHALL be declared as a single constant in the shim
with a note that the CLI, profile, and patch contracts must be re-validated
before the pin is moved.

#### Scenario: Explicit binary wins

- **WHEN** `DSH_BINARY` names an executable and `dsh` also exists on `PATH`
- **THEN** the shim launches the `DSH_BINARY` executable

#### Scenario: Pinned npx fallback

- **WHEN** neither `DSH_BINARY` nor a `dsh` on `PATH` is available and `npx` is present
- **THEN** the shim launches dsh via `npx --yes @deepseek-ai/dsh@0.1.0-rc.7`

#### Scenario: No resolvable binary

- **WHEN** no resolution source yields an executable
- **THEN** the shim exits non-zero and the diagnostic names `DSH_BINARY`, `PATH`, and the pinned npx package

### Requirement: Model override via generated patch

When `OPSX_<ROLE>_MODEL` is set for the dispatched role, the shim SHALL split
the value into provider and model, map the provider through the built-in
mapping (`deepseek` → `deepseek-official`) overlaid by the
`OPSX_DSH_PROVIDER_MAP` JSON environment variable when set, and write a flat
entry-override patch file containing an `agent-default-model` entry under
`$DSH_HOME/patches/`. The shim SHALL pass that file via `--patch`.

When `OPSX_<ROLE>_MODEL` is unset the shim SHALL pass no `--patch` and dsh's
shipped default model applies. The shim SHALL NOT write API keys, tokens, or
other secret values into patch files or prompts.

#### Scenario: Configured model produces a patch

- **WHEN** the reviewer role dispatches with `OPSX_REVIEWER_MODEL=deepseek/deepseek-chat`
- **THEN** dsh is launched with `--patch` pointing at a flat YAML file overriding `agent-default-model` for the mapped provider and model

#### Scenario: No configured model uses the dsh default

- **WHEN** the implementer role dispatches and `OPSX_IMPLEMENTER_MODEL` is unset
- **THEN** dsh is launched without `--patch`

#### Scenario: Provider overlay takes precedence

- **WHEN** `OPSX_DSH_PROVIDER_MAP` maps `deepseek` to a custom provider id and the model is `deepseek/deepseek-chat`
- **THEN** the generated patch names the custom provider id

### Requirement: Controlled dsh runtime environment

The shim SHALL establish `DSH_HOME` with the precedence: ambient `DSH_HOME`,
then `OPSX_DSH_HOME`, then a default under the user's state directory. The
shim SHALL set `DSH_PERMISSION_MODE=workspace-write`, `DSH_TOOLS_MODE=code`,
and `DSH_TELEMETRY_DISABLED=1` unless the operator has already set the
corresponding variable, and SHALL write a stable startup `AGENTS.md` into
`DSH_HOME` when one does not exist.

The shim SHALL NOT override operator-set dsh environment variables.

#### Scenario: Defaults applied on a clean environment

- **WHEN** the shim launches dsh with no dsh-related environment variables set
- **THEN** the dsh process runs with `DSH_PERMISSION_MODE=workspace-write`, `DSH_TOOLS_MODE=code`, `DSH_TELEMETRY_DISABLED=1`, and a `DSH_HOME` containing the startup `AGENTS.md`

#### Scenario: Operator overrides are respected

- **WHEN** the operator has set `DSH_PERMISSION_MODE` and `DSH_HOME` in the environment
- **THEN** the shim preserves both values and does not write the startup `AGENTS.md` over an existing file

### Requirement: Implementer role instructions

The adapter SHALL provide an implementer instruction file at
`agents/opsx-implementer.md` whose workflow directs the worker to:
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
- Return exactly one JSON object in the success or blocked format as the final response

#### Scenario: Successful implementation with progress

- **WHEN** implementer receives valid input and tasks remain
- **THEN** implementer returns `{"status":"implemented","progress_made":true,...}` with `completed_tasks`, `files_touched`, `known_change_files`, and a `summary`

#### Scenario: Blocked implementation

- **WHEN** implementer encounters ambiguous or blocking conditions
- **THEN** implementer returns `{"status":"blocked","progress_made":false,...}` with a `reason` field

#### Scenario: Single JSON object output only

- **WHEN** implementer completes its work
- **THEN** the final response is exactly one JSON object with no markdown, headings, code fences, or commentary

### Requirement: Reviewer role instructions

The adapter SHALL provide a reviewer instruction file at
`agents/opsx-reviewer.md` whose workflow directs the worker to:
- Parse the input block
- Read repository guidance files
- Run live OpenSpec status, instructions, and `openspec validate <change> --strict`
- Trust cached context for stable background understanding when valid
- Reread verification-critical artifacts for the active round
- Classify findings: missing/incorrect work as `critical`, partial coverage/missing tests as `warning`, minor notes as `note`
- Return `verdict=pass` only when all three counts are zero
- Include a concise fix prompt when `verdict=fail`
- Return exactly one JSON object as the final response

#### Scenario: Clean review passes

- **WHEN** all implementation matches specs, tasks are complete, and validation passes
- **THEN** reviewer returns `{"status":"reviewed","verdict":"pass","finding_counts":{"critical":0,"warning":0,"note":0},"fix_prompt":"","next_phase":"archive"}`

#### Scenario: Review finds warnings

- **WHEN** implementation is correct but test coverage is incomplete (a warning)
- **THEN** reviewer returns `verdict=fail` with `warning > 0` and a fix prompt

#### Scenario: Review finds critical issues

- **WHEN** implementation is materially incorrect or missing required work
- **THEN** reviewer returns `verdict=fail` with `critical > 0` and a fix prompt

### Requirement: Archiver role instructions

The adapter SHALL provide an archiver instruction file at
`agents/opsx-archiver.md` whose workflow directs the worker to:
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
- Return exactly one JSON object as the final response

#### Scenario: Successful archive

- **WHEN** all checks pass and scope is clean
- **THEN** archiver returns `{"status":"archived","archive_path":"openspec/changes/archive/YYYY-MM-DD-<change>","spec_sync_status":"synced|no-delta","commit":"<sha>"}`

#### Scenario: Ambiguous archive scope

- **WHEN** the archiver cannot determine a narrow explicit staged set
- **THEN** archiver returns `{"status":"blocked","reason":"ambiguous archive commit scope","triage":{...}}` without mutating any files

#### Scenario: Commit failure with restore

- **WHEN** the move succeeds but `git commit` fails
- **THEN** archiver restores the change directory and returns a blocked result

### Requirement: Plan manifest adapter defaults

The orchestrator SHALL recognize `adapter = "dsh"` in a plan manifest and
resolve defaults of:
- `state_file`: `.opsx-controller/<change>.json`
- `implement_invoke`: `opsx-dsh-worker --role implementer`
- `review_invoke`: `opsx-dsh-worker --role reviewer`
- `archive_invoke`: `opsx-dsh-worker --role archiver`

Plan-provided `implement_invoke`, `review_invoke`, `archive_invoke`, and
`state_file` keys SHALL override these defaults. Models for dsh plans SHALL
resolve through the existing `[adapters.dsh]` configuration table and
`OPSX_*_MODEL` environment contract with no dsh-specific branching in the
resolver.

#### Scenario: dsh plan takes the direct path with no manual invokes

- **WHEN** a plan declares `adapter = "dsh"` and no invoke overrides
- **THEN** the implement, review, and archive stages dispatch through `opsx-dsh-worker` with the matching roles and the state file resolves to `.opsx-controller/<change>.json`

#### Scenario: Plan overrides win over defaults

- **WHEN** a plan declares `adapter = "dsh"` and an explicit `review_invoke`
- **THEN** the review stage uses the plan's command and the other stages use the dsh defaults

### Requirement: Install script

The adapter SHALL provide an install script at `install.sh` supporting:
- `--global`: installs the shim to `~/.local/bin`, the role instruction files
  and support files to the global dsh controller support directory, and the
  shared orchestrator runtime via the common installer.
- `--project <path>`: installs the role instruction files and support files to
  the project controller support directory so the shim resolves them first.

The install script SHALL warn when no `dsh` binary, `npx`, or Node.js with
TypeScript type-stripping support is detectable, naming the
`process.features.typescript` check, and SHALL fail with a usage message when
no valid mode is provided.

#### Scenario: Global install deploys the shim and role files

- **WHEN** an operator runs `bash adapters/dsh/install.sh --global`
- **THEN** `~/.local/bin/opsx-dsh-worker` is executable and the three role instruction files exist in the global dsh controller support directory

#### Scenario: Project install shadows global role files

- **WHEN** an operator runs `bash adapters/dsh/install.sh --project /path/to/project` after a global install
- **THEN** the shim dispatches with the project-installed role instruction files

#### Scenario: Host without type-stripping Node is warned

- **WHEN** the installer runs on a host whose Node.js reports `process.features.typescript` as falsy
- **THEN** the installer prints a warning that dsh tool calls will fail and names the check

#### Scenario: No mode specified

- **WHEN** an operator runs `bash adapters/dsh/install.sh` with no arguments
- **THEN** the script prints a usage message and exits with non-zero code

### Requirement: Durable state file

The adapter SHALL use the identical state schema v3 as defined in the core
contract, persisted to `.opsx-controller/<change-id>.json` at the project
root.

The state file SHALL contain all required fields: `version`, `change`,
`schema`, `status`, `phase`, `round`, `max_rounds`, `no_progress_streak`,
`latest_fix_prompt`, `last_result`, `task_counts`, `tracked_change_files`,
`context_cache`, `last_review`, `archive`, and `history`.

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
- Single JSON object output from all phase workers
- Fail-closed on ambiguous conditions
- Archive only after fresh clean review

#### Scenario: Workflow matches other adapters

- **WHEN** the dsh adapter drives a change through the full loop
- **THEN** the sequence of phase transitions, state updates, and stop conditions is identical to the OpenCode, Claude Code, and Codex CLI adapters

#### Scenario: Review gate treats all finding types as blocking

- **WHEN** reviewer returns `finding_counts: {critical:0, warning:0, note:1}`
- **THEN** the controller treats this as a review failure and loops back to implement
