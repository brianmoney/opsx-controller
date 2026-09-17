# plan-manifest-lifecycle Specification

## Purpose
TBD - created by archiving change standardize-plan-manifest-lifecycle. Update Purpose after archive.

## Requirements

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

### Requirement: A canonical sample plan pair ships with the orchestrator

The orchestrator SHALL ship a canonical sample plan as a markdown source and its compiled TOML manifest, kept together as a pair, so that the transformation from an authored plan document to a manifest can be demonstrated rather than only described.

The sample SHALL exercise the documented `[plan]` and `[[changes]]` field surface, including phase assignment, dependency edges, and gating, so it serves as a complete reference rather than a minimal one.

The sample SHALL be deployed with the orchestrator runtime and SHALL be resolvable when `opsx-plan` executes from its installed location against an unrelated repository. Resolution SHALL prefer the installed runtime location and SHALL fall back to the repository checkout.

The repository SHALL NOT ship a second, competing example manifest presented as authoritative.

#### Scenario: Sample is resolvable from an installed run
- **WHEN** `opsx-plan` executes from its installed location with a working directory in an unrelated repository that contains no plans
- **THEN** the canonical sample pair is resolved from the installed runtime location

#### Scenario: Sample is resolvable from a repository checkout
- **WHEN** `opsx-plan` executes directly from a repository checkout that has not been installed
- **THEN** the canonical sample pair is resolved from the checkout

#### Scenario: Missing sample is not fatal
- **WHEN** neither the installed runtime location nor a repository checkout provides the sample pair
- **THEN** commands that would include the sample continue without it instead of failing

### Requirement: The canonical sample is verified against the plan loader

The canonical sample manifest SHALL load successfully through the same plan-loading path used by `opsx-plan status` and `opsx-plan run`, and this SHALL be enforced by the test suite.

The test suite SHALL additionally assert that the sample exercises the documented field surface, so that a field added to or changed in the loader cannot leave the shipped sample silently stale.

The sample SHALL NOT contain keys the current loader ignores.

#### Scenario: Shipped sample loads
- **WHEN** the test suite loads the canonical sample manifest through the plan loader
- **THEN** it loads without error and yields the changes, dependency edges, and gates the sample markdown describes

#### Scenario: Loader drift fails the suite
- **WHEN** the plan loader changes such that the canonical sample no longer covers the documented field surface, or the sample carries a key the loader ignores
- **THEN** the test suite fails rather than shipping a stale example

### Requirement: Completed plans retire to an archived subdirectory

Completed authored plans SHALL retire to `openspec/plans/archived/`, retaining both the markdown source and the compiled manifest as a pair.

Archived plan pairs SHALL remain available to the orchestrator as repository template plan references.
#### Scenario: Archived pair keeps both artifacts together

- **WHEN** a completed plan `openspec/plans/example.md` and `openspec/plans/example.toml` is retired
- **THEN** both files reside at `openspec/plans/archived/example.md` and `openspec/plans/archived/example.toml`

### Requirement: Supervision storage leaves JSON execution state authoritative and unchanged

For unregistered legacy jobs, the JSON execution state under `.opsx-plan/`
SHALL remain the authoritative state record for plan execution, whether or
not any supervisor ledger exists. The supervisor ledger SHALL NOT be stored
under `.opsx-plan/` or anywhere else inside the repository worktree, and
introducing supervision storage SHALL NOT change the JSON execution state's
format, location, or read/write semantics for unregistered jobs. Legacy jobs
without a supervised registration SHALL keep their existing JSON handling
with no dependency on the supervisor package or ledger.

For a registered supervised job, the broker and supervisor ledger SHALL be
the phase authority, and the JSON execution state SHALL be a projection of
broker and ledger state rather than a competing authority: direct JSON writes
SHALL NOT release gates, satisfy checkpoints, or alter supervised identity,
and the projection SHALL be derivable from broker-recorded receipts and
ledger records.

#### Scenario: Legacy runs are untouched by the supervisor package

- **WHEN** an ordinary (non-supervised) plan run executes with the supervisor
  package present
- **THEN** its JSON execution state handling is unchanged and no supervisor
  ledger is created or consulted

#### Scenario: The ledger never lands in the worktree

- **WHEN** a supervisor ledger is created for a supervised job
- **THEN** the ledger file resides in service-owned storage outside the
  repository worktree, and no ledger file appears under `.opsx-plan/`

#### Scenario: The JSON projection follows broker state

- **WHEN** a broker receipt changes the gate state of a registered job and
  the JSON projection is regenerated
- **THEN** the projected state matches the broker and ledger records, and any
  direct JSON edit that disagrees with them has no authority

### Requirement: The plan loader validates and resolves `pause_before_human_only`

The plan loader SHALL accept an optional boolean `pause_before_human_only`
key on each `[[changes]]` entry. A non-boolean value SHALL be rejected with
a named error identifying the key and the change, rather than coerced by
truthiness. `pause_before_human_only = true` without `pause_before = true`
on the same change SHALL be rejected with a named error identifying the key
and the change.

The loader SHALL resolve the key into the loaded change configuration: when
`pause_before = true` is set and the key is absent, the resolved value SHALL
be human-only (`true`); an explicit `false` SHALL be preserved as `false`.
Manifests that never set the key SHALL load with exactly the same values for
every previously existing field.

Derived single-change manifests SHALL preserve the resolved value through
the existing serialize-and-round-trip verification path, so a regenerated
manifest cannot silently drop or alter the flag.

