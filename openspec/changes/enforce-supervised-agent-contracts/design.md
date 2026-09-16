# Design: enforce-supervised-agent-contracts

## Context

See proposal.md — Why. Load-bearing facts about the current tree:

- Supervised roles `supervisor`, `supervised_author`, `acceptance_reviewer`,
  `fixer`, `verifier` are registered optional roles
  (`lib/models/types.py::OPTIONAL_ROLES`); `ROLE_ENV`/`ROLE_VARIANT_ENV`
  generate `OPSX_<ROLE>_MODEL` / `OPSX_<ROLE>_VARIANT` for every role.
  `lib/supervisor/model_policy.py::check_dispatch` is the fail-closed model
  gate; `SUPERVISOR_ROLE` is allowlist-exempt and budget-counted.
- The operator authority boundary is landed: `lib/supervisor/authority.py`
  (worker principal, activation probe), `lib/supervisor/endpoints.py`
  (`ENDPOINT_OPERATOR` vs `ENDPOINT_WORKER`; `WORKER_HANDLERS` =
  `report_status`, `request_action`, `record_evidence`, `heartbeat`,
  `release_delegated_gate`; no credential material on either endpoint), and
  `lib/supervisor/broker_client.py` for transport.
- The session bridge is landed: `lib/supervisor/session_bridge.py` —
  `JournaledSessionBridge.prompt(role=, model=, agent=)` is the single
  choke point before any supervised model side effect (intent → reserve →
  dispatch → `SessionBridge.prompt_async`); `LoopbackTransport` rejects
  non-loopback targets; `create_session(agent=, model=)` binds an agent.
- Orchestrator dispatch is landed: `lib/orchestrator/journal_dispatch.py`
  (`gated_dispatch` = lock/authority/material/model-policy gates, then
  budget reservation; `note_spawned_process` binds subprocess identity;
  `record_session_binding` binds a native Task session identity reported
  through the worker endpoint — `tests/supervisor/test_action_journal.py::DispatchPathTests`
  covers both).
- OpenCode agents are markdown + YAML frontmatter
  (`adapters/opencode/agents/opsx-{implementer,reviewer,archiver}.md`) with
  `{env:OPSX_<ROLE>_MODEL}` placeholders substituted line-wise by
  `lib/install-common.sh::install_agent`. OpenCode permission values accept
  per-pattern objects (`bash: {"*": deny, "x *": allow}`) where the **last**
  matching rule wins, so broad rules come first and narrow exceptions last.
- Skills deploy by directory copy: `adapters/opencode/install.sh::install_skills`
  copies `skills/*` wholesale, so a new `skills/opsx-supervision/` needs no
  installer logic of its own.
- Installer verification today covers only the plan-authoring reference
  (`verify_plan_authoring_reference`, byte-compare with `cmp -s`). No
  agent or skill verification exists.
- `tests/supervisor/test_module_layout.py` AST-enforces acyclic imports in
  `lib/supervisor/`; a new module must not import `session_bridge` or
  `endpoints` (they will import it).

## Goals / Non-Goals

**Goals:**

- Four concrete OpenCode agents (`opsx-supervisor`,
  `opsx-acceptance-reviewer`, `opsx-fixer`, `opsx-verifier`) plus the
  `opsx-supervision` skill, installed and installer-verified.
- A per-role least-privilege permission contract: no recursive arbitrary
  dispatch, no non-allowlisted subagents, no shell bypass into a model
  client.
- The primary's bounded surface: read evidence; invoke only the tracked
  service tool; never arbitrary Bash or Task.
- A named, fail-closed pre-prompt enforcement step for model credentials
  and network egress at both supervised prompt choke points.
- Journaled worker-initiated delegation on both dispatch paths, and a
  spoofed/escalated worker surfaced as a blocked policy violation incident.
- Fixer/verifier routing semantics: mechanical work down, hard judgments
  back to the primary; verifier independence (specs —
  `task-completeness-gates`).

**Non-Goals:**

- The acceptance stage's revision binding and verdict flow
  (`add-acceptance-review-stage`), lifecycle CLI
  (`add-supervised-plan-lifecycle`), incident repair policy
  (`add-bounded-incident-recovery`), budgets, watchdog.
- Any change to the legacy `opsx-implementer`/`opsx-reviewer`/
  `opsx-archiver` agents or to non-supervised dispatch.
- Non-OpenCode adapters' supervised agents (future work, per the plan).
- Deploying or qualifying a production model gateway and real inexpensive
  models — operator `(manual)` follow-up; this change enforces that
  *some* enforced path is configured, not which one.

## Decisions

### D1. Concrete agents through the existing install path

