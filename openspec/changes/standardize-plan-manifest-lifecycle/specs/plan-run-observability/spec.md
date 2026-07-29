## ADDED Requirements

### Requirement: Report and dashboard can target a single-change run by change id

The `report` and `dashboard` commands SHALL accept a `--for-change <change-id>` option that selects the run namespace of a single-change run without requiring the operator to name a manifest path.

`--for-change` SHALL be mutually exclusive with the positional plan argument, and SHALL be distinct from the existing `--change` filter, which narrows output within an already-resolved plan and SHALL retain its current meaning.

When `--for-change <change-id>` is given, the commands SHALL resolve the derived manifest at `.opsx-plan/plans/run-<change-id>.toml` when it exists.

#### Scenario: Report resolves a single-change run by id
- **WHEN** an operator runs `opsx-plan report --for-change vault-gardening-suggestions` and `.opsx-plan/plans/run-vault-gardening-suggestions.toml` exists
- **THEN** the report covers the `run-vault-gardening-suggestions` run namespace and produces the same output as passing that manifest path explicitly

#### Scenario: Dashboard resolves a single-change run by id
- **WHEN** an operator runs `opsx-plan dashboard --for-change vault-gardening-suggestions`
- **THEN** the dashboard is generated for the `run-vault-gardening-suggestions` run namespace and defaults its output to `.opsx-plan/dashboards/run-vault-gardening-suggestions.html`

#### Scenario: Unrelated active plan does not capture the request
- **WHEN** the active-plan pointer references an unrelated plan and an operator runs `opsx-plan report --for-change vault-gardening-suggestions`
- **THEN** the report covers the single-change run and not the active plan

#### Scenario: Conflicting selectors are refused
- **WHEN** an operator passes both a positional plan path and `--for-change`
- **THEN** the command exits with a usage error and produces no report or dashboard

### Requirement: Single-change reporting tolerates a missing derived manifest

When `--for-change <change-id>` is given and no derived manifest exists, the commands SHALL fall back to using the plan name `run-<change-id>` directly, without loading a manifest, provided the run's state file exists at `.opsx-plan/run-<change-id>.state.json`.

This fallback SHALL keep single-change runs recorded before derived manifests existed reportable from their retained telemetry and state.

When neither the derived manifest nor the run state file exists, the commands SHALL exit with an error naming the change id, and SHALL NOT emit an empty report or dashboard.

#### Scenario: Run predating derived manifests remains reportable
- **WHEN** an operator runs `opsx-plan report --for-change earlier-change` and `.opsx-plan/run-earlier-change.state.json` exists but no derived manifest does
- **THEN** the report is produced from the retained telemetry and state for the `run-earlier-change` namespace

#### Scenario: Unknown change id is refused
- **WHEN** an operator runs `opsx-plan report --for-change never-run-change` and neither a derived manifest nor a run state file exists for it
- **THEN** the command exits with an error naming `never-run-change` and produces no report

#### Scenario: Fallback reporting does not require a loadable manifest
- **WHEN** the fallback path is used because no derived manifest exists
- **THEN** the commands aggregate telemetry and state without invoking plan-manifest validation, since the plan name is the only manifest-derived input they consume