#### Scenario: Legacy manifest loading is unchanged

- **WHEN** a manifest with `pause_before = true` and no
  `pause_before_human_only` key is loaded
- **THEN** the change configuration resolves `pause_before_human_only` to
  `true` and every previously existing field loads exactly as before

#### Scenario: Explicit delegation survives loading

- **WHEN** a manifest sets `pause_before = true` and
  `pause_before_human_only = false` on a change
- **THEN** the loaded change configuration carries `pause_before = true` and
  `pause_before_human_only = false`

#### Scenario: Human-only without a gate is a named error

- **WHEN** a manifest sets `pause_before_human_only = true` on a change
  without `pause_before = true`
- **THEN** loading fails with a named error identifying the key and the
  change

#### Scenario: Non-boolean values are rejected

- **WHEN** a manifest sets `pause_before_human_only` to a non-boolean value
  such as a string or an integer
- **THEN** loading fails with a named error identifying the key and the
  change rather than coercing the value

#### Scenario: Derived manifests round-trip the flag

- **WHEN** a derived single-change manifest is generated for a change whose
  resolved `pause_before_human_only` differs from the loader default
- **THEN** the serialized manifest states the key explicitly and the
  round-trip verification preserves the resolved value

### Requirement: Supervised registration binds the job to the canonical plan manifest

Registering a supervised job for a plan SHALL capture the protected manifest
snapshot from the plan's canonical manifest content and record it with the
job at registration. Gate and dispatch decisions for the job SHALL be
evaluated against that protected snapshot, not against repo-writable copies.

A manifest that changes after registration SHALL NOT be silently adopted:
adopting a changed manifest SHALL require an explicit operator decision — a
new registration or an explicit revision — and dispatch against stale
material SHALL be blocked with the named stale-material error under the
existing revalidation requirement.

A supervised job's completion and plan retirement SHALL consume the same
manifest ground truth as an unsupervised run: the existing archive evidence
and the existing completed-plan retirement semantics. Supervision SHALL
introduce no separate plan-completion or plan-retirement authority.

#### Scenario: Registration snapshots the canonical manifest

- **WHEN** an operator registers a supervised job for a plan
- **THEN** the protected manifest snapshot is captured from the plan's
  canonical manifest content and recorded with the job at registration

#### Scenario: A changed manifest is not silently adopted

- **WHEN** the plan manifest changes after registration and the supervised
  job is about to dispatch
- **THEN** dispatch is blocked with the named stale-material error until the
  operator explicitly adopts the change through a new registration or an
  explicit revision

#### Scenario: Supervised completion uses the same plan ground truth

- **WHEN** a supervised job's plan completes
- **THEN** completion is determined from the same archive evidence as an
  unsupervised run, and the completed plan retires under the existing
  retirement semantics unchanged

### Requirement: Acceptance consumes the canonical manifest and artifact ground truth

The acceptance artifact revision SHALL be derived from the protected canonical
plan manifest snapshot and its dependency edges together with the change's
real artifacts, so that a change to the manifest, the dependency graph, or the
reviewed artifacts invalidates a stale acceptance. Acceptance SHALL consume the
same manifest ground truth as registration and as an unsupervised run: it SHALL
introduce no separate plan-completion authority and SHALL NOT treat a prior
archive as proof of done.

#### Scenario: A manifest change invalidates a stale acceptance

- **WHEN** the plan manifest or its dependency edges change after an
  acceptance verdict was recorded for a change
- **THEN** the recorded verdict no longer satisfies acceptance because its
  artifact revision no longer matches the canonical manifest ground truth

#### Scenario: Artifact changes invalidate a stale acceptance

- **WHEN** a change's proposal, design, tasks, spec deltas, or canonical spec
  references change after an acceptance verdict was recorded
- **THEN** the recorded verdict is stale and a fresh acceptance over the new
  revision is required

#### Scenario: Acceptance adds no plan-completion authority

- **WHEN** a supervised job's plan completion is evaluated
- **THEN** completion is determined from the same archive and check evidence
  as an unsupervised run, and the acceptance verdict does not by itself mark
  the plan or a change complete

### Requirement: Recovery revalidates archive and completion material against canonical ground truth

Incident recovery that touches archive or completion material SHALL consume the
same canonical manifest snapshot and artifact ground truth as an unsupervised
run. A partial archive, or an archive whose post-archive fast checks fail,
SHALL NOT be treated as a completed change, and recovery SHALL require an
appropriate fresh review over the repaired revision before completion is
reasserted.

A delta `MODIFIED` identity mismatch repaired by recovery SHALL derive the
corrected identity from the canonical specification, so the canonical
specification remains the authority for the requirement's intent and the
canonical specs are updated only through the existing archive and delta
application semantics. Recovery SHALL introduce no separate plan-completion,
archive, or spec-synchronization authority.

#### Scenario: A partial archive is not completion

- **WHEN** recovery encounters a change whose archive did not complete or whose
  post-archive fast check failed
- **THEN** the change is not treated as complete and a fresh review over the
  repaired revision is required before completion is reasserted

#### Scenario: A repaired delta derives its identity from the canonical spec

- **WHEN** recovery repairs a delta `MODIFIED` identity mismatch
- **THEN** the corrected identity is derived from the canonical specification
  and the canonical requirement's intent is preserved

#### Scenario: Recovery adds no archive or completion authority

- **WHEN** recovery repairs archive or completion material for a change
- **THEN** completion and archive state are still determined by the existing
  archive evidence and delta-application semantics, not by a recovery outcome
  alone