Add `adapters/opencode/agents/opsx-{supervisor,acceptance-reviewer,fixer,verifier}.md`
in the established frontmatter format with
`model: "{env:OPSX_<ROLE>_MODEL}"` / `variant: "{env:OPSX_<ROLE>_VARIANT}"`.
`install_agent`'s substitution is already generic over role env vars
(`ROLE_ENV` covers the optional supervised roles), so no substitution
changes are needed. Per-role permission matrix (last-match-wins ordering
for `bash`):

| Agent | read/glob/grep | edit | bash | task | skill |
|---|---|---|---|---|---|
| `opsx-supervisor` | allow | deny | `"*": deny`, then `"opsx-supervise *": allow` | deny | `"*": deny`, then `"opsx-supervision": allow` |
| `opsx-fixer` | allow | allow | allow | deny | deny |
| `opsx-verifier` | allow | deny | allow | deny | deny |
| `opsx-acceptance-reviewer` | allow | deny | allow | deny | deny |

All four keep the existing worker conventions: `question: deny`,
`external_directory` default-deny with the installed-prompt-file
exceptions, `hidden: true`, `mode: all`. Denying `task` on every
supervised agent is what makes delegation non-recursive by construction;
the primary's judgments happen in-session.

Alternatives considered: generating the agents from a single template at
install time — rejected; static files keep installer verification simple
and match the three legacy agents. Sharing one generic "supervised worker"
agent with per-prompt role text — rejected; the permission contract must be
per role, and OpenCode binds permissions per agent.

### D2. The tracked service tool is `opsx-supervise`, a thin worker-endpoint shim

The primary's only side-effecting capability is a small executable the
adapter installer deploys (alongside its other support files) that maps
verbs 1:1 onto the worker-actions endpoint via
`lib/supervisor/broker_client.py`: report status, request an action,
record evidence, heartbeat, release a delegated gate. Journaling is then
structural rather than voluntary: the endpoint handlers already write the
ledger before acting, so every service-tool invocation is a journaled
supervised action with the primary's session identity bound. The
`opsx-supervisor` agent's `bash` permission allows only the
`opsx-supervise *` invocation pattern; everything else is denied.

Alternatives considered: an OpenCode plugin custom tool (`tool: {...}`) —
rejected; the repo ships no TypeScript plugin surface today
(`plugins/opsx-controller/` holds agents/skills/readme), and a JS runtime
dependency contradicts the stdlib-only supervision path. Exposing
`broker_client` as a Python API the primary "reads evidence" through —
rejected; the primary is an LLM session that invokes tools, not a Python
caller, and an unmediated API would bypass the endpoint's journaled
handlers.

### D3. Pre-prompt egress enforcement as a named step in `lib/supervisor/agent_contracts.py`

New stdlib-only module `lib/supervisor/agent_contracts.py` (imports only
`ledger`/`clock`-level modules to preserve the acyclic layout) provides:

- `TransportDecision` — `{enforced, path: "gateway" | "isolated-transport", detail}`.
- `evaluate_transport(environ, config)` — pure decision: enforced when a
  trusted model gateway endpoint is configured (operator env
  `OPSX_MODEL_GATEWAY_ENDPOINT`), or when the isolated-transport conditions
  hold (loopback-only target to the service-owned session server **and** a
  worker environment free of provider credentials). Otherwise unenforced.
- `assert_pre_prompt_transport(...)` — raises the named
  `EgressEnforcementError` before any prompt side effect; on success the
  decision is recorded as action evidence **before** the dispatch record.
- `check_session_contract(policy, role, observed_agent, requested_permissions)`
  — pure predicate for spoof/escalation detection (D5).

Call sites: `JournaledSessionBridge.prompt` (supervised primary and
auxiliary prompts) immediately after action intent and before dispatch;
and `lib/orchestrator/journal_dispatch.gated_dispatch` for supervised
stage-worker subprocess dispatch, as an additional named check after the
model-policy gate and before spawn. On failure the action is failed with
the gate reason; no prompt or subprocess exists yet, so nothing needs
unwinding. Provider credentials live behind the gateway or in the
service-owned server; the worker principal's environment never carries a
reusable provider credential.

Alternatives considered: after-the-fact usage detection — rejected
outright by the plan. Requiring a configured gateway with no
isolated-transport equivalent — rejected; the plan explicitly admits
"an equivalently enforced isolated transport", and the loopback
service-owned bridge already provides it for the primary session.

### D4. Worker-initiated delegation is brokered through the worker endpoint

