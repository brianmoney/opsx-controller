## ADDED Requirements

### Requirement: Operators can archive a completed plan

The orchestrator SHALL provide `opsx-plan archive-plan <plan.toml>` that retires a completed authored plan by moving its compiled manifest, and its markdown source when present, into `openspec/plans/archived/`.

The command SHALL move tracked files with `git mv` and untracked files with a plain rename, determining tracked status before moving. The command SHALL NOT create a commit, and SHALL report the moved paths and that the move needs committing.

The command SHALL fail without moving anything when the target is already under `openspec/plans/archived/`, when the target is not under `openspec/plans/`, or when the target does not exist.

When the active-plan pointer references the plan being archived, the command SHALL clear the pointer and SHALL report that it did so. The command SHALL NOT repoint the pointer at the archived copy, because operating on an archived plan mutates its state destructively.

#### Scenario: Plan pair is archived
- **WHEN** an operator runs `opsx-plan archive-plan openspec/plans/example.toml` and `openspec/plans/example.md` exists
- **THEN** both files are moved into `openspec/plans/archived/`, the moved paths are reported, and no commit is created

#### Scenario: Manifest without a markdown source is archived
- **WHEN** an operator runs `opsx-plan archive-plan openspec/plans/example.toml` and no `openspec/plans/example.md` exists
- **THEN** the manifest is moved into `openspec/plans/archived/` and the command succeeds without reporting a missing markdown source as an error

#### Scenario: Archiving the active plan clears the pointer
- **WHEN** the active-plan pointer contains `openspec/plans/example.toml` and the operator runs `opsx-plan archive-plan openspec/plans/example.toml`
- **THEN** the active-plan pointer is cleared, the command reports that it was cleared, and the pointer is not set to the archived path

#### Scenario: Archiving a different plan leaves the pointer intact
- **WHEN** the active-plan pointer contains `openspec/plans/active.toml` and the operator runs `opsx-plan archive-plan openspec/plans/other.toml`
- **THEN** `openspec/plans/other.toml` is archived and the active-plan pointer still contains `openspec/plans/active.toml`

#### Scenario: Double archive is refused
- **WHEN** an operator runs `opsx-plan archive-plan openspec/plans/archived/example.toml`
- **THEN** the command exits with an error and no file is moved

## MODIFIED Requirements

### Requirement: Stale active-plan pointers fail closed

If the active-plan pointer exists but references a missing file, the orchestrator SHALL fail closed with an error that includes the recorded path.

When resolving a plan for a command, the orchestrator SHALL NOT auto-discover another plan TOML, select a nearby plan, or silently clear the stale pointer.

This constraint governs plan resolution. It SHALL NOT prevent a command whose explicit purpose is to retire a plan from clearing the pointer as a reported part of that operation, before the pointer becomes stale.

#### Scenario: Pointer target is missing

- **WHEN** the active-plan pointer contains `openspec/plans/deleted.toml` and that file no longer exists
- **THEN** `opsx-plan status` exits with an error naming `openspec/plans/deleted.toml` and does not load any other plan

#### Scenario: Resolution never self-heals a stale pointer

- **WHEN** the active-plan pointer references a missing file and other plan TOML files exist under `openspec/plans/`
- **THEN** the orchestrator still fails closed and leaves the pointer unchanged rather than selecting one of the available plans

### Requirement: Successful compile and explicit run activate plans

After a successful `opsx-plan compile <source.md> -o <plan.toml>`, the orchestrator SHALL record the output TOML as the active plan.

The `-o` argument SHALL be optional. When it is omitted, the orchestrator SHALL compile to `openspec/plans/<source-stem>.toml` and SHALL record that defaulted output as the active plan on success, identically to an explicitly specified output.

After `opsx-plan run <plan.toml>` is invoked with an explicit plan argument and that plan loads successfully, the orchestrator SHALL record that explicit plan as the active plan.

Failed compile or run invocations SHALL NOT update the active-plan pointer.

Single-change runs SHALL NOT update the active-plan pointer, whether they succeed or fail.

#### Scenario: Compile output becomes active

- **WHEN** `opsx-plan compile openspec/plans/example.md -o openspec/plans/example.toml` succeeds
- **THEN** `openspec/plans/example.toml` is recorded as the active plan

#### Scenario: Defaulted compile output becomes active

- **WHEN** `opsx-plan compile openspec/plans/example.md` succeeds with no output argument
- **THEN** the manifest is written to `openspec/plans/example.toml` and that path is recorded as the active plan

#### Scenario: Explicit run path becomes active

- **WHEN** an operator runs `opsx-plan run openspec/plans/example.toml` and the plan loads successfully
- **THEN** `openspec/plans/example.toml` is recorded as the active plan

#### Scenario: Failed compile does not replace active plan

- **WHEN** an existing active plan is recorded and `opsx-plan compile` fails validation before writing its output
- **THEN** the existing active-plan record remains unchanged

#### Scenario: Single-change run does not replace active plan

- **WHEN** the active-plan pointer contains `openspec/plans/active.toml` and an operator runs `opsx-run vault-gardening-suggestions` to completion
- **THEN** the active-plan pointer still contains `openspec/plans/active.toml` and does not reference the derived single-change manifest
