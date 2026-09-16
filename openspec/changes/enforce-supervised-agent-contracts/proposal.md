# Proposal: enforce-supervised-agent-contracts

## Why

The supervision stack now has registered supervised model roles with a
fail-closed model policy (`register-supervised-model-roles`), an enforced
operator authority boundary (`establish-operator-authority-boundary`), and a
journaled OpenCode session bridge (`add-opencode-session-bridge`). What it
does not yet have is the agents themselves: no concrete agent definition
exists for `supervisor`, `acceptance_reviewer`, `fixer`, or `verifier`, no
supervision skill briefs the primary, and nothing constrains what an
inexpensive worker session may *do* once dispatched. The model policy pins
which model a role runs; nothing yet pins which tools and subagents that
session may invoke, so a cheap worker could dispatch an arbitrary Task agent
or shell around the allowlist, and model credentials or network egress are
observed only as usage after the fact rather than enforced before a prompt
executes. This change installs the concrete supervised agents and skill and
enforces the per-role contracts at the bridge and journal, before the live
lifecycle and acceptance changes depend on them.

## What Changes

- Add concrete OpenCode agent definitions `opsx-supervisor`,
  `opsx-acceptance-reviewer`, `opsx-fixer`, and `opsx-verifier` under
  `adapters/opencode/agents/`, each pinned to its role through
  `{env:OPSX_<ROLE>_MODEL}` / `{env:OPSX_<ROLE>_VARIANT}` substitution and
  carrying a least-privilege `permission` block. The legacy
  `opsx-implementer`, `opsx-reviewer`, and `opsx-archiver` agents are not
  modified.
- Add the supervision skill under `skills/` that briefs the primary session
  on its bounded loop: read journaled evidence, choose among policy-bound
  options, and invoke only the tracked service tool.
- Extend the OpenCode adapter installer to deploy the new agents and skill,
  and extend `--verify` to byte-compare the installed agents and skill the
  way `verify_plan_authoring_reference` already does for the authoring
  reference.
- Enforce per-role contracts at runtime: a constrained tool and subagent
  allowlist per role with no recursive arbitrary dispatch; the frontier
  primary session may read evidence and invoke only the tracked, journaled
  service tool — never arbitrary Bash or Task agents.
- Add a named, fail-closed enforcement step for model credentials and
  network egress that runs *before* a prompt executes at the session-bridge
  choke point, so model traffic flows only through the trusted model gateway
  or an equivalently enforced isolated transport and no reusable provider
  credential reaches a worker environment.
- Journal worker-initiated delegation — native Task dispatch inside a worker
  session and subprocess dispatch alike — through the worker endpoint into
  the action journal, and surface a spoofed or escalated worker as a blocked
  policy violation incident rather than executing it.
- Route mechanical investigation and repair to the inexpensive fixer and
  independent verifier agents; hard judgments (accept/fix/escalate
  decisions, remedy selection) return to the primary instead of being pushed
  down to an expensive subagent.

Out of scope (unchanged by this change): the acceptance stage itself and its
revision binding (`add-acceptance-review-stage`), budget policy
(`add-supervision-budgets` already landed), watchdog behavior
(`add-watchdog-reconstitution`), lifecycle registration commands
(`add-supervised-plan-lifecycle`), and any change to the legacy
reviewer/archiver agents or non-supervised dispatch.

## Capabilities

### New Capabilities

(None.)

### Modified Capabilities

- `durable-plan-supervision`: adds the supervised agent contract to the
  proposed capability — concrete installed agent definitions and the
  supervision skill, per-role tool and subagent allowlists with no recursive
  arbitrary dispatch, the primary's bounded evidence-read plus
  tracked-service-tool surface, the named pre-prompt gateway/egress
  enforcement step, journaled worker delegation on both dispatch paths, and
  spoofed/escalated-worker handling as a blocked policy violation.
- `plan-driven-opencode-execution`: the OpenCode adapter installs the four
  new supervised agents with model/variant env substitution and permission
  frontmatter, the installer verifies them, and the session bridge selects
  the concrete agent for each supervised prompt while stage workers keep the
  existing direct-dispatch path.
- `task-completeness-gates`: the fixer and verifier repair loop never
  self-certifies completion — a fixer report is always validated by the
  independent verifier against the actual diff, and these supervised repair
  verdicts feed but never replace the existing implement/review/archive
  task-completeness gates.

`adapter-model-configuration` is intentionally not modified: the roles,
allowlist, and resolver semantics were fixed by
`register-supervised-model-roles` and this change consumes them unchanged.
`shared-orchestrator-installation` is intentionally not modified: agent and
skill deployment is OpenCode-adapter installer surface, not orchestrator
runtime packaging.

## Impact

- **Code:** new `adapters/opencode/agents/opsx-supervisor.md`,
  `opsx-acceptance-reviewer.md`, `opsx-fixer.md`, `opsx-verifier.md`; new
  `skills/opsx-supervision/`; `adapters/opencode/install.sh` and
  `lib/install-common.sh` verification; new contract-enforcement module in
  `lib/supervisor/` (agent contracts, pre-prompt egress gate) wired into
  `lib/supervisor/session_bridge.py` and
  `lib/orchestrator/journal_dispatch.py`; worker-endpoint handlers in
  `lib/supervisor/endpoints.py`.
- **Docs:** `core/plan-supervision.md` gains the agent-contract section;
  `models.example.toml` comments stay accurate for the supervised roles.
- **Tests:** new `tests/supervisor/test_agent_contracts.py`; installer
  verification coverage in `tests/installer/test_installers.py`; existing
  `tests/supervisor/` suites must keep passing.
- **Dependencies:** none new; stdlib-only Python and the existing shell
  installer.
- **Compatibility:** non-supervised runs are unchanged; the legacy agents
  and the direct-dispatch path are untouched; no new required model roles
  for existing users.
