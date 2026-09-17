"""Bounded incident recovery: classification, remedies, grants, orchestration.

This module owns the recovery half of durable plan supervision. It is split, on
purpose, into two layers:

- **Pure decisions.** Failure classification, the closed incident-class and
  remedy vocabularies, the class -> bounded-path mapping, the standing-grant
  schema and its evaluation, the delta-identity repair, and the dirty-worktree
  preservation predicates. None of these touch I/O, so "transient vs
  permanent", "known vs unknown class", and "covered vs uncovered effect" are
  testable without a ledger or a dispatch.
- **Orchestration.** The recovery entry point, the fixer/verifier dispatch
  routing, the repair-consumption gate, the bounded retry, and the per-class
  path handlers. These consume a caller-supplied ledger handle and dispatch
  callback, and reuse the existing journaled boundaries
  (:mod:`lib.supervisor.agent_contracts` for the non-self-certifying repair gate
  and :mod:`lib.supervisor.budgets` for the bounded backoff/attempt helpers).

Design rules enforced here:

- Standard library only, and it imports no other runtime package
  (``lib.orchestrator``, ``lib.metrics``, ``lib.pricing``, ``lib.models``).
- Cross-module references go through the module object, never
  ``from lib.supervisor.x import name``, so the package's import graph stays
  acyclic.
- Recovery introduces **no completion, archive, or spec-synchronization
  authority**. The delta-identity repair derives the corrected identity from
  the canonical specification and never rewrites canonical intent; a partial
  archive routes back to the existing review loop without asserting
  completion.
- A root runtime defect is terminal for recovery and produces an operator
  blocker. This module deliberately exposes **no** API that edits the installed
  runtime, reloads the service, or redeploys it: the guarantee is structural,
  not procedural.
- A standing grant is decoded fail-closed: absent, malformed, or
  ``legacy_unversioned`` authorizes no recovery effect, and only an explicit
  operator revision can write one.

Importing this module has no side effects, parses no arguments, spawns no
process, and touches no ``.opsx-plan/`` state.
"""

from __future__ import annotations

import difflib
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from lib.supervisor import acceptance as acceptance_mod
from lib.supervisor import agent_contracts as agent_contracts_mod
from lib.supervisor import budgets as budgets_mod

# ---------------------------------------------------------------------------
# Incident classes: the closed vocabulary
# ---------------------------------------------------------------------------

CLASS_INVALID_RESULT = "invalid_result"
CLASS_TRANSIENT_PROVIDER = "transient_provider"
CLASS_PERMANENT_PROVIDER = "permanent_provider"
CLASS_DELTA_IDENTITY = "delta_identity"
CLASS_DIRTY_WORKTREE = "dirty_worktree"
CLASS_RECURRING_FINDINGS = "recurring_findings"
CLASS_PROCESS_INTERRUPTION = "process_interruption"
CLASS_PARTIAL_ARCHIVE = "partial_archive"
CLASS_RUNTIME_DEFECT = "runtime_defect"
CLASS_UNDETERMINED = "undetermined"

#: The complete, closed incident-class vocabulary. ``undetermined`` is the
#: fail-safe member: a failure that does not classify into any of the known
#: recoverable classes is recorded under it and escalated, never repaired.
INCIDENT_CLASSES: tuple[str, ...] = (
    CLASS_INVALID_RESULT,
    CLASS_TRANSIENT_PROVIDER,
    CLASS_PERMANENT_PROVIDER,
    CLASS_DELTA_IDENTITY,
    CLASS_DIRTY_WORKTREE,
    CLASS_RECURRING_FINDINGS,
    CLASS_PROCESS_INTERRUPTION,
    CLASS_PARTIAL_ARCHIVE,
    CLASS_RUNTIME_DEFECT,
    CLASS_UNDETERMINED,
)

# ---------------------------------------------------------------------------
# Bounded recovery paths: one per class
# ---------------------------------------------------------------------------

PATH_CORRECTIVE_REDISPATCH = "corrective_redispatch"
PATH_BOUNDED_TRANSIENT_RETRY = "bounded_transient_retry"
PATH_ESCALATE = "escalate"
PATH_DELTA_IDENTITY_REPAIR = "delta_identity_repair"
PATH_WORKTREE_PRESERVING_REPAIR = "worktree_preserving_repair"
PATH_RECURRING_FINDINGS_REPAIR = "recurring_findings_repair"
PATH_UNCERTAIN_ACTION_RECONCILIATION = "uncertain_action_reconciliation"
PATH_FRESH_REVIEW = "fresh_review"
PATH_RUNTIME_DEFECT_BLOCKER = "runtime_defect_blocker"

PATHS: tuple[str, ...] = (
    PATH_CORRECTIVE_REDISPATCH,
    PATH_BOUNDED_TRANSIENT_RETRY,
    PATH_ESCALATE,
    PATH_DELTA_IDENTITY_REPAIR,
    PATH_WORKTREE_PRESERVING_REPAIR,
    PATH_RECURRING_FINDINGS_REPAIR,
    PATH_UNCERTAIN_ACTION_RECONCILIATION,
    PATH_FRESH_REVIEW,
    PATH_RUNTIME_DEFECT_BLOCKER,
)

#: Every known class resolves to exactly one bounded path; nothing is left
#: without one.
CLASS_PATHS: dict[str, str] = {
    CLASS_INVALID_RESULT: PATH_CORRECTIVE_REDISPATCH,
    CLASS_TRANSIENT_PROVIDER: PATH_BOUNDED_TRANSIENT_RETRY,
    CLASS_PERMANENT_PROVIDER: PATH_ESCALATE,
    CLASS_DELTA_IDENTITY: PATH_DELTA_IDENTITY_REPAIR,
    CLASS_DIRTY_WORKTREE: PATH_WORKTREE_PRESERVING_REPAIR,
    CLASS_RECURRING_FINDINGS: PATH_RECURRING_FINDINGS_REPAIR,
    CLASS_PROCESS_INTERRUPTION: PATH_UNCERTAIN_ACTION_RECONCILIATION,
    CLASS_PARTIAL_ARCHIVE: PATH_FRESH_REVIEW,
    CLASS_RUNTIME_DEFECT: PATH_RUNTIME_DEFECT_BLOCKER,
    CLASS_UNDETERMINED: PATH_ESCALATE,
}

# ---------------------------------------------------------------------------
# Remedies: the closed vocabulary
# ---------------------------------------------------------------------------

REMEDY_RETRY_TRANSIENT = "retry_transient"
REMEDY_REDISPATCH = "redispatch"
REMEDY_REPAIR_ARTIFACT = "repair_artifact"
REMEDY_REPAIR_DELTA_IDENTITY = "repair_delta_identity"
REMEDY_REPAIR_WORKTREE_PRESERVING = "repair_worktree_preserving"
REMEDY_RECONCILE_UNCERTAIN_ACTION = "reconcile_uncertain_action"
REMEDY_FRESH_REVIEW = "fresh_review"
REMEDY_ESCALATE = "escalate"
REMEDY_REPORT_RUNTIME_DEFECT = "report_runtime_defect"

