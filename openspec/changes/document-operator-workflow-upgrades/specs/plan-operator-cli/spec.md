## ADDED Requirements

### Requirement: Operator documentation describes the upgraded `opsx-plan` workflow end to end

The repository SHALL provide operator-facing documentation for `opsx-plan` that
explains the upgraded workflow from plan compilation and activation through run
supervision.

That documentation SHALL cover, at minimum:

- active-plan activation and omitted-plan resolution precedence
- `opsx-plan doctor` preflight usage
- `opsx-plan run` with time and spend budget controls
- batch `approve --all`, `accept --all`, and `reset --failed` gate handling
- `opsx-plan logs` usage for current or recent stage output
- notification hook behavior and event coverage

The documentation SHALL include at least one worked example that starts with
`opsx-plan compile` and continues through an operator-driven plan run using the
final command surface.

#### Scenario: Operator docs cover the final CLI workflow

- **WHEN** an operator reads the documented `opsx-plan` workflow after the
  operator-workflow-upgrade series lands
- **THEN** the documentation includes activation, doctor, run, budgets, gate
  controls, logs, notifications, and a worked compile-to-run example using the
  final command names

### Requirement: Operator documentation makes default-off and override behavior explicit

The same operator documentation SHALL identify which new operator-facing
features are disabled by default and SHALL document the one-run overrides or
precedence rules that change behavior for a single invocation.

At minimum, the documentation SHALL explicitly describe:

- the precedence of explicit plan argument, `OPSX_PLAN`, and the active-plan pointer
- that budget controls are opt-in flags
- the operator-visible outcome of doctor failures and budget-triggered stops

#### Scenario: Operator docs explain defaults and precedence clearly

- **WHEN** an operator checks whether a new CLI workflow feature is always on,
  optional, or invocation-scoped
- **THEN** the documentation states the default behavior and names the
  precedence rule or flag that changes it
