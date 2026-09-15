## ADDED Requirements

### Requirement: The journal dispatch integration lives in a concern-named orchestrator module

The supervised journal dispatch integration — the pre-dispatch gate
evaluation, journal lifecycle calls around stage dispatch, worker identity
capture, outcome and evidence recording, and replay re-observation — SHALL
live in a concern-named, importable runtime module under `lib/orchestrator/`,
following the shared runtime-module discipline. The entrypoint SHALL retain
the command-line surface and call into the module; the integration logic
SHALL NOT be added as new inline entrypoint code beyond the calls themselves.
The module SHALL be importable and exercisable in tests without invoking the
command-line surface.

#### Scenario: The integration module is importable on its own

- **WHEN** the journal dispatch integration module is imported directly by a
  test or another runtime module
- **THEN** it loads without side effects and exposes the dispatch-boundary
  operations the entrypoint calls

#### Scenario: The entrypoint delegates rather than inlining

- **WHEN** a registered supervised job dispatches a stage through any
  entrypoint run path
- **THEN** the gate evaluation, journal lifecycle, and outcome recording are
  performed by the concern-named module, with the entrypoint providing only
  command parsing and call-site wiring
