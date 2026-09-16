## ADDED Requirements

### Requirement: The OpenCode adapter installs and verifies the supervised agents and supervision skill

The OpenCode adapter SHALL provide agent definitions
`adapters/opencode/agents/opsx-supervisor.md`,
`opsx-acceptance-reviewer.md`, `opsx-fixer.md`, and `opsx-verifier.md`, each
carrying `model: "{env:OPSX_<ROLE>_MODEL}"` and
`variant: "{env:OPSX_<ROLE>_VARIANT}"` placeholders substituted at install
time in the same manner as the existing worker agents, and each carrying a
least-privilege `permission` block expressing that role's tool and subagent
allowlist. The adapter SHALL install the supervision skill alongside the
existing skills. The adapter installer's verification SHALL byte-compare
each installed supervised agent and the supervision skill against the
repository source and report any missing or differing file by name, in the
same manner as the existing plan-authoring reference verification.

#### Scenario: Install deploys the supervised agents with substitution

- **WHEN** the OpenCode adapter installer runs with the supervised roles
  resolved
- **THEN** the four agent definitions are installed with their
  `{env:...}` placeholders replaced by the resolved models and variants, and
  the supervision skill is installed alongside the existing skills

#### Scenario: Verification reports a stale or missing contract

- **WHEN** the installer verification runs and an installed supervised agent
  or the supervision skill is missing or differs from the repository source
- **THEN** verification reports the differing file by name rather than
  passing

#### Scenario: Unresolved supervised roles do not break installation

- **WHEN** the installer runs on a machine with no supervised roles
  configured
- **THEN** the existing agents, skills, and commands install exactly as
  before, and any supervised-agent placeholder substitution fails closed or
  is reported rather than corrupting the legacy agents

### Requirement: Supervised prompts run under their concrete role agents

For a registered supervised job executing under the OpenCode adapter, the
session bridge SHALL name the concrete agent for each supervised prompt:
the primary session runs under `opsx-supervisor`, and `acceptance_reviewer`,
`fixer`, and `verifier` prompts run under their respective agents, so the
role's permission contract applies to the session. Stage workers —
implement, review, archive, and create — SHALL continue to use the existing
direct-dispatch path with their existing agents. Non-supervised runs SHALL
behave exactly as before.

#### Scenario: The primary session runs under the supervisor agent

- **WHEN** the session bridge creates or prompts the primary session for a
  registered supervised job
- **THEN** the session is bound to the `opsx-supervisor` agent so its
  bounded permission contract applies

#### Scenario: An auxiliary role prompt runs under its own agent

- **WHEN** the bridge prompts for the `acceptance_reviewer`, `fixer`, or
  `verifier` role
- **THEN** the prompt carries that role's concrete agent, and the role's
  allowlist applies to the session

#### Scenario: Stage workers and legacy runs are unchanged

- **WHEN** a supervised job dispatches an implement, review, archive, or
  create stage worker, or any run executes without supervision
- **THEN** dispatch uses the existing direct-dispatch path and existing
  agents with no behavioral change
