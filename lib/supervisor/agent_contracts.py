"""Per-role supervised agent contracts: session identity and prompt transport.

This module owns two fail-closed contracts for supervised sessions:

- **Session contract.** Each supervised role has exactly one concrete agent
  and a least-privilege capability allowlist. A session whose observed agent,
  identity, or requested permissions do not match its registered role contract
  is a spoof or an escalation attempt: the dispatch is blocked and a
  ``policy_violation`` incident is recorded against the job, never silently
  granted or defaulted. :func:`check_session_contract` is the pure predicate;
  :func:`enforce_session_contract` adds the durable incident.
- **Pre-prompt transport.** Model credentials and network egress are enforced
  *before* a prompt executes rather than detected afterward.
  :func:`evaluate_transport` is the pure decision and
  :func:`assert_pre_prompt_transport` raises the named
  :class:`EgressEnforcementError` before any prompt side effect, recording the
  decision as action evidence **before** the dispatch record. The isolated
  path is bound to the *launched service-server identity* (a fenceable
  pid/start-time/boot identity plus the server's own address), never to a bare
  loopback hostname.
- **Repair consumption.** A repair is consumed only through
  :func:`assert_repair_consumable`, which requires an independent verifier
  verdict on the real diff; a fixer-only or same-session verdict is a recorded
  ``policy_violation`` and the consuming transition (resume/dispatch, service
  reset, or delegated release) is refused.

Design rules enforced here:

- Standard library only, and it imports no other runtime package.
- It imports only low-level supervisor modules (``ledger``/``clock``/``lock``
  level), never ``session_bridge`` or ``endpoints`` — those import it — so the
  package's acyclic import graph is preserved.
- I/O is limited to reading an environment mapping and, for the isolated
  transport decision, the low-level process identity probes in ``lock``
  (``/proc`` start time and boot id) that prove the launched session server is
  live. The functions otherwise operate on plain mappings and a ledger handle,
  and the incident/evidence writes go through the caller-supplied ledger's
  existing methods.

Importing this module has no side effects, parses no arguments, spawns no
process, and touches no ``.opsx-plan/`` state.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from lib.supervisor import lock as lock_mod
from lib.supervisor import model_policy as model_policy_mod

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# The operator-configured trusted model gateway. When this is set, model
# traffic is expected to flow through it and no worker needs a provider
# credential.
GATEWAY_ENDPOINT_ENV = "OPSX_MODEL_GATEWAY_ENDPOINT"

# Action-evidence kind carrying the pre-prompt transport decision. It is
# deliberately outside the decisive evidence vocabulary (``stage_result``,
# ``usage``, ``spawn_loss``, ``session_binding``), so recording it can never
# complete, fail, or reconcile an action on its own.
EVIDENCE_TRANSPORT_DECISION = "transport_decision"

# Action-evidence kind carrying a repair's fixer report and its independent
# verifier verdict. Like the transport decision it is non-decisive: a repair
# claim never completes an action, and consumption is a separate gate.
EVIDENCE_REPAIR = "repair_verdict"

# The incident kind a spoofed or escalated worker is surfaced as.
POLICY_VIOLATION_INCIDENT = "policy_violation"

# The identity of the launched service-owned session server, carried by the
# worker environment (worker-writable, so it is evidence, not authority) and
# asserted against the running server when the isolated transport is decided.
SERVER_IDENTITY_ENV = "OPSX_SUPERVISOR_SERVER_IDENTITY"
SERVER_ADDRESS_ENV = "OPSX_SUPERVISOR_SERVER_ADDRESS"

# Transport enforcement paths.
TRANSPORT_GATEWAY = "gateway"
TRANSPORT_ISOLATED = "isolated-transport"
TRANSPORT_UNENFORCED = "unenforced"

# The two supervised roles whose independence is load-bearing: a ``fixer``
# report is never consumed until a separate ``verifier`` session validates the
# actual diff.
FIXER_ROLE = "fixer"
VERIFIER_ROLE = "verifier"

# Credential name suffixes that mark a reusable provider credential.
_CREDENTIAL_SUFFIXES = (
    "_API_KEY",
    "_API_TOKEN",
    "_ACCESS_TOKEN",
    "_AUTH_TOKEN",
    "_SECRET_KEY",
    "_SECRET",
)

# Provider prefixes that make a credential-bearing environment variable a
# *model provider* credential (as opposed to an unrelated service token). A
# worker domain must never carry one of these.
_PROVIDER_CREDENTIAL_PREFIXES = (
    "alibaba",
    "anthropic",
    "azure",
    "cerebras",
    "cohere",
    "commandcode",
    "copilot",
    "dashscope",
    "deepseek",
    "fireworks",
    "gemini",
    "github",
    "google",
    "groq",
    "grok",
    "hf",
    "huggingface",
    "hyperbolic",
    "jina",
    "minimax",
    "mistral",
    "moonshot",
    "nvidia",
    "ollama",
    "openai",
    "openrouter",
    "perplexity",
    "replicate",
    "sambanova",
    "together",
    "voyage",
    "xai",
    "zhipu",
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class AgentContractError(Exception):
    """Base class for a supervised agent-contract refusal."""


class EgressEnforcementError(AgentContractError):
    """A prompt was refused before execution because enforcement is absent.

    Named explicitly so a caller (and a test) can distinguish a fail-closed
    egress block from any other dispatch failure.
    """

    def __init__(self, reason: str, *, decision: "TransportDecision | None" = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.decision = decision


class SessionContractViolation(AgentContractError):
    """A session's observed agent or requested capability broke its contract."""

    def __init__(self, reason: str, *, check: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.check = dict(check or {})


class RepairConsumptionError(AgentContractError):
    """A repair-consuming transition was refused a non-independent repair."""

    def __init__(self, reason: str, *, decision: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.decision = dict(decision or {})


# ---------------------------------------------------------------------------
# The role -> concrete agent / capability contract
# ---------------------------------------------------------------------------

# Every supervised policy role maps to exactly one concrete installed agent.
# The role-to-agent mapping is the contract the bridge and the worker endpoint
# assert against, so a session cannot run under a different agent than its
# registered role.
SUPERVISED_ROLE_AGENTS: dict[str, str] = {
    model_policy_mod.SUPERVISOR_ROLE: "opsx-supervisor",
    "implementer": "opsx-implementer",
    "reviewer": "opsx-reviewer",
    "archiver": "opsx-archiver",
    "supervised_author": "opsx-implementer",
    "acceptance_reviewer": "opsx-acceptance-reviewer",
    FIXER_ROLE: "opsx-fixer",
    VERIFIER_ROLE: "opsx-verifier",
    "implementer_escalation": "opsx-implementer",
}

# Per-role least-privilege capability allowlist, mirroring each installed
# agent's permission block. A capability outside this set is an escalation.
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    model_policy_mod.SUPERVISOR_ROLE: frozenset(
        {"read", "glob", "grep", "skill:opsx-supervision", "service_tool"}
    ),
    "implementer": frozenset({"read", "glob", "grep", "edit", "bash"}),
    "supervised_author": frozenset({"read", "glob", "grep", "edit", "bash"}),
    "implementer_escalation": frozenset({"read", "glob", "grep", "edit", "bash"}),
    "reviewer": frozenset({"read", "glob", "grep", "bash"}),
    "archiver": frozenset({"read", "glob", "grep", "edit", "bash"}),
    "acceptance_reviewer": frozenset({"read", "glob", "grep", "bash"}),
    FIXER_ROLE: frozenset({"read", "glob", "grep", "edit", "bash"}),
    VERIFIER_ROLE: frozenset({"read", "glob", "grep", "bash"}),
}

# The primary's bounded surface, asserted by tests: evidence reads plus the
# tracked service tool, and nothing else.
PRIMARY_SERVICE_TOOL_CAPABILITY = "service_tool"


def role_agent(role: Any) -> str | None:
    """Return the concrete agent bound to *role*, or ``None``."""
    if not isinstance(role, str):
        return None
    return SUPERVISED_ROLE_AGENTS.get(role.strip())


def role_capabilities(role: Any) -> frozenset[str]:
    """Return *role*'s capability allowlist, or the empty set for a stranger."""
    if not isinstance(role, str):
        return frozenset()
    return ROLE_CAPABILITIES.get(role.strip(), frozenset())


def _declared_pins(policy: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the policy's role pins, or ``None`` when unavailable.

    Accepts either the ledger's tagged decoder shape
    (``{"state": ..., "payload": {...}}``) or a raw ``model_selection``
    mapping. A payload with no pins is ``None`` — never an empty allowance.
    """
    if not isinstance(policy, Mapping):
        return None
    value = policy.get("model_selection")
    if isinstance(value, Mapping) and value.get("state") in (
        "versioned",
        "legacy_unversioned",
    ):
        value = value.get("payload")
    if not isinstance(value, Mapping):
        return None
    roles = value.get("roles")
    return dict(roles) if isinstance(roles, Mapping) and roles else None


def declared_model_pin(policy: Mapping[str, Any] | None, role: Any) -> str | None:
    """Return *role*'s exact pinned model identifier, or ``None`` when unpinned."""
    pins = _declared_pins(policy)
    if pins is None or not isinstance(role, str):
        return None
    pin = pins.get(role.strip())
    return pin if isinstance(pin, str) and pin.strip() else None


def model_identity_string(model: Mapping[str, Any] | None) -> str | None:
    """Normalize a bridge model mapping to the ``provider/model`` pin string.

    The bridge's ``{providerID, modelID}`` shape and the policy's exact
    ``provider/model`` identifier are the same identity expressed two ways, so
    the pin check compares normalized strings rather than raw mappings.
    """
    if not isinstance(model, Mapping):
        return None
    provider = model.get("providerID")
    model_id = model.get("modelID")
    if isinstance(provider, str) and isinstance(model_id, str):
        provider, model_id = provider.strip(), model_id.strip()
        if provider and model_id:
            return f"{provider}/{model_id}"
    return None


def check_model_pin(
    policy: Mapping[str, Any] | None,
    role: Any,
    requested_model: Any,
) -> dict[str, Any]:
    """Pure predicate: does *requested_model* equal the role's exact pin?

    The bridge forwards a caller-supplied model; this is what refuses an
    unpinned override. A requested identity that is absent, malformed, or
    merely different from the exact policy pin is never repaired by
    substituting the role's pin or another role's model.
    """
    role_name = role.strip() if isinstance(role, str) and role.strip() else ""
    pin = declared_model_pin(policy, role_name)
    requested = (
        requested_model.strip()
        if isinstance(requested_model, str) and requested_model.strip()
        else None
    )
    violations: list[dict[str, str]] = []
    if pin is None:
        violations.append(
            {
                "kind": "unpinned_role",
                "detail": (
                    f"role {role_name or role!r} has no model pin in the job "
                    "policy; a model request is never defaulted"
                ),
            }
        )
    if requested is None:
        violations.append(
            {
                "kind": "unbound_model",
                "detail": (
                    "the dispatch carries no model identity; a supervised "
                    "prompt is never issued without its pinned model"
                ),
            }
        )
    elif pin is not None and requested != pin:
        violations.append(
            {
                "kind": "model_pin_mismatch",
                "detail": (
                    f"requested model {requested!r} does not equal role "
                    f"'{role_name}'s exact pin {pin!r}; a model override is "
                    "refused, never substituted"
                ),
            }
        )
    return {
        "allowed": not violations,
        "role": role_name or None,
        "pin": pin,
        "requested_model": requested,
        "violations": violations,
        "reason": violations[0]["detail"] if violations else None,
    }


# ---------------------------------------------------------------------------
# Session contract
# ---------------------------------------------------------------------------


def check_session_contract(
    policy: Mapping[str, Any] | None,
    role: Any,
    observed_agent: Any,
    requested_permissions: Sequence[str] | None = None,
    requested_model: Any = None,
) -> dict[str, Any]:
    """Pure predicate: does this session match its registered role contract?

    A violation is recorded for an unregistered role, an agent that is not the
    role's concrete installed agent (an unbound agent counts as not matching),
    a role with no model pin in the job policy, a *requested_model* that is not
    the role's exact pin (an override is refused, never substituted), and any
    requested capability outside the role's allowlist. Nothing here mutates
    state or defaults a violation away: an escalation is never silently
    satisfied by a broader permission.

    *requested_model* is optional so a caller that checks the model separately
    (or has no model to check) does not have to supply it; when it is supplied
    the exact-pin equality is part of the same contract result.
    """
    violations: list[dict[str, str]] = []
    role_name = role.strip() if isinstance(role, str) and role.strip() else ""
    expected_agent = role_agent(role_name)
    if expected_agent is None:
        violations.append(
            {
                "kind": "unregistered_role",
                "detail": (
                    f"role {role!r} has no registered supervised agent contract"
                ),
            }
        )
    observed = observed_agent.strip() if isinstance(observed_agent, str) else None
    if expected_agent is not None and observed != expected_agent:
        violations.append(
            {
                "kind": "agent_mismatch",
                "detail": (
                    f"observed agent {observed or '(unbound)'!r} does not match "
                    f"role '{role_name}'s contract agent {expected_agent!r}"
                ),
            }
        )
    pins = _declared_pins(policy)
    if role_name and expected_agent is not None:
        if pins is None:
            violations.append(
                {
                    "kind": "policy_unavailable",
                    "detail": (
                        "the job policy carries no role pins; the role's pinned "
                        "identity cannot be established"
                    ),
                }
            )
        elif not pins.get(role_name):
            violations.append(
                {
                    "kind": "unpinned_role",
                    "detail": (
                        f"role '{role_name}' has no pin in the job policy; an "
                        "unpinned role is never defaulted"
                    ),
                }
            )
    if requested_model is not None:
        pin_check = check_model_pin(policy, role_name, requested_model)
        violations.extend(dict(v) for v in pin_check["violations"])
    allowed_capabilities = role_capabilities(role_name)
    for permission in list(requested_permissions or ()):
        if not isinstance(permission, str) or permission not in allowed_capabilities:
            violations.append(
                {
                    "kind": "capability_escalation",
                    "detail": (
                        f"requested capability {permission!r} is outside role "
                        f"'{role_name or role!r}'s contract"
                    ),
                }
            )
    reason = violations[0]["detail"] if violations else None
    return {
        "allowed": not violations,
        "role": role_name or None,
        "expected_agent": expected_agent,
        "observed_agent": observed,
        "allowlist": sorted(allowed_capabilities),
        "violations": violations,
        "reason": reason,
    }


def record_policy_violation(
    ledger: Any,
    job_id: int,
    *,
    role: Any = None,
    observed_agent: Any = None,
    expected_agent: Any = None,
    reason: str = "",
    run_id: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> int:
    """Record a ``policy_violation`` incident against *job_id*.

    The violation is durable job state, not a transcript blip: a spoofed or
    escalated worker is surfaced through the existing incident surface with the
    observed and expected identity in the summary.
    """
    summary_parts = [reason or "supervised session contract violated"]
    if role:
        summary_parts.append(f"role={role}")
    if observed_agent or expected_agent:
        summary_parts.append(
            f"observed_agent={observed_agent or '(unbound)'} "
            f"expected_agent={expected_agent or '(none)'}"
        )
    if detail:
        extra = ", ".join(f"{key}={value!r}" for key, value in sorted(detail.items()))
        if extra:
            summary_parts.append(extra)
    return int(
        ledger.record_incident(
            int(job_id),
            kind=POLICY_VIOLATION_INCIDENT,
            summary="; ".join(summary_parts),
            state="open",
            run_id=run_id,
        )
    )


def enforce_session_contract(
    ledger: Any,
    job_id: int,
    *,
    policy: Mapping[str, Any] | None,
    role: Any,
    observed_agent: Any,
    requested_permissions: Sequence[str] | None = None,
    requested_model: Any = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Check the contract and refuse with a recorded ``policy_violation``.

    Returns the check result when the session matches its contract; otherwise
    records the incident against the job and raises
    :class:`SessionContractViolation` before any side effect of the dispatch.
    """
    check = check_session_contract(
        policy, role, observed_agent, requested_permissions, requested_model
    )
    if check["allowed"]:
        return check
    incident_id = record_policy_violation(
        ledger,
        int(job_id),
        role=check.get("role") or role,
        observed_agent=check.get("observed_agent"),
        expected_agent=check.get("expected_agent"),
        reason=str(check.get("reason") or "session contract violated"),
        run_id=run_id,
        detail={"violations": [v["kind"] for v in check["violations"]]},
    )
    check = dict(check)
    check["incident_id"] = incident_id
    raise SessionContractViolation(
        f"supervised session refused: {check['reason']} "
        f"(policy_violation incident {incident_id} recorded against job {job_id})",
        check=check,
    )


# ---------------------------------------------------------------------------
# Supervised worker identity: authenticated, non-optional, never spoofable
# ---------------------------------------------------------------------------

# Every supervised worker request carries its role, its observed concrete
# agent, and the job's registered service identity. The fields are mandatory:
# a request that omits them is the *supervised* path running unsupervised and
# is refused with a recorded ``policy_violation`` rather than treated as a
# legacy caller.
WORKER_ROLE_FIELD = "role"
WORKER_AGENT_FIELD = "observed_agent"
WORKER_IDENTITY_FIELD = "service_identity"


def missing_worker_identity_fields(request: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Return the absent supervised-identity fields of a worker request, if any."""
    if not isinstance(request, Mapping):
        return (WORKER_ROLE_FIELD, WORKER_AGENT_FIELD, WORKER_IDENTITY_FIELD)
    missing: list[str] = []
    for field in (WORKER_ROLE_FIELD, WORKER_AGENT_FIELD, WORKER_IDENTITY_FIELD):
        value = request.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(field)
    return tuple(missing)


def check_worker_identity(
    request: Mapping[str, Any] | None, job: Any
) -> dict[str, Any]:
    """Pure predicate: is this supervised worker request fully identified?

    A supervised worker request must name its role, its observed concrete
    agent, and the job's registered service identity. A missing field, or an
    identity that is not the job's registered principal, is a spoof and is
    refused — never defaulted to the legacy unauthenticated path.
    """
    missing = missing_worker_identity_fields(request)
    violations: list[dict[str, str]] = [
        {
            "kind": "missing_worker_identity",
            "detail": (
                f"supervised worker request is missing required identity field "
                f"{field!r}; supervised requests are never treated as legacy"
            ),
        }
        for field in missing
    ]
    request = request if isinstance(request, Mapping) else {}
    identity = request.get(WORKER_IDENTITY_FIELD)
    registered = None
    if isinstance(job, Mapping):
        registered = job.get("owner_principal")
    elif job is not None:
        try:
            registered = job["owner_principal"]
        except Exception:  # noqa: BLE001 - unknown shape is absence
            registered = None
    if WORKER_IDENTITY_FIELD not in missing:
        if not registered:
            violations.append(
                {
                    "kind": "unregistered_job_identity",
                    "detail": (
                        "the job has no registered service identity; a worker "
                        "request cannot be bound to it"
                    ),
                }
            )
        elif identity != registered:
            violations.append(
                {
                    "kind": "worker_identity_mismatch",
                    "detail": (
                        f"requesting identity {identity!r} is not the job's "
                        f"registered service identity {registered!r}"
                    ),
                }
            )
    return {
        "allowed": not violations,
        "missing": list(missing),
        "requested_identity": identity,
        "registered_identity": registered,
        "violations": violations,
        "reason": violations[0]["detail"] if violations else None,
    }


# ---------------------------------------------------------------------------
# Repair verdicts: a fixer claim is never self-certifying
# ---------------------------------------------------------------------------


def check_verifier_independence(
    fixer_session_id: Any, verifier_session_id: Any
) -> dict[str, Any]:
    """True only when the verifying session is a different session.

    A repair validated by the session that produced it is not independently
    verified, whatever its verdict says.
    """
    fixer = str(fixer_session_id).strip() if fixer_session_id else ""
    verifier = str(verifier_session_id).strip() if verifier_session_id else ""
    if not fixer or not verifier:
        return {
            "independent": False,
            "reason": (
                "both the fixer and verifier session identities are required to "
                "establish independence"
            ),
        }
    if fixer == verifier:
        return {
            "independent": False,
            "reason": "the verifier session is the fixer session",
        }
    return {"independent": True, "reason": None}


def repair_consumable(
    *,
    fixer_report: Mapping[str, Any] | None,
    verifier_verdict: Mapping[str, Any] | None,
    fixer_session_id: Any = None,
    verifier_session_id: Any = None,
) -> dict[str, Any]:
    """Decide whether a repair may be consumed by a commit, reset, or resume.

    A fixer report of repaired is **never** sufficient: consumption requires an
    independent verifier verdict (`pass`) that reviewed the actual diff
    (`diff_reviewed` true, `repair_verified` true) in a session distinct from
    the fixer's. A contradicting or missing verdict blocks the repair, and the
    fixer's own claim is reported as a claim rather than treated as evidence.

    This predicate never checks or waives a task; the implement/review/archive
    task-completeness gates are unaffected by any repair verdict.
    """
    fixer_report = fixer_report if isinstance(fixer_report, Mapping) else None
    verdict = verifier_verdict if isinstance(verifier_verdict, Mapping) else None
    claim = None
    if fixer_report is not None:
        claim = str(
            fixer_report.get("repair")
            or fixer_report.get("status")
            or fixer_report.get("outcome")
            or "repaired"
        )
    if fixer_report is None and verdict is None:
        # No repair context at all: the transition has no repair to consume,
        # so there is nothing for the independence gate to refuse. This is the
        # ordinary path for a transition unrelated to a fixer/verifier cycle.
        return {
            "consumable": True,
            "repair_present": False,
            "fixer_claim": None,
            "fixer_self_certified": False,
            "verifier_verdict": None,
            "reason": None,
        }
    base: dict[str, Any] = {
        "consumable": False,
        "repair_present": True,
        "fixer_claim": claim,
        "fixer_self_certified": bool(
            fixer_report.get("self_certified") if fixer_report else False
        ),
        "verifier_verdict": None,
        "reason": None,
    }
    if verdict is None:
        base["reason"] = (
            "no independent verifier verdict exists; a fixer report never "
            "self-certifies completion"
        )
        return base
    independence = check_verifier_independence(
        fixer_session_id if fixer_session_id is not None else fixer_report.get("session_id") if fixer_report else None,
        verifier_session_id
        if verifier_session_id is not None
        else verdict.get("session_id"),
    )
    outcome = str(verdict.get("verdict") or verdict.get("outcome") or "").strip().lower()
    base["verifier_verdict"] = outcome or None
    if not independence["independent"]:
        base["reason"] = (
            f"the repair is not independently verified: {independence['reason']}"
        )
        return base
    if outcome != "pass":
        base["reason"] = (
            f"the verifier verdict {outcome or '(missing)'!r} does not pass the "
            "repair; the repair is blocked regardless of the fixer's report"
        )
        return base
    if verdict.get("repair_verified") is not True:
        base["reason"] = (
            "the verifier did not confirm the repair against the actual diff"
        )
        return base
    if verdict.get("diff_reviewed") is not True:
        base["reason"] = (
            "the verifier did not review the actual diff; its verdict cannot be "
            "consumed"
        )
        return base
    base["consumable"] = True
    base["reason"] = None
    return base


# Repair consumption is a *transition* gate, not merely a predicate: every
# repair-consuming path invokes :func:`assert_repair_consumable`, which records
# the decision as non-decisive evidence against the owning action and refuses
# non-independent repairs with a recorded ``policy_violation``.
#
# ``commit`` names the git delivery an archive-stage worker produces: the
# archive dispatch is gated (``dispatch``) and its commit is therefore only
# reached through a gated transition. It is listed so the vocabulary is
# explicit rather than implied.
REPAIR_CONSUMING_TRANSITIONS: tuple[str, ...] = (
    "resume",
    "dispatch",
    "commit",
    "reset",
    "release_delegated_gate",
)


def is_repair_consuming_transition(transition: Any) -> bool:
    """True when *transition* names a repair-consuming transition."""
    return isinstance(transition, str) and transition in REPAIR_CONSUMING_TRANSITIONS


def record_repair_evidence(
    ledger: Any,
    action_id: int,
    *,
    fixer_report: Mapping[str, Any] | None,
    verifier_verdict: Mapping[str, Any] | None,
    decision: Mapping[str, Any],
) -> int:
    """Append the repair claim and verdict as non-decisive action evidence."""
    payload = {
        "fixer_report": dict(fixer_report) if isinstance(fixer_report, Mapping) else None,
        "verifier_verdict": (
            dict(verifier_verdict) if isinstance(verifier_verdict, Mapping) else None
        ),
        "consumable": bool(decision.get("consumable")),
        "fixer_claim": decision.get("fixer_claim"),
        "verifier_verdict_outcome": decision.get("verifier_verdict"),
    }
    return int(
        ledger.record_evidence(
            int(action_id),
            kind=EVIDENCE_REPAIR,
            payload=payload,
        )
    )


def assert_repair_consumable(
    ledger: Any,
    job_id: int,
    *,
    transition: str,
    fixer_report: Mapping[str, Any] | None = None,
    verifier_verdict: Mapping[str, Any] | None = None,
    fixer_session_id: Any = None,
    verifier_session_id: Any = None,
    action_id: int | None = None,
    change_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Gate a repair-consuming transition on an independent verifier verdict.

    Every ``resume``/``dispatch``/``reset``/``release_delegated_gate`` that
    consumes a supervised repair calls this. The decision is recorded as
    non-decisive action evidence (when an *action_id* is known) so it is
    durable; a fixer-only report, a same-session verdict, a contradicting
    verdict, or a verdict that never reviewed the real diff records a durable
    ``policy_violation`` and raises :class:`RepairConsumptionError` *before*
    the transition's side effect.

    When the caller does not present the repair context, it is read from the
    journal for *change_id*, so a caller cannot escape the gate by omitting
    its arguments: a recorded fixer report with no independent verdict blocks
    the transition either way.
    """
    if change_id:
        recorded = repair_state_from_ledger(ledger, int(job_id), str(change_id))
        # The journal fills in only what the caller did not present, so a
        # caller-supplied verdict's own session identity is the one compared.
        if fixer_report is None:
            fixer_report = recorded["fixer_report"]
            if fixer_session_id is None:
                fixer_session_id = recorded["fixer_session_id"]
        if verifier_verdict is None:
            verifier_verdict = recorded["verifier_verdict"]
            if verifier_session_id is None:
                verifier_session_id = recorded["verifier_session_id"]
        if action_id is None:
            action_id = recorded["action_id"]
    decision = repair_consumable(
        fixer_report=fixer_report,
        verifier_verdict=verifier_verdict,
        fixer_session_id=fixer_session_id,
        verifier_session_id=verifier_session_id,
    )
    if action_id is not None:
        try:
            record_repair_evidence(
                ledger,
                int(action_id),
                fixer_report=fixer_report,
                verifier_verdict=verifier_verdict,
                decision=decision,
            )
        except Exception:  # noqa: BLE001 - evidence is best-effort durability
            pass
    if decision["consumable"]:
        return decision
    incident_id = record_policy_violation(
        ledger,
        int(job_id),
        role=FIXER_ROLE,
        reason=(
            f"repair consumption via {transition!r} refused: {decision['reason']}"
        ),
        run_id=run_id,
        detail={"transition": transition, "fixer_claim": decision.get("fixer_claim")},
    )
    decision = dict(decision)
    decision["incident_id"] = incident_id
    raise RepairConsumptionError(
        f"repair consumption via {transition!r} refused: {decision['reason']} "
        f"(policy_violation incident {incident_id} recorded against job {job_id})",
        decision=decision,
    )


# Repair reports and verdicts are recorded as non-decisive evidence on the
# action that produced them; the consuming transition reads the latest recorded
# pair for the change so the gate does not depend on caller-supplied context.
REPAIR_FIXER_ROLE_KIND = "fixer"
REPAIR_VERIFIER_ROLE_KIND = VERIFIER_ROLE


def repair_state_from_ledger(
    ledger: Any, job_id: int, change_id: str
) -> dict[str, Any]:
    """Return the latest recorded fixer report and verifier verdict for a change.

    Scans the job's journal for actions whose detail names *change_id*, then
    reads their ``repair_verdict`` evidence. The returned mapping always has
    the documented keys, with ``None`` where nothing was recorded, so a caller
    cannot distinguish "no repair" from "unreadable journal" by omission.
    """
    state: dict[str, Any] = {
        "change_id": change_id,
        "fixer_report": None,
        "verifier_verdict": None,
        "fixer_session_id": None,
        "verifier_session_id": None,
        "action_id": None,
    }
    if not change_id:
        return state
    try:
        actions = ledger.list_actions(int(job_id))
    except Exception:  # noqa: BLE001 - an unreadable journal is absence
        return state
    for row in actions:
        detail = _decode_json_mapping(row["detail"])
        if detail.get("change_id") != change_id:
            continue
        action_id = int(row["id"])
        for evidence in ledger.list_evidence(action_id):
            if evidence["kind"] != EVIDENCE_REPAIR:
                continue
            payload = _decode_json_mapping(evidence["payload"])
            fixer_report = payload.get("fixer_report")
            verdict = payload.get("verifier_verdict")
            if isinstance(fixer_report, Mapping):
                state["fixer_report"] = dict(fixer_report)
                state["action_id"] = action_id
                try:
                    dispatch = ledger.latest_dispatch(action_id)
                except Exception:  # noqa: BLE001 - identity absence
                    dispatch = None
                if dispatch is not None:
                    state["fixer_session_id"] = dispatch["session_id"]
            if isinstance(verdict, Mapping):
                state["verifier_verdict"] = dict(verdict)
                state["verifier_session_id"] = verdict.get("session_id")
    return state


def _decode_json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


# ---------------------------------------------------------------------------
# Pre-prompt transport enforcement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransportDecision:
    """The pre-prompt transport decision for one supervised prompt."""

    enforced: bool
    path: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "enforced": self.enforced,
            "path": self.path,
            "detail": self.detail,
        }


def provider_credentials(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return the sorted provider-credential variable names in *environ*.

    A reusable provider credential (an ``<provider>_API_KEY`` /
    ``_TOKEN`` / ``_SECRET`` shaped variable) must never be present in a worker
    session's environment: credentials stay behind the gateway or enforced
    transport. Detection is deliberately name-based and conservative.
    """
    environment = os.environ if environ is None else environ
    found: list[str] = []
    for key, value in environment.items():
        if not isinstance(key, str) or not key:
            continue
        if value is not None and not isinstance(value, str):
            continue
        if value is not None and not str(value).strip():
            # An unset-but-exported variable is not a usable credential.
            continue
        upper = key.upper()
        suffix = next(
            (candidate for candidate in _CREDENTIAL_SUFFIXES if upper.endswith(candidate)),
            None,
        )
        if suffix is None:
            continue
        prefix = upper[: -len(suffix)].lower()
        if prefix in _PROVIDER_CREDENTIAL_PREFIXES:
            found.append(key)
    return tuple(sorted(found))


def is_loopback_address(address: Any) -> bool:
    """True when *address* names a loopback host (with or without a port)."""
    if not isinstance(address, str) or not address.strip():
        return False
    candidate = address.strip()
    if candidate.startswith("["):
        host = candidate[1:].partition("]")[0]
    elif ":" in candidate:
        host, _, port = candidate.rpartition(":")
        if not port.isdigit():
            return False
    else:
        host = candidate
    host = host.strip().strip("[]")
    if host in _LOOPBACK_HOSTS:
        return True
    if host.startswith("127."):
        parts = host.split(".")
        return len(parts) == 4 and all(part.isdigit() for part in parts)
    return False


def _fenceable_server_identity(value: Any) -> dict[str, Any] | None:
    """Parse a serialized pid/start-time/boot server identity, or ``None``."""
    decoded: Any = value
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(decoded, Mapping):
        return None
    try:
        pid = int(decoded.get("pid"))
        process_start = float(decoded.get("process_start"))
    except (TypeError, ValueError):
        return None
    boot_id = decoded.get("boot_id")
    if pid <= 0 or not isinstance(boot_id, str) or not boot_id:
        return None
    return {"pid": pid, "process_start": process_start, "boot_id": boot_id}


def server_identity_is_live(value: Any) -> bool:
    """True when *value* names a live process on the current boot.

    Liveness is pid + process start time + boot identity, never a bare pid: a
    recycled pid with a different start time, or an identity from another boot,
    is not the launched server.
    """
    identity = _fenceable_server_identity(value)
    if identity is None:
        return False
    current_boot = lock_mod.boot_identity()
    if not current_boot or identity["boot_id"] != current_boot:
        return False
    observed = lock_mod.process_start_time(identity["pid"])
    if observed is None:
        return False
    try:
        return float(observed) == float(identity["process_start"])
    except (TypeError, ValueError):
        return False


def check_server_identity(
    options: Mapping[str, Any] | None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Pure predicate: is the transport target the *launched* service server?

    A loopback hostname is not by itself proof of isolation: any process on the
    host could bind a loopback port. The isolated path therefore requires the
    launched service-owned session server's own identity — its fenceable
    process identity and the address it reported at launch — to be live and to
    match the transport target. A bare loopback address with no launched-server
    identity is not isolated transport and fails closed.

    Config (``server_identity``/``server_address``) wins over the worker
    environment, because the service captured the launched values while the
    environment fields are worker-writable evidence.
    """
    environment = os.environ if environ is None else environ
    options = options if isinstance(options, Mapping) else {}

    target = options.get("target")
    if target is None:
        target = environment.get(SERVER_ADDRESS_ENV)
    target = target.strip() if isinstance(target, str) and target.strip() else None

    expected_address = options.get("server_address")
    if expected_address is None:
        expected_address = environment.get(SERVER_ADDRESS_ENV)
    expected_address = (
        str(expected_address).strip() if expected_address else None
    )

    provided_identity = options.get("server_identity")
    if provided_identity is None:
        provided_identity = environment.get(SERVER_IDENTITY_ENV)
    if isinstance(provided_identity, Mapping):
        provided_identity = json.dumps(
            {key: provided_identity[key] for key in sorted(provided_identity)},
            sort_keys=True,
        )
    provided_identity = (
        str(provided_identity).strip() if provided_identity else None
    )

    violations: list[dict[str, str]] = []
    if not provided_identity:
        violations.append(
            {
                "kind": "missing_server_identity",
                "detail": (
                    "no launched service-server identity accompanies the "
                    "transport target; a loopback address alone does not prove "
                    "the service-owned session server"
                ),
            }
        )
    elif _fenceable_server_identity(provided_identity) is None:
        violations.append(
            {
                "kind": "unfenceable_server_identity",
                "detail": (
                    f"the presented server identity {provided_identity!r} is not "
                    "a fenceable pid/start-time/boot identity"
                ),
            }
        )
    elif not server_identity_is_live(provided_identity):
        violations.append(
            {
                "kind": "dead_server_identity",
                "detail": (
                    f"the presented server identity {provided_identity!r} does "
                    "not name a live process on the current boot; it is not the "
                    "launched service-owned session server"
                ),
            }
        )
    if target is None or not is_loopback_address(target):
        violations.append(
            {
                "kind": "non_loopback_target",
                "detail": (
                    f"the isolated transport target {target!r} is not a loopback "
                    "service-server address"
                ),
            }
        )
    elif expected_address is not None and target != expected_address:
        violations.append(
            {
                "kind": "server_address_mismatch",
                "detail": (
                    f"the transport target {target!r} is not the launched "
                    f"service server's reported address {expected_address!r}"
                ),
            }
        )
    return {
        "allowed": not violations,
        "target": target,
        "server_identity": provided_identity,
        "expected_address": expected_address,
        "violations": violations,
        "reason": violations[0]["detail"] if violations else None,
    }


def evaluate_transport(
    environ: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> TransportDecision:
    """Pure decision: is model traffic enforced for this prompt/spawn?

    Enforced when a trusted model gateway endpoint is configured, or when the
    isolated-transport conditions hold: the transport target is loopback to the
    *launched service-owned session server* (proven by that server's fenceable
    identity and reported address, not a bare loopback hostname) **and** the
    worker environment carries no reusable provider credential. A service-owned
    spawn inside the isolated worker domain has no network egress target of its
    own and is enforced under the same credential rule.

    A worker environment carrying a provider credential is never enforced —
    not even with a gateway configured — because the credential is itself the
    bypass. Otherwise the decision is unenforced and the caller must block with
    the named error.
    """
    environment = os.environ if environ is None else environ
    options = dict(config or {})
    gateway = options.get("gateway_endpoint")
    if not isinstance(gateway, str) or not gateway.strip():
        configured = environment.get(GATEWAY_ENDPOINT_ENV, "")
        gateway = configured if isinstance(configured, str) else ""
    gateway = gateway.strip() if isinstance(gateway, str) else ""

    leaked = provider_credentials(environment)
    if leaked:
        return TransportDecision(
            enforced=False,
            path=TRANSPORT_UNENFORCED,
            detail=(
                "the worker environment carries a reusable provider credential "
                f"({', '.join(leaked)}); credentials must remain behind the "
                "trusted model gateway or an enforced isolated transport"
            ),
        )
    if gateway:
        return TransportDecision(
            enforced=True,
            path=TRANSPORT_GATEWAY,
            detail=f"model traffic is routed through the trusted gateway {gateway!r}",
        )
    explicit_target = options.get("target")
    server = check_server_identity(options, environment)
    if explicit_target is not None:
        # An explicit transport target must be the launched service-owned
        # session server, proven by that server's own fenceable identity and
        # reported address. A bare loopback hostname is not isolation.
        if server["allowed"]:
            return TransportDecision(
                enforced=True,
                path=TRANSPORT_ISOLATED,
                detail=(
                    f"the transport target {server['target']!r} is the launched "
                    "service-owned session server (identity "
                    f"{server['server_identity']!r}) and the worker environment "
                    "carries no provider credential"
                ),
            )
        return TransportDecision(
            enforced=False,
            path=TRANSPORT_UNENFORCED,
            detail=(
                "an explicit transport target was presented but it is not the "
                f"launched service-owned session server ({server['reason']}); "
                "refusing to issue a prompt or spawn that would reach a model "
                "unenforced"
            ),
        )
    if options.get("worker_domain_spawn") is True:
        # A service-owned spawn inside the isolated worker domain has no
        # network egress target of its own; it is enforced under the same
        # credential rule. This is a service-code assertion, not worker input.
        return TransportDecision(
            enforced=True,
            path=TRANSPORT_ISOLATED,
            detail=(
                "the dispatch is a service-owned spawn inside the isolated "
                "worker domain (no transport target of its own) and the worker "
                "environment carries no provider credential"
            ),
        )
    # No explicit target: fall back to the worker-domain environment's
    # launched-server address and identity, both of which must be consistent.
    if server["allowed"] and server["target"] is not None:
        return TransportDecision(
            enforced=True,
            path=TRANSPORT_ISOLATED,
            detail=(
                f"the worker-domain target {server['target']!r} is the launched "
                "service-owned session server (identity "
                f"{server['server_identity']!r}) and the worker environment "
                "carries no provider credential"
            ),
        )
    return TransportDecision(
        enforced=False,
        path=TRANSPORT_UNENFORCED,
        detail=(
            "no trusted model gateway is configured and no enforced isolated "
            "transport is in place ("
            f"{server['reason'] or 'no explicit target or launched-server '
            'identity was presented'}); refusing to issue a prompt or spawn "
            "that would reach a model unenforced"
        ),
    )


def assert_pre_prompt_transport(
    ledger: Any = None,
    action_id: int | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
    record: bool = True,
) -> TransportDecision:
    """Enforce the transport decision before any prompt side effect.

    The decision is recorded as action evidence against *action_id* (before the
    caller writes the dispatch record) so the journal shows enforcement before
    the side effect rather than a usage observation afterward. When the
    decision is unenforced the named :class:`EgressEnforcementError` is raised
    and no prompt is issued; the caller fails the action with the gate reason.
    """
    decision = evaluate_transport(environ, config)
    if ledger is not None and action_id is not None and record:
        ledger.record_evidence(
            int(action_id),
            kind=EVIDENCE_TRANSPORT_DECISION,
            payload=decision.as_dict(),
        )
    if not decision.enforced:
        raise EgressEnforcementError(
            f"pre-prompt transport enforcement failed: {decision.detail}",
            decision=decision,
        )
    return decision


__all__ = [
    "AgentContractError",
    "EVIDENCE_REPAIR",
    "EVIDENCE_TRANSPORT_DECISION",
    "EgressEnforcementError",
    "FIXER_ROLE",
    "GATEWAY_ENDPOINT_ENV",
    "POLICY_VIOLATION_INCIDENT",
    "PRIMARY_SERVICE_TOOL_CAPABILITY",
    "REPAIR_CONSUMING_TRANSITIONS",
    "ROLE_CAPABILITIES",
    "RepairConsumptionError",
    "SERVER_ADDRESS_ENV",
    "SERVER_IDENTITY_ENV",
    "SUPERVISED_ROLE_AGENTS",
    "SessionContractViolation",
    "TRANSPORT_GATEWAY",
    "TRANSPORT_ISOLATED",
    "TRANSPORT_UNENFORCED",
    "TransportDecision",
    "VERIFIER_ROLE",
    "WORKER_AGENT_FIELD",
    "WORKER_IDENTITY_FIELD",
    "WORKER_ROLE_FIELD",
    "assert_pre_prompt_transport",
    "assert_repair_consumable",
    "check_model_pin",
    "check_server_identity",
    "check_session_contract",
    "check_verifier_independence",
    "check_worker_identity",
    "declared_model_pin",
    "enforce_session_contract",
    "evaluate_transport",
    "is_loopback_address",
    "is_repair_consuming_transition",
    "missing_worker_identity_fields",
    "model_identity_string",
    "provider_credentials",
    "record_policy_violation",
    "record_repair_evidence",
    "repair_consumable",
    "repair_state_from_ledger",
    "role_agent",
    "role_capabilities",
    "server_identity_is_live",
]
