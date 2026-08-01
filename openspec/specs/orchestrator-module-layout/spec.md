# orchestrator-module-layout Specification

## Purpose

Define how the orchestrator's Python implementation is organized into
importable runtime modules under `lib/orchestrator/`, and how that layout
stays behavior-preserving, testable, and diagnosable as the entrypoint and
its installed runtime package evolve.

## Requirements

### Requirement: Orchestrator source is organized into importable runtime modules

The orchestrator's Python implementation SHALL be distributed across modules in
a `lib/orchestrator/` package rather than held entirely in the
`orchestrator/opsx-plan.py` entrypoint. Each module SHALL cover one named
concern and SHALL be importable as `lib.orchestrator.<module>` without
executing the CLI.

The package SHALL sit alongside the existing `lib/metrics`, `lib/pricing`, and
`lib/models` runtime packages and SHALL be resolved by the same `sys.path`
mechanism the entrypoint already uses for those packages.

#### Scenario: A module is imported without running the CLI

- **WHEN** a test or tool imports `lib.orchestrator.cost`
- **THEN** the import succeeds, no argument parsing occurs, no process is
  spawned, and no file under `.opsx-plan/` is read or written

#### Scenario: Modules resolve from the repository checkout

- **WHEN** `orchestrator/opsx-plan.py` is executed from a repository checkout
- **THEN** it imports the orchestrator modules from that checkout's `lib/`
  directory rather than from any installed copy

### Requirement: The entrypoint retains the command-line surface

`orchestrator/opsx-plan.py` SHALL remain the executable entrypoint and SHALL
continue to own subcommand registration and dispatch. Moving a command's
implementation into a module SHALL NOT change the subcommand's name, flags,
defaults, exit codes, or output.

#### Scenario: Subcommand invocation is unchanged

- **WHEN** an operator runs `opsx-plan report` or `opsx-plan dashboard` with
  any combination of flags accepted before this change
- **THEN** the command is accepted, produces byte-identical output for
  identical telemetry and state inputs, and exits with the same status code

#### Scenario: Help output is unchanged

- **WHEN** an operator runs `opsx-plan --help` or any subcommand's `--help`
- **THEN** the listed subcommands and their flags are the same as before the
  extraction

### Requirement: Extraction preserves observable behavior

Moving code into a module SHALL be behavior-preserving. The extracted
definitions SHALL keep their names and signatures, and no requirement in
`plan-run-observability` SHALL change as a result of the move.

A module extraction SHALL NOT introduce an import cycle: modules SHALL depend
only on modules extracted before them or on shared runtime packages.

#### Scenario: The existing suite passes unchanged in count

- **WHEN** the full test suite runs after an extraction
- **THEN** it passes with no fewer tests than before the extraction, and no
  test is skipped or deleted to accommodate the new layout

#### Scenario: Report and dashboard output is identical

- **WHEN** `opsx-plan report --json` and `opsx-plan dashboard` run against a
  fixed telemetry directory and plan state before and after the extraction
- **THEN** the JSON payload and the generated HTML are identical

#### Scenario: A cyclic import is rejected

- **WHEN** an extracted module imports a module that transitively imports it
- **THEN** the layout is invalid and the extraction is reworked so the
  dependency runs in one direction only

### Requirement: Test modules mirror the source modules they cover

Tests for an extracted module SHALL live in a test module named for it, rather
than remaining in the aggregate `tests/orchestrator/test_opsx_plan.py`. Moved
tests SHALL keep their assertions; only the import target and any monkeypatch
target SHALL change.

Where a moved test patches a definition, it SHALL patch that definition in the
module where the calling code resolves it, so the patch remains effective.

#### Scenario: Moved tests are discovered

- **WHEN** the suite is discovered from the repository root
- **THEN** the new test modules are collected, and the total test count is at
  least the pre-extraction count

#### Scenario: A patch applied to the wrong module is detected

- **WHEN** a moved test patches a definition that the code under test resolves
  from a different module
- **THEN** the test fails rather than silently exercising the real definition

### Requirement: A partial installation is diagnosed rather than crashing

`opsx-plan` SHALL exit with a clear message when it runs against an installed
runtime that predates this layout, meaning one where the shared runtime
packages are present but the orchestrator package is missing. The message
SHALL name the missing package and instruct the operator to rerun a global
installer. `opsx-plan` SHALL NOT surface an unhandled `ModuleNotFoundError`
traceback in this case.

`opsx-plan doctor` SHALL report such an installation as stale.

#### Scenario: Stale install missing the orchestrator package

- **WHEN** an operator runs an installed `opsx-plan` whose
  `~/.local/lib/opsx-controller/lib` contains `metrics`, `pricing`, and
  `models` but no orchestrator package
- **THEN** the command exits non-zero with a message naming the missing
  package and directing the operator to rerun an adapter's global installer

#### Scenario: Doctor flags the partial runtime

- **WHEN** `opsx-plan doctor` runs against that same installation
- **THEN** it reports the installed runtime as stale