REMEDIES: tuple[str, ...] = (
    REMEDY_RETRY_TRANSIENT,
    REMEDY_REDISPATCH,
    REMEDY_REPAIR_ARTIFACT,
    REMEDY_REPAIR_DELTA_IDENTITY,
    REMEDY_REPAIR_WORKTREE_PRESERVING,
    REMEDY_RECONCILE_UNCERTAIN_ACTION,
    REMEDY_FRESH_REVIEW,
    REMEDY_ESCALATE,
    REMEDY_REPORT_RUNTIME_DEFECT,
)

#: Per-class permitted remedies. The primary may choose only a member of the
#: incident's class set; anything else is refused and recorded as a policy
#: violation. ``escalate`` is permitted everywhere so an operator hand-off is
#: always a legal choice.
CLASS_REMEDIES: dict[str, tuple[str, ...]] = {
    CLASS_INVALID_RESULT: (REMEDY_REDISPATCH, REMEDY_REPAIR_ARTIFACT, REMEDY_ESCALATE),
    CLASS_TRANSIENT_PROVIDER: (REMEDY_RETRY_TRANSIENT, REMEDY_ESCALATE),
    CLASS_PERMANENT_PROVIDER: (REMEDY_ESCALATE,),
    CLASS_DELTA_IDENTITY: (REMEDY_REPAIR_DELTA_IDENTITY, REMEDY_ESCALATE),
    CLASS_DIRTY_WORKTREE: (REMEDY_REPAIR_WORKTREE_PRESERVING, REMEDY_ESCALATE),
    CLASS_RECURRING_FINDINGS: (REMEDY_REDISPATCH, REMEDY_REPAIR_ARTIFACT, REMEDY_ESCALATE),
    CLASS_PROCESS_INTERRUPTION: (REMEDY_RECONCILE_UNCERTAIN_ACTION, REMEDY_ESCALATE),
    CLASS_PARTIAL_ARCHIVE: (REMEDY_FRESH_REVIEW, REMEDY_ESCALATE),
    CLASS_RUNTIME_DEFECT: (REMEDY_REPORT_RUNTIME_DEFECT,),
    CLASS_UNDETERMINED: (REMEDY_ESCALATE,),
}

#: The remedy a class's bounded path defaults to when the primary does not
#: choose one explicitly. The primary still chooses; this is the recorded
#: default rather than a silent alternative.
CLASS_DEFAULT_REMEDY: dict[str, str] = {
    CLASS_INVALID_RESULT: REMEDY_REDISPATCH,
    CLASS_TRANSIENT_PROVIDER: REMEDY_RETRY_TRANSIENT,
    CLASS_PERMANENT_PROVIDER: REMEDY_ESCALATE,
    CLASS_DELTA_IDENTITY: REMEDY_REPAIR_DELTA_IDENTITY,
    CLASS_DIRTY_WORKTREE: REMEDY_REPAIR_WORKTREE_PRESERVING,
    CLASS_RECURRING_FINDINGS: REMEDY_REDISPATCH,
    CLASS_PROCESS_INTERRUPTION: REMEDY_RECONCILE_UNCERTAIN_ACTION,
    CLASS_PARTIAL_ARCHIVE: REMEDY_FRESH_REVIEW,
    CLASS_RUNTIME_DEFECT: REMEDY_REPORT_RUNTIME_DEFECT,
    CLASS_UNDETERMINED: REMEDY_ESCALATE,
}

#: Durable effects a recovery may consume. These change durable state, so they
#: are the transitions the independent-verification gate and the standing grant
#: both cover.
DURABLE_EFFECTS: tuple[str, ...] = ("commit", "reset", "resume")

#: The non-destructive vocabulary. A remedy that would discard unrelated
#: worktree work (a whole-tree reset/checkout/stash) is outside every class's
#: permitted set by construction, and is refused explicitly for clarity.
DESTRUCTIVE_REMEDY_MARKERS: tuple[str, ...] = (
    "discard",
    "reset_hard",
    "hard_reset",
    "checkout_all",
    "stash",
    "clean_force",
    "wipe",
)

# Action-evidence kind carrying the primary's journaled remedy choice. Non
# decisive, like every other evidence kind: choosing a remedy never completes
# an action.
EVIDENCE_REMEDY_CHOICE = "remedy_choice"

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RecoveryError(Exception):
    """Base class for bounded-recovery failures."""


