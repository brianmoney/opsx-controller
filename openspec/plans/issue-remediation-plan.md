---
title: Open-Issue Remediation (Autopilot Pause + Phase-Agent Generation)
doc_type: implementation-plan
status: proposed
updated: 2026-10-06
---

# Open-Issue Remediation

## Purpose

Close out the open issue queue with two OpenSpec changes driven as one
`opsx-plan` run:

- **#33** — autopilot restart-thrashes on deterministic environment failures
  (dirty tracked worktree) instead of failing fast and waiting for an operator.
- **#32** — implementer/reviewer/archiver instruction bodies are hand-maintained
  across five distributions and drift; generate them from one canonical source.

Issue **#25** (unarchive/restore primitive) was verified as already implemented
on `main` and closed 2026-10-06; it has no change in this plan. Evidence:
`orchestrator/opsx-plan.py:2469` (+ wiring at `:3551`) and the integration
regression at `tests/supervisor/test_supervision_lifecycle.py:810`.

## Capability Ownership

- `plan-operator-cli` (existing) owns the autopilot failure-classification and
  durable-pause contract added by phase 1.
- `phase-agent-generation` (new) owns the canonical phase bodies, adapter
  templates, deterministic generation, and drift checking added by phase 2.

## Phase 1: Autopilot deterministic pause (issue #33)

### Change: `fix-autopilot-deterministic-pause`

**Purpose:** Split environment failures into deterministic and transient.
Deterministic failures write a durable pause marker
(`.opsx-plan/autopilot-paused.json`), escalate exactly once, and exit 0 so
systemd leaves the unit down until an operator resumes; transient failures keep
exit 2 and systemd retries them. Add `opsx-plan autopilot status` and
`opsx-plan autopilot resume`.

**Depends on:** None.

**Capabilities:** `plan-operator-cli`.

**Scope:** `lib/orchestrator/cmd_autopilot.py` (classification, marker
lifecycle, preflight escalation, status/resume), `orchestrator/opsx-plan.py`
(action parser/dispatch), `systemd/opsx-autopilot.service.in` comment block,
autopilot tests, and the autopilot runbook/operator-workflow/README/skill
documentation.

**Out of scope:** Changing `Restart=`, `RestartSec=`, or the StartLimit bounds;
enabling the unit; an operator CLI for issue #25.

**Success parameters:** A dirty-tree start pauses once, writes one digest, and
exits 0; a subsequent start no-ops with exit 0 and no new escalation; `resume`
refuses while the tree is still dirty and clears the marker after it is
cleaned; a transient lock-contention failure still exits 2; both test suites
and `openspec validate --all --strict` pass.

## Phase 2: Phase-agent generation (issue #32)

### Change: `add-phase-agent-generation`

**Purpose:** Author one canonical client-neutral body per phase under
`core/phase-agents/`, keep client metadata and steps in thin per-adapter
templates, and render every adapter agent file plus the Codex and Claude plugin
copies with a stdlib generator that supports a drift check.

**Depends on:** None. It is serialized after phase 1 only to keep review
bandwidth focused; the two changes touch disjoint files.

**Capabilities:** `phase-agent-generation`.

**Scope:** New `core/phase-agents/`, `adapters/*/agent-templates/`,
`scripts/generate-phase-agents.py`, and
`tests/adapters/test_generated_phase_agents.py`; regenerated
`adapters/{opencode,claude-code,codex-cli,dsh}/agents/`,
`adapters/codex-cli/plugin/agents/`, and `plugins/opsx-controller/agents/`;
parity extensions in `tests/adapters/test_phase_completion_instructions.py`;
docs in `AGENTS.md`, `docs/adapters.md`, and `core/README.md`.

**Out of scope:** The plan-author agents and the supervised agents
(`opsx-supervisor`, `opsx-acceptance-reviewer`, `opsx-fixer`, `opsx-verifier`)
are not covered by this change.

**Success parameters:** A shared policy edit needs only the canonical body; all
distributions regenerate byte-identically including the archiver; metadata
(`{env:}` tokens, sandbox modes, tools/effort, dsh no-frontmatter) is preserved;
the drift check fails on a hand edit; both test suites and
`openspec validate --all --strict` pass.

## Verification

- `python3 -m unittest discover -t . -s tests`
- `node tests/opencode/test-opsx-usage-emitter.js`
- `openspec validate fix-autopilot-deterministic-pause --strict` and
  `openspec validate add-phase-agent-generation --strict`
- `openspec validate --all --strict`
- Maintainer deploy after merge: `bash install.sh --global --verify`
  (autopilot unit template and agent files are installed data).
