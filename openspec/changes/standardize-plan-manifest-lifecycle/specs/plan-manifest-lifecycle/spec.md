## ADDED Requirements

### Requirement: Authored plan manifests have a canonical repository location

Compiled plan manifests produced from an authored markdown plan SHALL have `openspec/plans/` as their canonical location, and the orchestrator SHALL use `openspec/plans/<source-stem>.toml` as the compile output when the operator does not specify one.

The location of authored markdown plans before compilation SHALL NOT be constrained by this capability.

#### Scenario: Compile without an explicit output path
- **WHEN** an operator runs `opsx-plan compile openspec/plans/example.md` with no output argument
- **THEN** the compiled manifest is written to `openspec/plans/example.toml`

#### Scenario: Markdown source outside the canonical directory still defaults there
- **WHEN** an operator runs `opsx-plan compile docs/plans/example-plan.md` with no output argument
- **THEN** the compiled manifest is written to `openspec/plans/example-plan.toml`

### Requirement: Derived single-change manifests are separated from authored manifests

Manifests the orchestrator generates for itself SHALL be written under `.opsx-plan/plans/` and SHALL NOT be written into `openspec/plans/`.

A single-change run of change id `<change-id>` SHALL use the manifest path `.opsx-plan/plans/run-<change-id>.toml`, matching the `run-<change-id>` plan name its state, telemetry, usage, and worker artifacts already use.

Because `.opsx-plan/` is excluded from version control, generating a derived manifest SHALL NOT modify the tracked working tree.

#### Scenario: Derived manifest is written beside its run artifacts
- **WHEN** a single-change run of `vault-gardening-suggestions` generates its manifest
- **THEN** the manifest is written to `.opsx-plan/plans/run-vault-gardening-suggestions.toml` and the run's state remains at `.opsx-plan/run-vault-gardening-suggestions.state.json`

#### Scenario: Generating a derived manifest leaves the tracked tree clean
- **WHEN** a single-change run generates its manifest in a repository with a clean tracked worktree
- **THEN** no tracked file is added, modified, or deleted by the manifest write

### Requirement: Derived manifests are verified by round-trip before they are written

Before a derived manifest replaces any existing file, the orchestrator SHALL write it to a temporary path, load it through the same plan-loading path used by `opsx-plan status` and `opsx-plan run`, and compare the loaded configuration against the configuration that was serialized.

The orchestrator SHALL fail with a clear error and SHALL NOT leave a manifest in place when the loaded configuration differs from the serialized configuration in any field, so a derived manifest can never describe a configuration other than the one the run uses.

The serialized manifest SHALL explicitly record every field whose value differs from the plan loader's default, including fields whose synthesized value is the loader default's opposite.

#### Scenario: Faithful manifest is written atomically
- **WHEN** the serialized single-change manifest loads successfully and its loaded configuration equals the synthesized configuration
- **THEN** the manifest is moved into place atomically and the run proceeds

#### Scenario: Divergent manifest aborts the write
- **WHEN** the serialized manifest loads successfully but the loaded configuration differs from the synthesized configuration in any field
- **THEN** the orchestrator exits with an error identifying the mismatch, removes the temporary file, and does not leave a derived manifest in place

#### Scenario: Loader-default opposites survive the round trip
- **WHEN** the synthesized single-change configuration sets a field to a value opposite the plan loader's default for that field
- **THEN** the serialized manifest states that field explicitly and the reloaded configuration preserves the synthesized value

### Requirement: Derived manifests are regenerated rather than preserved

The orchestrator SHALL regenerate the derived manifest on every single-change run and SHALL overwrite any existing derived manifest for that change id without requiring a force flag.

#### Scenario: Repeated run refreshes the manifest
- **WHEN** an operator runs the same change id a second time after adapter defaults or resolved models have changed
- **THEN** the derived manifest is rewritten to reflect the configuration of the current run without an overwrite prompt or error

### Requirement: Completed plans retire to an archived subdirectory

Completed authored plans SHALL retire to `openspec/plans/archived/`, retaining both the markdown source and the compiled manifest as a pair.

Archived plan pairs SHALL remain available to the orchestrator as repository template plan references.

#### Scenario: Archived pair keeps both artifacts together
- **WHEN** a completed plan `openspec/plans/example.md` and `openspec/plans/example.toml` is retired
- **THEN** both files reside at `openspec/plans/archived/example.md` and `openspec/plans/archived/example.toml`