class RemedyPolicyViolation(RecoveryError):
    """A chosen remedy is outside the incident class's permitted set."""

    def __init__(self, reason: str, *, decision: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.decision = dict(decision or {})


class RecoveryBlocked(RecoveryError):
    """A recovery path escalated or refused instead of performing an effect."""

    def __init__(self, reason: str, *, state: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.state = dict(state or {})


class BoundedRecoveryExceeded(RecoveryError):
    """An identical recovery exhausted its durable per-signature bound."""


class RuntimeDefectError(RecoveryError):
    """A root runtime defect was recorded; no automated repair is possible."""


# ``StandingGrantError``/``StandingGrantVersionError``/``StandingGrantShapeError``
# are the schema errors owned by :mod:`lib.supervisor.budgets`; they are aliased
# in the standing-grant section below so the schema has one implementation.


# ---------------------------------------------------------------------------
# Failure classification (pure)
# ---------------------------------------------------------------------------

# Runtime-defect markers name a defect in the installed runtime/service, whose
# only path is an operator blocker.
_RUNTIME_DEFECT_ERROR_TYPES = frozenset(
    {
        "modulenotfounderror",
        "importerror",
        "schemaerror",
        "schemaversionerror",
        "runtimeerror",
        "assertionerror",
        "notimplementederror",
    }
)
_RUNTIME_DEFECT_MARKERS: tuple[str, ...] = (
    "runtime_defect",
    "installed runtime",
    "installed service",
    "schema version",
    "schema_version",
    "internal invariant",
    "internal error",
    "cannot import",
    "no module named",
)

# Hard-permanent provider markers. They are checked before the transient ones
# so a quota/auth/billing failure is never retried.
_PERMANENT_MESSAGE_MARKERS: tuple[str, ...] = (
    "authentication",
    "unauthorized",
    "forbidden",
    "invalid api key",
    "invalid_api_key",
    "api key",
    "credential",
    "billing",
    "payment",
    "insufficient_quota",
    "insufficient quota",
    "quota exceeded",
    "hard quota",
    "out of quota",
    "permission denied",
    "not authorized",
    "not configured",
    "misconfigured",
    "configuration error",
    "model not found",
    "model_not_found",
)
_TRANSIENT_MESSAGE_MARKERS: tuple[str, ...] = (
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "connection aborted",
    "temporarily unavailable",
    "service unavailable",
    "server error",
    "bad gateway",
    "gateway timeout",
    "rate limit",
    "too many requests",
    "overloaded",
    "try again",
    "econnreset",
    "etimedout",
)

_TRANSIENT_STATUS = frozenset({408, 409, 425, 429, *range(500, 512)})
# Exception-type markers for the transient provider class, matched
# case-insensitively against a normalized ``error_type`` (no spaces).
_TRANSIENT_ERROR_TYPES: tuple[str, ...] = (
    "timeout",
    "timedout",
    "connectionreset",
    "connectionrefused",
    "connectionaborted",
    "connectionerror",
    "servererror",
    "unavailable",
    "temporarilyunavailable",
    "reset",
)
_PERMANENT_STATUS = frozenset({400, 401, 402, 403, 404, 405, 406, 407, 410, 412, 413, 414, 415, 422, 451})

# Class aliases accepted from an explicit evidence ``kind``/``failure_class``.
_CLASS_ALIASES: dict[str, str] = {
    "invalid_result": CLASS_INVALID_RESULT,
    "invalid_output": CLASS_INVALID_RESULT,
    "invalid_json": CLASS_INVALID_RESULT,
    "exhausted_invalid_result": CLASS_INVALID_RESULT,
    "transient": CLASS_TRANSIENT_PROVIDER,
    "transient_provider": CLASS_TRANSIENT_PROVIDER,
    "provider_transient": CLASS_TRANSIENT_PROVIDER,
    "server_error": CLASS_TRANSIENT_PROVIDER,
    "timeout": CLASS_TRANSIENT_PROVIDER,
    "connection_reset": CLASS_TRANSIENT_PROVIDER,
    "permanent": CLASS_PERMANENT_PROVIDER,
    "permanent_provider": CLASS_PERMANENT_PROVIDER,
    "provider_permanent": CLASS_PERMANENT_PROVIDER,
    "auth_error": CLASS_PERMANENT_PROVIDER,
    "billing_error": CLASS_PERMANENT_PROVIDER,
    "quota_error": CLASS_PERMANENT_PROVIDER,
    "config_error": CLASS_PERMANENT_PROVIDER,
    "delta_identity": CLASS_DELTA_IDENTITY,
    "delta_identity_mismatch": CLASS_DELTA_IDENTITY,
    "identity_mismatch": CLASS_DELTA_IDENTITY,
    "modified_identity": CLASS_DELTA_IDENTITY,
    "dirty_worktree": CLASS_DIRTY_WORKTREE,
    "worktree_dirty": CLASS_DIRTY_WORKTREE,
    "recurring_findings": CLASS_RECURRING_FINDINGS,
    "finding_recurrence": CLASS_RECURRING_FINDINGS,
    "finding_recurrence_exceeded": CLASS_RECURRING_FINDINGS,
    "process_interruption": CLASS_PROCESS_INTERRUPTION,
    "uncertain_action": CLASS_PROCESS_INTERRUPTION,
    "interrupted": CLASS_PROCESS_INTERRUPTION,
    "partial_archive": CLASS_PARTIAL_ARCHIVE,
    "archive_partial": CLASS_PARTIAL_ARCHIVE,
    "runtime_defect": CLASS_RUNTIME_DEFECT,
    "installed_runtime_defect": CLASS_RUNTIME_DEFECT,
    "service_defect": CLASS_RUNTIME_DEFECT,
    "undetermined": CLASS_UNDETERMINED,
    "unknown": CLASS_UNDETERMINED,
    "unclassified": CLASS_UNDETERMINED,
}


def normalize_class(value: Any) -> str | None:
    """Return the canonical incident class for an explicit *value*, or ``None``."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower().replace("-", "_").replace(" ", "_")
    if candidate in INCIDENT_CLASSES:
        return candidate
    return _CLASS_ALIASES.get(candidate)


def classify_failure(evidence: Any) -> str:
    """Classify a failure into the closed incident-class vocabulary (pure).

    Classification is evidence-based: an explicit class, a runtime-defect
    marker, a permanent provider marker, a transient provider marker, or a
    provider status code, in that order. A failure whose class cannot be
    determined returns :data:`CLASS_UNDETERMINED`, which is treated as
    permanent by :func:`is_retryable` — the worst case is an escalation, never
    an unbounded retry.
    """
    if not isinstance(evidence, Mapping):
        evidence = {"message": "" if evidence is None else str(evidence)}
    explicit = (
        evidence.get("failure_class")
        or evidence.get("incident_class")
        or evidence.get("kind")
    )
    normalized = normalize_class(explicit)
    if normalized is not None:
        return normalized

    error_type = str(
        evidence.get("error_type") or evidence.get("exception") or ""
    ).strip().lower()
    message = str(
        evidence.get("message")
        or evidence.get("detail")
        or evidence.get("reason")
        or ""
    ).lower()

    if error_type in _RUNTIME_DEFECT_ERROR_TYPES or any(
        marker in message for marker in _RUNTIME_DEFECT_MARKERS
    ):
        return CLASS_RUNTIME_DEFECT

    if any(marker in message for marker in _PERMANENT_MESSAGE_MARKERS):
        return CLASS_PERMANENT_PROVIDER

    status = _status_code(evidence)
    # A permanent status dominates a message-level transient hint.
    if status in _PERMANENT_STATUS:
        return CLASS_PERMANENT_PROVIDER

    if evidence.get("timed_out") is True or evidence.get("connection_reset") is True:
        return CLASS_TRANSIENT_PROVIDER
    if any(marker in error_type for marker in _TRANSIENT_ERROR_TYPES):
        return CLASS_TRANSIENT_PROVIDER
    if any(marker in message for marker in _TRANSIENT_MESSAGE_MARKERS):
        return CLASS_TRANSIENT_PROVIDER
    if status in _TRANSIENT_STATUS:
        return CLASS_TRANSIENT_PROVIDER
    return CLASS_UNDETERMINED


def _status_code(evidence: Mapping[str, Any]) -> int | None:
    for key in ("status_code", "status", "http_status", "provider_status"):
        value = evidence.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def is_retryable(failure_class: Any) -> bool:
    """True only for the transient provider class; everything else escalates."""
    return failure_class == CLASS_TRANSIENT_PROVIDER


def is_recoverable(failure_class: Any) -> bool:
    """True when the class has a bounded repair path rather than escalation."""
    if failure_class not in CLASS_PATHS:
        return False
    return failure_class not in (CLASS_UNDETERMINED, CLASS_RUNTIME_DEFECT)


def classification(evidence: Any) -> dict[str, Any]:
    """Return *evidence*'s class, bounded path, and permitted remedies."""
    failure_class = classify_failure(evidence)
    return {
        "failure_class": failure_class,
        "path": CLASS_PATHS[failure_class],
        "permitted_remedies": list(CLASS_REMEDIES[failure_class]),
        "default_remedy": CLASS_DEFAULT_REMEDY[failure_class],
        "retryable": is_retryable(failure_class),
        "recoverable": is_recoverable(failure_class),
    }


# ---------------------------------------------------------------------------
# Remedy validation (pure)
# ---------------------------------------------------------------------------


def was_destructive_remedy(remedy: Any) -> bool:
    """True when *remedy* names a destructive whole-tree operation."""
    if not isinstance(remedy, str):
        return False
    candidate = remedy.strip().lower().replace("-", "_")
    return any(marker in candidate for marker in DESTRUCTIVE_REMEDY_MARKERS)


def permitted_remedies(failure_class: Any) -> tuple[str, ...]:
    """Return the remedies *failure_class* permits (empty for an unknown class)."""
    return tuple(CLASS_REMEDIES.get(failure_class, ()))


def remedy_decision(failure_class: Any, remedy: Any) -> dict[str, Any]:
    """Pure predicate: may this class's incident choose this remedy?

    ``allowed`` is true only for a member of the class's permitted set. A
    destructive operation is refused even if it were somehow listed, so the
    preservation guarantee does not depend on the vocabulary staying correct.
    """
    permitted = permitted_remedies(failure_class)
    normalized = remedy.strip() if isinstance(remedy, str) else remedy
    if not isinstance(normalized, str) or not normalized:
        return {
            "allowed": False,
            "failure_class": failure_class,
            "remedy": remedy,
            "permitted_remedies": list(permitted),
            "reason": "no remedy was chosen",
        }
    if was_destructive_remedy(normalized):
        return {
            "allowed": False,
            "failure_class": failure_class,
            "remedy": normalized,
            "permitted_remedies": list(permitted),
            "reason": (
                f"remedy {normalized!r} would discard unrelated worktree work; "
                "destructive remedies are outside the permitted set"
            ),
        }
    if normalized not in permitted:
        return {
            "allowed": False,
            "failure_class": failure_class,
            "remedy": normalized,
            "permitted_remedies": list(permitted),
            "reason": (
                f"remedy {normalized!r} is not permitted for incident class "
                f"{failure_class!r}; permitted: "
                + (", ".join(permitted) if permitted else "(none)")
            ),
        }
    return {
        "allowed": True,
        "failure_class": failure_class,
        "remedy": normalized,
        "permitted_remedies": list(permitted),
        "reason": None,
    }


def validate_remedy(failure_class: Any, remedy: Any) -> str:
    """Return *remedy* when the class permits it, else raise a policy violation."""
    decision = remedy_decision(failure_class, remedy)
    if not decision["allowed"]:
        raise RemedyPolicyViolation(str(decision["reason"]), decision=decision)
    return str(decision["remedy"])


# ---------------------------------------------------------------------------
# Standing grants: versioned protected-policy payload (pure decode/evaluate)
# ---------------------------------------------------------------------------

STANDING_GRANT_SCHEMA_VERSION = budgets_mod.STANDING_GRANT_SCHEMA_VERSION
STANDING_GRANT_FIELD = budgets_mod.STANDING_GRANT_FIELD

#: The durable effects a standing grant can authorize. This is the whole
#: vocabulary; an effect outside it is a shape error, not an unknown default.
STANDING_GRANT_EFFECTS: tuple[str, ...] = budgets_mod.STANDING_GRANT_EFFECTS

# The grant's content schema, write validation, read decoding, and durable
# consumption counter live in :mod:`lib.supervisor.budgets`, which owns the
# protected-policy payload schemas and is importable by the ledger without an
# import cycle. Recovery decides how the grant is evaluated and consumed.
validate_standing_grants = budgets_mod.validate_standing_grants
encode_standing_grants = budgets_mod.encode_standing_grants
decode_standing_grants = budgets_mod.decode_standing_grants
standing_grants_value = budgets_mod.standing_grants_value
standing_grant_state = budgets_mod.standing_grant_state
grant_effect_consumption = budgets_mod.grant_effect_consumption
record_grant_effect_consumption = budgets_mod.record_grant_effect_consumption

StandingGrantError = budgets_mod.StandingGrantError
StandingGrantVersionError = budgets_mod.StandingGrantVersionError
StandingGrantShapeError = budgets_mod.StandingGrantShapeError


def evaluate_standing_grant(
    policy: Mapping[str, Any] | None,
    *,
    effect: Any,
    verdict: Mapping[str, Any] | None,
    consumed: int = 0,
) -> dict[str, Any]:
    """Pure decision: may this verified recovery consume *effect*?

    Authorization requires **all** of: a versioned grant is present, the effect
    is covered, the effect's bound has not been reached, and an independent
    verifier verdict consumed the repair. An absent or ``legacy_unversioned``
    grant authorizes nothing; a missing or failing verdict authorizes nothing.
    """
    result: dict[str, Any] = {
        "authorized": False,
        "effect": effect,
        "state": "legacy_unversioned",
        "bound": None,
        "consumed": int(consumed),
        "reason": None,
    }
    if effect not in STANDING_GRANT_EFFECTS:
        raise StandingGrantShapeError(
            f"unknown standing-grant effect {effect!r}; expected one of "
            + ", ".join(STANDING_GRANT_EFFECTS)
        )
    decoded = decode_standing_grants(standing_grants_value(policy))
    result["state"] = decoded["state"]
    if decoded["state"] != "versioned":
        result["reason"] = (
            "no versioned standing grant is recorded for this job; an absent or "
            "legacy grant authorizes no recovery effect"
        )
        return result
    if not (isinstance(verdict, Mapping) and verdict.get("consumable") is True):
        result["reason"] = (
            "a passing independent verifier verdict is required before a "
            "recovery effect consumes a standing grant"
        )
        return result
    grant = decoded["payload"]
    covered = grant["effects"].get(effect)
    if covered is None:
        result["reason"] = (
            f"the standing grant does not cover the {effect!r} effect; it is "
            "escalated for operator action"
        )
        return result
    bound = covered["max"]
    result["bound"] = bound
    if bound is not None and int(consumed) >= int(bound):
        result["reason"] = (
            f"the standing grant's {effect!r} bound ({bound}) is reached; it is "
            "escalated for operator action"
        )
        return result
    result["authorized"] = True
    return result


# ---------------------------------------------------------------------------
# Operator-only grant revision
# ---------------------------------------------------------------------------

OPERATOR_ACTOR = "operator"


def assert_operator_grant_write(actor: Any) -> str:
    """Refuse a grant write from any non-operator authority.

    No worker, model session, fixer, verifier, reset, or automated path may
    create, widen, or bypass a standing grant; only an explicit operator
    revision may. The stored policy is unchanged when this refuses.
    """
    name = actor.strip() if isinstance(actor, str) else ""
    if name != OPERATOR_ACTOR:
        raise StandingGrantShapeError(
            "a standing grant may be written only by an explicit operator "
            f"revision; refusing actor {actor!r}"
        )
    return name


def operator_standing_grant_revision(
    ledger: Any,
    *,
    job_id: int,
    revision: int,
    policy: Mapping[str, Any],
    operator: Any,
    standing_grants: Mapping[str, Any] | None,
) -> int:
    """Record an explicit operator standing-grant revision.

    The grant is written into the protected ``authority_config`` payload under
    one schema version, validated here before the ledger write. Passing
    ``standing_grants=None`` removes the grant, which fails closed: an absent
    grant authorizes no recovery effect. The revision increment rule is the
    ledger's existing explicit one, and the caller must present an operator
    identity.
    """
    actor = assert_operator_grant_write(operator)
    if not isinstance(policy, Mapping):
        raise StandingGrantShapeError("a standing-grant revision requires the current policy")
    new_policy = dict(policy)
    authority = dict(new_policy.get("authority_config") or {})
    if standing_grants is None:
        authority.pop(STANDING_GRANT_FIELD, None)
    else:
        authority[STANDING_GRANT_FIELD] = encode_standing_grants(standing_grants)
    new_policy["authority_config"] = authority
    return int(
        ledger.revise_policy(
            int(job_id), revision=int(revision), policy=new_policy, operator=actor
        )
    )


def consume_recovery_effect(
    ledger: Any,
    *,
    job_id: int,
    incident_id: int,
    change_id: str,
    effect: str,
    policy: Mapping[str, Any],
    verdict: Mapping[str, Any] | None,
    fixer_report: Mapping[str, Any] | None = None,
    fixer_session_id: Any = None,
    verifier_session_id: Any = None,
    action_id: int | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Gate and perform one verified recovery effect (commit/reset/resume).

    The gate order is load-bearing: the independent-verification gate fires
    first, then the standing grant is evaluated, and only then is the
    consumption recorded. The fixer/verifier *dispatches* that produced the
    repair are repair production and are never gated here; the gate applies to
    the durable effect that consumes them.
    """
    decision = agent_contracts_mod.assert_repair_consumable(
        ledger,
        int(job_id),
        transition=str(effect),
        fixer_report=fixer_report,
        verifier_verdict=verdict,
        fixer_session_id=fixer_session_id,
        verifier_session_id=verifier_session_id,
        action_id=action_id,
        change_id=change_id,
        run_id=run_id,
    )
    consumed = grant_effect_consumption(ledger, job_id, str(effect))
    grant = evaluate_standing_grant(
        policy, effect=str(effect), verdict=decision, consumed=consumed
    )
    if not grant["authorized"]:
        state = escalate_incident(
            ledger,
            int(incident_id),
            reason=str(grant["reason"]),
            operator_action=(
                f"review the {effect!r} recovery effect and revise the standing "
                "grant if it should be authorized"
            ),
        )
        raise RecoveryBlocked(str(grant["reason"]), state=state)
    record_grant_effect_consumption(ledger, job_id, str(effect))
    return {
        "authorized": True,
        "effect": str(effect),
        "grant": grant,
        "verdict": decision,
        "consumed": consumed + 1,
    }


# ---------------------------------------------------------------------------
# Incident lifecycle orchestration
# ---------------------------------------------------------------------------


def find_open_incident(ledger: Any, job_id: int, signature: str) -> Any:
    """Return the job's non-terminal incident for *signature*, or ``None``."""
    for row in ledger.list_incidents(int(job_id)):
        if str(row["signature"] or "") != str(signature):
            continue
        if str(row["state"]) not in ("resolved", "escalated"):
            return row
    return None


def open_incident(
    ledger: Any,
    *,
    job_id: int,
    failure_class: str,
    signature: str,
    summary: str = "",
    run_id: str | None = None,
) -> int:
    """Open a durable incident for *failure_class* under *signature*."""
    existing = find_open_incident(ledger, job_id, signature)
    if existing is not None:
        return int(existing["id"])
    return int(
        ledger.record_incident(
            int(job_id),
            kind=str(failure_class),
            signature=str(signature),
            state="open",
            summary=summary,
            run_id=run_id,
        )
    )


def escalate_incident(
    ledger: Any,
    incident_id: int,
    *,
    reason: str,
    operator_action: str,
) -> dict[str, Any]:
    """Escalate an incident to its terminal state and return its blocker."""
    blocker = budgets_mod.blocker_state(reason, operator_action=operator_action)
    try:
        row = ledger.transition_incident(
            int(incident_id), target_state="escalated", summary=reason
        )
        state = str(row["state"])
    except Exception:  # noqa: BLE001 - a terminal/unknown incident is not re-raised
        try:
            state = str(ledger.get_incident(int(incident_id))["state"])
        except Exception:  # noqa: BLE001 - absent incident is durable-state absence
            state = "unknown"
    return {
        "incident_id": int(incident_id),
        "state": state,
        "escalated": state == "escalated",
        "blocker": blocker,
        "reason": reason,
    }


def resolve_incident(ledger: Any, incident_id: int, *, summary: str) -> dict[str, Any]:
    """Resolve an incident through ``recovering`` to its terminal state."""
    row = ledger.get_incident(int(incident_id))
    if str(row["state"]) == "open":
        ledger.transition_incident(
            int(incident_id), target_state="recovering", summary=summary
        )
    if str(ledger.get_incident(int(incident_id))["state"]) == "recovering":
        ledger.transition_incident(
            int(incident_id), target_state="resolved", summary=summary
        )
    return dict(ledger.get_incident(int(incident_id)))


def record_remedy_choice(
    ledger: Any,
    job_id: int,
    *,
    incident_id: int,
    failure_class: str,
    remedy: str,
    action_id: int | None = None,
    run_id: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Journal the primary's remedy choice before its repair's side effect.

    The choice is recorded as non-decisive action evidence when the owning
    action is known; either way it is durable and names the incident.
    """
    payload = {
        "incident_id": int(incident_id),
        "failure_class": str(failure_class),
        "remedy": str(remedy),
        "path": CLASS_PATHS.get(str(failure_class)),
        "detail": dict(detail or {}),
    }
    evidence_id = None
    if action_id is not None:
        evidence_id = int(
            ledger.record_evidence(
                int(action_id), kind=EVIDENCE_REMEDY_CHOICE, payload=payload
            )
        )
    return {"journaled": evidence_id is not None, "evidence_id": evidence_id, **payload}


def begin_recovery(
    ledger: Any,
    *,
    job_id: int,
    change_id: str,
    stage: str,
    failure: Any,
    policy: Mapping[str, Any],
    remedy: Any = None,
    action_id: int | None = None,
    run_id: str | None = None,
    discriminator: str = "",
) -> dict[str, Any]:
    """Recovery entry point: classify, open an incident, and journal a remedy.

    The incident is linked to the stable attempt signature of the failure
    class, material identity, and stage. The primary-chosen remedy is validated
    against the class and journaled *before* any repair side effect; an
    out-of-set or destructive choice records a durable ``policy_violation`` and
    escalates. A runtime defect is terminal and becomes an operator blocker.
    """
    evidence = failure if isinstance(failure, Mapping) else {"message": str(failure)}
    failure_class = classify_failure(evidence)
    path = CLASS_PATHS[failure_class]
    signature = budgets_mod.incident_signature(
        kind=failure_class,
        change_id=str(change_id),
        stage=str(stage),
        discriminator=str(discriminator or failure_class),
    )
    summary = str(evidence.get("message") or evidence.get("detail") or failure_class)
    incident_id = open_incident(
        ledger,
        job_id=int(job_id),
        failure_class=failure_class,
        signature=signature,
        summary=summary,
        run_id=run_id,
    )
    chosen = remedy if remedy is not None else CLASS_DEFAULT_REMEDY[failure_class]
    decision = remedy_decision(failure_class, chosen)
    if not decision["allowed"]:
        violation_id = agent_contracts_mod.record_policy_violation(
            ledger,
            int(job_id),
            reason=f"remedy refused: {decision['reason']}",
            run_id=run_id,
            detail={
                "incident_id": int(incident_id),
                "failure_class": failure_class,
                "remedy": str(chosen),
                "permitted": list(decision["permitted_remedies"]),
            },
        )
        state = escalate_incident(
            ledger,
            incident_id,
            reason=str(decision["reason"]),
            operator_action=(
                "choose a remedy the incident class permits, or intervene directly"
            ),
        )
        state["policy_violation_incident"] = violation_id
        raise RemedyPolicyViolation(
            f"{decision['reason']} (policy_violation incident {violation_id} "
            f"recorded against job {job_id})",
            decision=state,
        )
    chosen_remedy = str(decision["remedy"])
    journal = record_remedy_choice(
        ledger,
        int(job_id),
        incident_id=incident_id,
        failure_class=failure_class,
        remedy=chosen_remedy,
        action_id=action_id,
        run_id=run_id,
        detail={"stage": stage, "change_id": change_id, "path": path},
    )
    if failure_class == CLASS_RUNTIME_DEFECT:
        return runtime_defect_blocker(
            ledger,
            job_id=int(job_id),
            incident_id=incident_id,
            reason=summary,
            remedy=chosen_remedy,
            journal=journal,
        )
    if path == PATH_ESCALATE:
        state = escalate_incident(
            ledger,
            incident_id,
            reason=str(decision["reason"])
            or f"{failure_class} has no automated repair path",
            operator_action="triage the failure and choose an operator action",
        )
        return {
            "status": "escalated",
            "failure_class": failure_class,
            "path": path,
            "remedy": chosen_remedy,
            "signature": signature,
            "blocker": state["blocker"],
            "journal": journal,
        }
    row = ledger.get_incident(incident_id)
    if str(row["state"]) == "open":
        row = ledger.transition_incident(
            int(incident_id), target_state="recovering", summary=summary
        )
    return {
        "status": "recovering",
        "failure_class": failure_class,
        "path": path,
        "remedy": chosen_remedy,
        "signature": signature,
        "incident_id": int(incident_id),
        "state": str(row["state"]),
        "permitted_remedies": list(decision["permitted_remedies"]),
        "journal": journal,
    }


def bounded_recovery_attempt(
    ledger: Any,
    *,
    job_id: int,
    signature: str,
    policy: Mapping[str, Any],
) -> int:
    """Consume one recovery attempt under the durable per-signature bound.

    The bound is the job policy's ``max_incident_attempts``; the counter lives
    in the external ledger, so it survives ``opsx-plan reset`` and is never
    re-baselined. Exhaustion raises :class:`BoundedRecoveryExceeded`.
    """
    try:
        return budgets_mod.record_incident_attempt(
            ledger, job_id=int(job_id), signature=str(signature), policy=policy
        )
    except budgets_mod.BoundedAttemptsExceededError as exc:
        raise BoundedRecoveryExceeded(str(exc)) from exc


def _record_retry(
    ledger: Any, job_id: int, signature: str, attempt: int, delay: float, exc: BaseException
) -> None:
    """Durably record one transient retry under its signature."""
    try:
        ledger.record_incident_attempt(int(job_id), signature=str(signature))
    except Exception:  # noqa: BLE001 - the retry record is best-effort durable
        pass


def bounded_transient_retry(
    ledger: Any,
    *,
    job_id: int,
    signature: str,
    operation: Callable[[], Any],
    sleep: Callable[[float], None] | None = None,
    classify: Callable[[Any], str] = classify_failure,
    max_attempts: int = budgets_mod.BACKOFF_MAX_ATTEMPTS,
) -> Any:
    """Run *operation* under bounded backoff, retrying only transient failures.

    A failure classifies from its ``evidence`` attribute (or its exception
    type/message) and is retried only when :func:`classify_failure` returns the
    transient provider class. Every retry is recorded durably. A permanent or
    undetermined failure is re-raised immediately for escalation, and
    exhausting the bound re-raises the last error rather than looping.
    """
    def _should_retry(exc: BaseException) -> bool:
        evidence = getattr(exc, "evidence", None)
        if not isinstance(evidence, Mapping):
            evidence = {"error_type": type(exc).__name__, "message": str(exc)}
        return classify(evidence) == CLASS_TRANSIENT_PROVIDER

    kwargs: dict[str, Any] = {
        "should_retry": _should_retry,
        "on_retry": lambda attempt, delay, exc: _record_retry(
            ledger, int(job_id), str(signature), attempt, delay, exc
        ),
        "max_attempts": int(max_attempts),
    }
    if sleep is not None:
        kwargs["sleep"] = sleep
    return budgets_mod.run_with_bounded_backoff(operation, **kwargs)


def dispatch_repair(
    ledger: Any,
    *,
    job_id: int,
    change_id: str,
    stage: str,
    incident_id: int,
    dispatch: Callable[..., Mapping[str, Any]],
    run_id: str | None = None,
    action_id: int | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch the cheap fixer, then an independent verifier, for an incident.

    These dispatches are repair *production*: they are journaled through the
    caller-supplied dispatch boundary but deliberately not gated on a verdict
    that does not exist yet. Consumption of the resulting repair is gated
    separately by :func:`consume_recovery_effect`.
    """
    fixer = dispatch(
        "fixer",
        {
            "change_id": change_id,
            "stage": stage,
            "incident_id": int(incident_id),
            "action": "repair",
            "context": dict(context or {}),
        },
    )
    fixer = dict(fixer or {})
    verifier = dispatch(
        "verifier",
        {
            "change_id": change_id,
            "stage": stage,
            "incident_id": int(incident_id),
            "action": "verify",
            "fixer": fixer,
            "context": dict(context or {}),
        },
    )
    verifier = dict(verifier or {})
    return {
        "stage": str(stage),
        "incident_id": int(incident_id),
        "fixer_report": fixer,
        "verifier_verdict": verifier,
        "fixer_session_id": fixer.get("session_id"),
        "verifier_session_id": verifier.get("session_id"),
    }


# ---------------------------------------------------------------------------
# Per-class bounded paths
# ---------------------------------------------------------------------------


def runtime_defect_blocker(
    ledger: Any,
    *,
    job_id: int,
    incident_id: int,
    reason: str,
    remedy: str = REMEDY_REPORT_RUNTIME_DEFECT,
    journal: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a root runtime defect as an operator blocker and stop.

    This module has no API that edits the installed runtime or service, reloads
    it, or redeploys it, so the no-self-repair guarantee holds structurally.
    """
    state = escalate_incident(
        ledger,
        int(incident_id),
        reason=f"root runtime defect: {reason}",
        operator_action=(
            "repair the installed runtime in the repository and redeploy it; "
            "recovery never self-edits or self-deploys the service"
        ),
    )
    state.update(
        {
            "status": "blocked",
            "failure_class": CLASS_RUNTIME_DEFECT,
            "path": PATH_RUNTIME_DEFECT_BLOCKER,
            "remedy": remedy,
            "self_repaired": False,
            "journal": dict(journal or {}),
        }
    )
    return state


_REQUIREMENT_RE = re.compile(r"^###\s+Requirement:\s*(.+?)\s*$")
_OPERATION_RE = re.compile(r"^##\s+(ADDED|MODIFIED|REMOVED|RENAMED)\b")


def _canonical_requirement_names(text: Any) -> tuple[str, ...]:
    names: list[str] = []
    for line in str(text or "").splitlines():
        match = _REQUIREMENT_RE.match(line.strip())
        if match:
            name = match.group(1).strip()
            if name and name not in names:
                names.append(name)
    return tuple(names)


def _requirement_sections(text: Any) -> list[tuple[str, str]]:
    """Split a spec into ``(requirement name, body)`` sections."""
    sections: list[tuple[str, str]] = []
    name: str | None = None
    body: list[str] = []
    for line in str(text or "").splitlines():
        match = _REQUIREMENT_RE.match(line.strip())
        if match:
            if name is not None:
                sections.append((name, "\n".join(body).strip()))
            name = match.group(1).strip()
            body = []
            continue
        if name is not None:
            body.append(line)
    if name is not None:
        sections.append((name, "\n".join(body).strip()))
    return sections


def _canonical_section_bodies(canonical_text: Any) -> list[tuple[str, str]]:
    return _requirement_sections(canonical_text)


def _best_canonical_match(body: str, canonical: Sequence[tuple[str, str]]) -> str | None:
    """Return the canonical requirement whose body best matches *body*."""
    best_name: str | None = None
    best_ratio = 0.0
    for name, canonical_body in canonical:
        ratio = difflib.SequenceMatcher(None, body, canonical_body).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_name = name
    if best_name is None:
        return None
    # A single canonical requirement is unambiguous; otherwise require a real
    # content match so an unrelated requirement is never renamed by a guess.
    if len(canonical) == 1 or best_ratio >= 0.6:
        return best_name
    return None


def repair_delta_modified_identity(
    delta_text: Any,
    canonical_text: Any,
    *,
    intended: str | None = None,
) -> dict[str, Any]:
    """Derive a delta ``MODIFIED`` requirement's identity from the canonical spec.

    The canonical specification stays the authority for the requirement's
    meaning: only the delta's identity heading is corrected, matched to the
    canonical requirement by content, and the canonical text is never rewritten.
    """
    canonical_names = _canonical_requirement_names(canonical_text)
    canonical = _canonical_section_bodies(canonical_text)
    delta_operations = acceptance_mod.parse_spec_delta_identity(str(delta_text or ""))
    modified = [
        identity["requirement"]
        for identity in delta_operations
        if identity.get("operation") == "MODIFIED"
    ]
    original = modified[0] if modified else ""
    corrected: str | None = None
    if isinstance(intended, str) and intended.strip() and intended.strip() in canonical_names:
        corrected = intended.strip()
    else:
        for name, body in _requirement_sections(str(delta_text or "")):
            if name == original or original == "":
                corrected = _best_canonical_match(body, canonical)
                break
    lines = str(delta_text or "").splitlines()
    repaired_lines: list[str] = []
    in_modified = False
    changed = False
    for line in lines:
        match = _OPERATION_RE.match(line.strip())
        if match:
            in_modified = match.group(1) == "MODIFIED"
            repaired_lines.append(line)
            continue
        requirement_match = _REQUIREMENT_RE.match(line.strip())
        if requirement_match and in_modified and corrected and not changed:
            repaired_lines.append(f"### Requirement: {corrected}")
            changed = True
            continue
        repaired_lines.append(line)
    return {
        "delta": "\n".join(repaired_lines) + ("\n" if str(delta_text or "").endswith("\n") else ""),
        "original_identity": original,
        "corrected_identity": corrected if changed else original,
        "canonical_names": list(canonical_names),
        "canonical_intent_preserved": True,
        "canonical_spec_unchanged": True,
        "changed": changed and corrected != original,
    }


def capture_worktree_state(repo: Path | str) -> dict[str, Any]:
    """Capture the worktree's tracked, staged, and untracked state.

    Uses ``git status --porcelain=v1 -z`` so unrelated user work is recorded
    exactly before a repair touches anything. A non-git directory yields an
    empty state rather than raising.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain=v1", "-z"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "tracked": {}, "staged": {}, "untracked": []}
    if completed.returncode != 0:
        return {"available": False, "tracked": {}, "staged": {}, "untracked": []}
    entries = [entry for entry in completed.stdout.split("\0") if entry]
    tracked: dict[str, str] = {}
    staged: dict[str, str] = {}
    untracked: list[str] = []
    for entry in entries:
        status = entry[:2]
        path = entry[3:] if len(entry) > 3 else ""
        if not path:
            continue
        if status == "??":
            untracked.append(path)
            continue
        if status[0] != " " and status[0] != "?":
            staged[path] = status[0]
        tracked[path] = status[1]
    return {
        "available": True,
        "tracked": tracked,
        "staged": staged,
        "untracked": sorted(untracked),
    }


def verify_worktree_preserved(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    *,
    authorized_paths: Iterable[str] = (),
) -> dict[str, Any]:
    """Verify unrelated tracked, staged, and untracked work is intact (pure).

    Every path recorded in *before* that is not in the authorized repair set
    must appear unchanged in *after*. Anything missing or altered is reported,
    never repaired away.
    """
    authorized = {str(path) for path in authorized_paths}
    before = before if isinstance(before, Mapping) else {}
    after = after if isinstance(after, Mapping) else {}
    violations: list[dict[str, str]] = []
    for bucket in ("tracked", "staged"):
        for path, status in dict(before.get(bucket) or {}).items():
            if path in authorized:
                continue
            current = dict(after.get(bucket) or {}).get(path)
            if current != status:
                violations.append(
                    {
                        "kind": f"unrelated_{bucket}_changed",
                        "path": str(path),
                        "before": str(status),
                        "after": str(current),
                    }
                )
    for path in list(before.get("untracked") or []):
        if path in authorized:
            continue
        if path not in set(after.get("untracked") or []):
            violations.append(
                {
                    "kind": "unrelated_untracked_changed",
                    "path": str(path),
                    "before": "untracked",
                    "after": "absent",
                }
            )
    return {
        "preserved": not violations,
        "authorized_paths": sorted(authorized),
        "violations": violations,
        "reason": violations[0]["kind"] if violations else None,
    }


def partial_archive_recovery(
    *,
    change_id: str,
    archive_complete: bool,
    fast_check_ok: bool,
    reason: str = "",
) -> dict[str, Any]:
    """Route a partial archive / failed fast check to a fresh review.

    Completion is never asserted here: the change is re-entered through the
    existing review loop, bounded by its existing round budget.
    """
    complete = bool(archive_complete) and bool(fast_check_ok)
    return {
        "status": "rework_required" if not complete else "no_rework",
        "failure_class": CLASS_PARTIAL_ARCHIVE,
        "path": PATH_FRESH_REVIEW,
        "change_id": str(change_id),
        "fresh_review_required": not complete,
        "completion_asserted": False,
        "archive_treated_as_done": False,
        "reason": reason
        or (
            "archive did not complete"
            if not archive_complete
            else "post-archive fast check failed"
        ),
    }


def reconcile_process_interruption(
    *,
    evidence: Mapping[str, Any] | None,
    action_id: int | None = None,
) -> dict[str, Any]:
    """Decide whether an uncertain action can be reconciled from evidence.

    Reconciliation requires conclusive evidence; without it the path escalates
    rather than resuming on an assumption.
    """
    evidence = evidence if isinstance(evidence, Mapping) else {}
    conclusion = evidence.get("conclusion") or evidence.get("state")
    conclusive = conclusion in ("completed", "failed", "exited", "done")
    return {
        "reconciled": conclusive,
        "conclusion": conclusion if conclusive else None,
        "action_id": action_id,
        "path": PATH_UNCERTAIN_ACTION_RECONCILIATION,
        "failure_class": CLASS_PROCESS_INTERRUPTION,
        "escalate": not conclusive,
        "reason": None if conclusive else (
            "the interrupted action's outcome cannot be reconciled from evidence; "
            "escalating rather than resuming on an assumption"
        ),
    }


def recovery_plan(failure_class: Any) -> dict[str, Any]:
    """Return the bounded path and permitted remedies for *failure_class*."""
    if failure_class not in CLASS_PATHS:
        raise RecoveryError(f"unknown incident class: {failure_class!r}")
    return {
        "failure_class": failure_class,
        "path": CLASS_PATHS[failure_class],
        "permitted_remedies": list(CLASS_REMEDIES[failure_class]),
        "default_remedy": CLASS_DEFAULT_REMEDY[failure_class],
        "recoverable": is_recoverable(failure_class),
    }


def self_repair_surface() -> tuple[str, ...]:
    """Return the module's self-repair API names.

    It is always empty: recovery exposes no function that edits the installed
    runtime or service, reloads it, or deploys it.
    """
    return ()


__all__ = [
    "CLASS_INVALID_RESULT",
    "CLASS_TRANSIENT_PROVIDER",
    "CLASS_PERMANENT_PROVIDER",
    "CLASS_DELTA_IDENTITY",
    "CLASS_DIRTY_WORKTREE",
    "CLASS_RECURRING_FINDINGS",
    "CLASS_PROCESS_INTERRUPTION",
    "CLASS_PARTIAL_ARCHIVE",
    "CLASS_RUNTIME_DEFECT",
    "CLASS_UNDETERMINED",
    "INCIDENT_CLASSES",
    "CLASS_PATHS",
    "PATHS",
    "PATH_CORRECTIVE_REDISPATCH",
    "PATH_BOUNDED_TRANSIENT_RETRY",
    "PATH_ESCALATE",
    "PATH_DELTA_IDENTITY_REPAIR",
    "PATH_WORKTREE_PRESERVING_REPAIR",
    "PATH_RECURRING_FINDINGS_REPAIR",
    "PATH_UNCERTAIN_ACTION_RECONCILIATION",
    "PATH_FRESH_REVIEW",
    "PATH_RUNTIME_DEFECT_BLOCKER",
    "REMEDY_RETRY_TRANSIENT",
    "REMEDY_REDISPATCH",
    "REMEDY_REPAIR_ARTIFACT",
    "REMEDY_REPAIR_DELTA_IDENTITY",
    "REMEDY_REPAIR_WORKTREE_PRESERVING",
    "REMEDY_RECONCILE_UNCERTAIN_ACTION",
    "REMEDY_FRESH_REVIEW",
    "REMEDY_ESCALATE",
    "REMEDY_REPORT_RUNTIME_DEFECT",
    "REMEDIES",
    "CLASS_REMEDIES",
    "CLASS_DEFAULT_REMEDY",
    "DURABLE_EFFECTS",
    "DESTRUCTIVE_REMEDY_MARKERS",
    "EVIDENCE_REMEDY_CHOICE",
    "STANDING_GRANT_SCHEMA_VERSION",
    "STANDING_GRANT_FIELD",
    "STANDING_GRANT_EFFECTS",
    "OPERATOR_ACTOR",
    "RecoveryError",
    "RemedyPolicyViolation",
    "RecoveryBlocked",
    "BoundedRecoveryExceeded",
    "RuntimeDefectError",
    "StandingGrantError",
    "StandingGrantVersionError",
    "StandingGrantShapeError",
    "normalize_class",
    "classify_failure",
    "is_retryable",
    "is_recoverable",
    "classification",
    "was_destructive_remedy",
    "permitted_remedies",
    "remedy_decision",
    "validate_remedy",
    "validate_standing_grants",
    "encode_standing_grants",
    "decode_standing_grants",
    "standing_grants_value",
    "standing_grant_state",
    "evaluate_standing_grant",
    "assert_operator_grant_write",
    "operator_standing_grant_revision",
    "grant_effect_consumption",
    "record_grant_effect_consumption",
    "consume_recovery_effect",
    "find_open_incident",
    "open_incident",
    "escalate_incident",
    "resolve_incident",
    "record_remedy_choice",
    "begin_recovery",
    "bounded_recovery_attempt",
    "bounded_transient_retry",
    "dispatch_repair",
    "runtime_defect_blocker",
    "repair_delta_modified_identity",
    "capture_worktree_state",
    "verify_worktree_preserved",
    "partial_archive_recovery",
    "reconcile_process_interruption",
    "recovery_plan",
    "self_repair_surface",
]
