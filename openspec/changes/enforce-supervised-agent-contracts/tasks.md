# Tasks: enforce-supervised-agent-contracts

## 1. Agent definitions and supervision skill

- [x] 1.1 Create `adapters/opencode/agents/opsx-supervisor.md` with frontmatter `model: "{env:OPSX_SUPERVISOR_MODEL}"`, `variant: "{env:OPSX_SUPERVISOR_VARIANT}"`, `hidden: true`, `mode: all`, and permission block: `read`/`glob`/`grep: allow`, `edit: deny`, `bash: {"*": deny, "opsx-supervise *": allow}` (broad rule first — last match wins), `task: deny`, `skill: {"*": deny, "opsx-supervision": allow}`, `question: deny`, `external_directory` default-deny with the installed-prompt-file exceptions used by the legacy agents; body defines the bounded primary loop: read journaled evidence, choose remedy or judgment, invoke only the tracked service tool, never arbitrary Bash or Task.
- [x] 1.2 Create `adapters/opencode/agents/opsx-fixer.md` with `{env:OPSX_FIXER_MODEL}`/`{env:OPSX_FIXER_VARIANT}` pins, `read`/`edit`/`glob`/`grep`/`bash: allow`, `task: deny`, `skill: deny`, `question: deny`; body: apply the primary-chosen mechanical repair, run the relevant checks, and return a machine-readable report without self-certifying completion.
- [x] 1.3 Create `adapters/opencode/agents/opsx-verifier.md` with `{env:OPSX_VERIFIER_MODEL}`/`{env:OPSX_VERIFIER_VARIANT}` pins, `read`/`glob`/`grep`/`bash: allow`, `edit: deny`, `task: deny`, `skill: deny`, `question: deny`; body: independently validate the actual diff and evidence for a repair and return a machine-readable verdict never derived from the fixer's report.
- [x] 1.4 Create `adapters/opencode/agents/opsx-acceptance-reviewer.md` with `{env:OPSX_ACCEPTANCE_REVIEWER_MODEL}`/`{env:OPSX_ACCEPTANCE_REVIEWER_VARIANT}` pins, `read`/`glob`/`grep`/`bash: allow`, `edit: deny`, `task: deny`, `skill: deny`, `question: deny`; body: review the real change artifacts and return an `accept`/`fix`/`escalate` outcome (stage wiring itself belongs to `add-acceptance-review-stage`).
- [x] 1.5 Create `skills/opsx-supervision/SKILL.md` (with the existing skills' frontmatter/metadata format) briefing the primary: inputs, the bounded loop, the service-tool verbs, what it must never do, and the fixer/verifier routing rule.

## 2. Tracked service tool

- [x] 2.1 Add `lib/supervisor/service_tool.py`: a stdlib-only verb dispatcher mapping the primary's service-tool verbs onto the worker-actions endpoint handlers via `lib/supervisor/broker_client.py`; it must resolve only the worker endpoint (never the operator endpoint) and must not import `session_bridge` or `endpoints` consumers in a way that breaks `tests/supervisor/test_module_layout.py`.
- [x] 2.2 Add the `opsx-supervise` shim to the OpenCode adapter install surface (`adapters/opencode/install.sh` support-file deployment): an executable that invokes the installed service-tool module with the worker socket environment, so the `opsx-supervisor` agent's `bash` allowlist pattern `opsx-supervise *` names a real binary.

## 3. Agent contracts module

- [x] 3.1 Create `lib/supervisor/agent_contracts.py` with `TransportDecision`, `evaluate_transport(environ, config)` (enforced when `OPSX_MODEL_GATEWAY_ENDPOINT` is configured, or when the target is loopback to the service-owned session server **and** the worker environment carries no provider credential), and `assert_pre_prompt_transport(...)` raising the named `EgressEnforcementError` before any prompt side effect and recording the decision as action evidence before the dispatch record.
- [x] 3.2 Add `check_session_contract(policy, role, observed_agent, requested_permissions)` as a pure predicate plus a helper that records a `policy_violation` incident against the job in the ledger.
- [x] 3.3 Verify `lib/supervisor/` imports stay acyclic and module-import discipline holds: `python3 -m unittest tests.supervisor.test_module_layout`.

## 4. Bridge and dispatch wiring

- [x] 4.1 Wire `assert_pre_prompt_transport` into `lib/supervisor/session_bridge.py::JournaledSessionBridge.prompt` after action intent and before dispatch: on `EgressEnforcementError` fail the action with the gate reason and issue no prompt; on success record the transport decision evidence before the dispatch record.
- [x] 4.2 Call the same enforcement in `lib/orchestrator/journal_dispatch.py::gated_dispatch` for supervised stage-worker dispatch, after the model-policy gate and before spawn, with the same named error and fail-closed semantics.
- [x] 4.3 Wire bridge session create/prompt to bind each role's concrete agent (`opsx-supervisor` for the primary; role agents for `acceptance_reviewer`/`fixer`/`verifier`) and run `check_session_contract`; on mismatch block the dispatch and record a `policy_violation` incident.
- [x] 4.4 Wire the worker-actions endpoint request path so a worker requesting a capability beyond its role contract is refused with a recorded `policy_violation`, and worker-initiated allowlisted delegation is journaled: native Task bindings reported through `record_evidence` and bound via `journal_dispatch.record_session_binding`, subprocess identity recorded against the owning action.

## 5. Installer and verification

- [x] 5.1 Confirm `install_agents` deploys the four new agents with `{env:...}` substitution from the resolved role environment, and that `install_skills` deploys `skills/opsx-supervision/`; installation on a machine with no supervised roles configured must leave the legacy agents byte-identical and report the supervised agents as unconfigured rather than failing.
- [x] 5.2 Add `verify_supervised_agents_and_skill` to `adapters/opencode/install.sh` following the `verify_plan_authoring_reference` pattern: byte-compare the installed supervision skill with `cmp -s`, re-render each supervised agent from source with the current resolved environment and compare, and report any missing or differing file by name.
- [x] 5.3 Extend `tests/installer/test_installers.py` (temp-HOME sandbox): install deploys agents/skill/shim, verification passes on a clean install, and verification reports a stale or missing installed agent by name.

## 6. Documentation

- [x] 6.1 Add the agent-contract section to `core/plan-supervision.md`: the per-role tool/subagent allowlists and non-recursive dispatch, the primary's bounded service-tool surface, the named pre-prompt gateway/egress enforcement step, journaled worker-initiated delegation on both paths, and `policy_violation` surfacing for spoofed or escalated workers.
- [x] 6.2 Review `models.example.toml` and installer `--verify` output text so the supervised roles and the new verification step are accurately described.

## 7. Tests and validation

- [x] 7.1 Create `tests/supervisor/test_agent_contracts.py` asserting: the installed concrete agents and skill (via the installer sandbox); a worker cannot dispatch a non-allowlisted agent; a shell bypass into a model client/agent runner is blocked or surfaced as a `policy_violation`; pre-prompt enforcement blocks with the named `EgressEnforcementError` when unenforced and records the decision before dispatch when enforced; Task and subprocess delegation paths are journaled; the primary's surface is bounded to evidence reads plus the tracked service tool.
- [x] 7.2 Add spoof/escalation coverage to `tests/supervisor/test_agent_contracts.py`: a mismatched observed agent blocks dispatch with a recorded `policy_violation` incident, and a capability escalation request is refused and recorded, never defaulted.
- [x] 7.3 Add verifier-independence coverage: a fixer completion claim is not consumed until the verifier validates the actual diff, and a contradicting verifier verdict blocks the repair while the existing task-completeness gates remain unaffected.
- [x] 7.4 Run `python3 -m unittest discover -t . -s tests` from the repository root — all suites pass, including the existing `tests/supervisor/` and `tests/installer/` suites.
- [x] 7.5 Run `node tests/opencode/test-opsx-usage-emitter.js` — passes.
- [x] 7.6 Run `openspec validate enforce-supervised-agent-contracts --strict` — passes.