The new agents deny `task` and (for the primary) arbitrary `bash`, so a
supervised worker cannot directly spawn an unjournaled delegation. When a
role's contract permits delegation, the worker requests it through the
worker-actions endpoint (`request_action`), which journals intent and
identity: a native Task session binding is reported back through
`record_evidence` and bound with `record_session_binding`; a subprocess
spawned on the worker's behalf is recorded with its process identity —
both landing in the same journal with the same lifecycle as orchestrator
dispatch. Delegation that cannot be journaled is denied by the permission
contract, never executed.

Alternatives considered: letting workers spawn directly and reconciling
identity afterward — rejected; it creates an unjournaled side-effect
window and violates intent-before-side-effects. Wrapping every worker in a
supervising ptrace/audit sandbox — rejected as disproportionate; the
permission contract plus the endpoint-brokered path covers the threat
model this change owns.

### D5. Spoof/escalation is a contract check plus a `policy_violation` incident

`check_session_contract` compares the observed agent/identity and any
requested capability against the registered role contract (from the job
policy's role pins and the agent registry). A mismatch — wrong agent,
unpinned model, broader tool request — blocks the dispatch and records a
`policy_violation` incident against the job; nothing executes. Wired at
two points: bridge session create/prompt (the bridge asserts the agent it
binds is the role's registered agent), and worker-endpoint request
handling (a worker asking for a capability beyond its contract is
refused). OpenCode's own permission denial remains the enforcement layer
inside the session; the contract check is what makes the event durable and
visible instead of a silent transcript blip.

Alternatives considered: relying on OpenCode permission denials alone —
rejected; denials live only in the transcript, and the plan requires
surfacing a spoofed or escalated worker as a blocked policy violation.
Trusting the worker-reported identity without comparison — rejected; the
whole point is detecting a spoof.

### D6. Installer verification is substitution-aware

Extend `adapters/opencode/install.sh` with
`verify_supervised_agents_and_skill`, following the
`verify_plan_authoring_reference` pattern: the skill directory is
byte-compared with `cmp -s` (it carries no placeholders); each agent is
re-rendered from the repository source with the currently resolved
environment and compared against the installed file, with any missing or
differing file reported by name. When the supervised roles are unresolved
on a non-supervised machine, verification reports the supervised agents as
unconfigured rather than failing, and all legacy verification is
unchanged. Installer tests run in the existing temp-HOME sandbox
(`tests/installer/test_installers.py`).

Alternatives considered: byte-comparing installed agents against the raw
source — wrong; installed agents have substituted models, so comparison
must be substitution-aware. Skipping verification until the packaging
change — rejected; the plan requires installer verification in *this*
change.

### D7. The supervision skill briefs the bounded loop

`skills/opsx-supervision/SKILL.md` (plus metadata, matching the existing
skill format) tells the primary: its inputs (job id, evidence reads via
the service tool), the bounded loop (read journaled evidence → choose a
remedy or judgment → invoke the tracked tool), what it must never do
(arbitrary Bash/Task, direct file edits, operator endpoints), and how
verdicts/fixes route (fixer applies, verifier independently validates,
hard judgments stay with the primary). Deployment is free via
`install_skills`' directory copy.

## Risks / Trade-offs

- [Bash patterns can be circumvented by indirection (`bash -c`,
  aliases, scripts)] → the permission layer is defense in depth, not the
  trust root: the worker endpoint carries no operator authority, every
  service-tool action is journaled, the egress gate (D3) blocks unenforced
  model traffic pre-prompt, and a detected bypass is a `policy_violation`
  incident (D5).
- [Credential-free worker environments may break provider auth for
  supervised stage-worker subprocesses] → supervised dispatch fails closed
  with the named `EgressEnforcementError` until the operator configures
  the gateway or isolated transport; non-supervised runs are untouched.
- [Re-render verification may disagree with hand-edited installs] → that
  is the point; verification reports the file by name exactly like the
  existing authoring-reference check, and the fix is reinstalling.
- [OpenCode permission-pattern semantics (last-match-wins, pattern
  vocabulary) may shift across versions] → the installer verification pins
  the installed frontmatter and `tests/supervisor/test_agent_contracts.py`
  asserts the rules' shape, so an upstream change surfaces as a test
  failure rather than silent contract drift.
- [A new `opsx-supervise` shim expands the adapter install surface] → it
  is verified by the same substitution-aware check, and it is inert on
  machines that never register a supervised job.

## Migration Plan

No ledger schema migration and no manifest or policy format change. Deploy
with the normal adapter reinstall (`bash install.sh --global --verify` or
`bash adapters/opencode/install.sh --global --verify`), which now also
verifies the new agents and skill. Supervised jobs cannot be live yet (the
lifecycle change lands later), so no running job observes a behavior
change. Rollback: reinstall the previous revision; the new files are
additive and unused by legacy runs.
