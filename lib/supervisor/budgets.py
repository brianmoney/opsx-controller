"""Versioned supervision budgets, reservations, and pure budget decisions.

This module owns the content schema of the protected job policy's ``budgets``
and ``deadlines`` payloads, the reservation/reconciliation vocabulary, the
incident-attempt signature bound, execution-deadline accounting, and the pure
fail-closed decisions the supervised budget gates apply.

Design rules enforced here:

- Standard library only. It imports **no** other runtime package
  (``lib.orchestrator``, ``lib.metrics``, ``lib.pricing``, ``lib.models``) and
  references its sibling :mod:`lib.supervisor.model_policy` through the module
  object so the package dependency graph stays acyclic.
- Pure: no I/O, no clock, and no database access. The reservation engine takes
  a concrete ledger handle that is given to it; pricing is computed at the
  orchestrator boundary and passed in as plain data. This mirrors how
  ``model_policy.py`` stays pure while the ledger routes through it.
- Forward-only versioning: a recorded nested payload version newer than
  :data:`BUDGET_SCHEMA_VERSION` raises :class:`BudgetVersionError` rather than
  being silently interpreted. This constant is independent of the outer ledger
  ``policy_version`` column and of ``MODEL_POLICY_VERSION``.
- Pre-schema (unversioned) payloads read as ``legacy_unversioned``: a JSON
  value with no ``version`` key is returned unmodified and is never
  interpreted as a current payload. A consumer treats it as carrying no
  enforceable limits and fails closed for supervised dispatch rather than
  defaulting to unlimited spend.

The v1 limits deliberately bound overshoot rather than imposing an exact
real-time ceiling: a dispatch is estimated from the pricing catalog with the
provider token-cap envelope and a stated headroom margin before it runs, so a
hard limit's expected overshoot is bounded by at most one in-flight action's
headroom.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from lib.supervisor import model_policy

# The single nested-payload version constant for both the budgets and
# deadlines payloads. Independent of the outer ledger ``policy_version`` and
# of ``MODEL_POLICY_VERSION``.
BUDGET_SCHEMA_VERSION = 1

# The versioned ``standing_grants`` payload of the protected job policy. It is
# an operator-established standing permission naming the recovery effects a job
# may perform unattended and their bounds. It shares the forward-only version
# discipline of the budget payloads: absent, unversioned, malformed, or
# newer-than-supported is never interpreted as a current grant.
STANDING_GRANT_SCHEMA_VERSION = 1

#: The policy field name carrying the grant, and the complete closed vocabulary
#: of durable effects it can authorize.
STANDING_GRANT_FIELD = "standing_grants"
STANDING_GRANT_EFFECTS: tuple[str, ...] = ("commit", "reset", "resume")

# v1 conservative defaults. A reservation estimate is
# ``rate(pinned model) x DEFAULT_TOKEN_CAP_ENVELOPE x HEADROOM_MULTIPLIER``,
# where the envelope is the per-call token cap used when the provider exposes
# no model-specific cap. Both are recorded per-reservation via the reserved
# amounts and the pricing-catalog version, so every estimate is auditable.
DEFAULT_TOKEN_CAP_ENVELOPE = 200_000
HEADROOM_MULTIPLIER = 1.5

# Bounded transient-failure backoff: base delay, cap, and maximum attempts.
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 8.0
BACKOFF_MAX_ATTEMPTS = 3

# The cost/elapsed limits a ``budgets`` payload may carry. A null value
# disables that limit; at least one SHALL be non-null.
BUDGET_LIMIT_FIELDS: tuple[str, ...] = (
    "total_cost_usd",
    "per_action_cost_usd",
    "total_elapsed_minutes",
    "per_action_elapsed_minutes",
)
MAX_INCIDENT_ATTEMPTS_FIELD = "max_incident_attempts"
BUDGET_FIELDS: tuple[str, ...] = BUDGET_LIMIT_FIELDS + (MAX_INCIDENT_ATTEMPTS_FIELD,)

# ``deadlines`` carries execution-time limits only. Human-wait duration is
# excluded by construction and never expressed as a wait budget here.
DEADLINE_LIMIT_FIELDS: tuple[str, ...] = ("execution_deadline_minutes",)
DEADLINE_FIELDS: tuple[str, ...] = DEADLINE_LIMIT_FIELDS

# Mirror the dispatch-identity vocabulary owned by ``model_policy`` exactly, so
# whichever change lands second reconciles to the shared definitions.
OBSERVATION_STATES: tuple[str, ...] = model_policy.OBSERVATION_STATES
RESERVATION_STATES: tuple[str, ...] = model_policy.RESERVATION_STATES

# The observation states whose consumption is never released back to the job.
RETAINING_OBSERVATION_STATES: tuple[str, ...] = ("unknown", "interrupted")


class BudgetError(Exception):
    """Base class for supervision-budget failures."""


class BudgetVersionError(BudgetError):
    """A recorded budget/deadline version is newer than this code supports."""


class BudgetShapeError(BudgetError):
    """A budget/deadline payload is malformed or has an unsupported shape."""


class UnknownPricingError(BudgetError):
    """A dispatch's pinned model cannot be priced by the catalog."""


class BudgetExhaustedError(BudgetError):
    """A reservation would exceed a per-action or total budget limit."""


class BoundedAttemptsExceededError(BudgetError):
    """An identical incident attempt reached the policy's bounded limit."""


class StandingGrantError(BudgetError):
    """Base class for standing-grant schema failures."""


class StandingGrantVersionError(StandingGrantError):
    """A recorded standing-grant version is newer than this code supports."""


class StandingGrantShapeError(StandingGrantError):
    """A standing-grant payload is malformed or has an unsupported shape."""


def _check_required_keys(payload: Mapping[str, Any], required: Iterable[str], field: str) -> None:
    """Require every declared key to be physically present before normalization.

    The spec defines the payload *shape*: an omitted declared key is not the
    same as an explicit null, so a partial object is rejected rather than
    silently normalized with nulls.
    """
    missing = sorted(set(required) - set(payload))
    if missing:
        raise BudgetShapeError(
            f"{field} payload is missing declared key(s): {', '.join(missing)}"
        )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_non_negative_number(value: Any) -> bool:
    return _is_number(value) and value >= 0


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_version(value: Mapping[str, Any], field: str) -> int:
    if "version" not in value:
        raise BudgetShapeError(f"{field} payload is missing its 'version' key")
    version = value["version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise BudgetShapeError(
            f"{field} payload 'version' must be an integer, got {version!r}"
        )
    if version > BUDGET_SCHEMA_VERSION:
        raise BudgetVersionError(
            f"{field} version {version} is newer than supported version "
            f"{BUDGET_SCHEMA_VERSION}; reinstall the matching runtime"
        )
    if version < BUDGET_SCHEMA_VERSION:
        raise BudgetShapeError(
            f"{field} version {version} is older than supported version "
            f"{BUDGET_SCHEMA_VERSION}"
        )
    return version


def _check_unknown_keys(value: Mapping[str, Any], allowed: Iterable[str], field: str) -> None:
    allowed_set = set(allowed) | {"version"}
    extra = sorted(set(value) - allowed_set)
    if extra:
        raise BudgetShapeError(
            f"{field} payload has unsupported key(s): {', '.join(extra)}"
        )


# ---------------------------------------------------------------------------
# budgets payload
# ---------------------------------------------------------------------------


def validate_budgets(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a versioned ``budgets`` payload.

    Raises :class:`BudgetShapeError` on a payload missing a declared key, a
    malformed value, or an all-null set of limits; :class:`BudgetVersionError`
    on a newer-than-supported version. New writes are strict: an unversioned
    payload is rejected.

    Every declared key SHALL be present. The all-null rule spans every budget
    bound, including ``max_incident_attempts``, so an incident-attempt-only
    policy is a valid enforceable limit.
    """
    if not isinstance(payload, Mapping):
        raise BudgetShapeError(
            f"budgets must be a JSON object, got {type(payload).__name__}"
        )
    _check_version(payload, "budgets")
    _check_unknown_keys(payload, BUDGET_FIELDS, "budgets")
    _check_required_keys(payload, BUDGET_FIELDS, "budgets")

    normalized: dict[str, Any] = {"version": BUDGET_SCHEMA_VERSION}
    for field in BUDGET_LIMIT_FIELDS:
        value = payload[field]
        if value is not None and not _is_non_negative_number(value):
            raise BudgetShapeError(
                f"budgets '{field}' must be a non-negative number or null, got {value!r}"
            )
        normalized[field] = value

    attempts = payload[MAX_INCIDENT_ATTEMPTS_FIELD]
    if attempts is not None and not _is_non_negative_int(attempts):
        raise BudgetShapeError(
            f"budgets '{MAX_INCIDENT_ATTEMPTS_FIELD}' must be a non-negative "
            f"integer or null, got {attempts!r}"
        )
    normalized[MAX_INCIDENT_ATTEMPTS_FIELD] = attempts

    if all(normalized[field] is None for field in BUDGET_FIELDS):
        raise BudgetShapeError(
            "budgets payload has no enforceable limit: at least one of "
            f"{', '.join(BUDGET_FIELDS)} must be non-null"
        )
    return normalized


def encode_budgets(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a versioned ``budgets`` payload for a new write."""
    return validate_budgets(payload)


def decode_budgets(value: Any) -> dict[str, Any]:
    """Decode a stored ``budgets`` payload.

    Returns a tagged plain result: ``state`` is ``"versioned"`` (with the
    normalized payload under ``payload``) or ``"legacy_unversioned"`` (with the
    original value preserved under ``payload`` and never interpreted as a
    current policy).
    """
    if not isinstance(value, Mapping) or "version" not in value:
        return {"state": "legacy_unversioned", "field": "budgets", "payload": value}
    return {"state": "versioned", "field": "budgets", "payload": validate_budgets(value)}


# ---------------------------------------------------------------------------
# deadlines payload
# ---------------------------------------------------------------------------


def validate_deadlines(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a versioned ``deadlines`` payload.

    ``deadlines`` carries only :data:`DEADLINE_LIMIT_FIELDS`; every declared
    key SHALL be present and a null deadline disables it. Human-wait duration
    never appears here.
    """
    if not isinstance(payload, Mapping):
        raise BudgetShapeError(
            f"deadlines must be a JSON object, got {type(payload).__name__}"
        )
    _check_version(payload, "deadlines")
    _check_unknown_keys(payload, DEADLINE_FIELDS, "deadlines")
    _check_required_keys(payload, DEADLINE_FIELDS, "deadlines")

    normalized: dict[str, Any] = {"version": BUDGET_SCHEMA_VERSION}
    for field in DEADLINE_LIMIT_FIELDS:
        value = payload[field]
        if value is not None and not _is_non_negative_number(value):
            raise BudgetShapeError(
                f"deadlines '{field}' must be a non-negative number or null, got {value!r}"
            )
        normalized[field] = value
    return normalized


def encode_deadlines(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a versioned ``deadlines`` payload for a new write."""
    return validate_deadlines(payload)


def decode_deadlines(value: Any) -> dict[str, Any]:
    """Decode a stored ``deadlines`` payload, tagged like :func:`decode_budgets`."""
    if not isinstance(value, Mapping) or "version" not in value:
        return {"state": "legacy_unversioned", "field": "deadlines", "payload": value}
    return {
        "state": "versioned",
        "field": "deadlines",
        "payload": validate_deadlines(value),
    }


# ---------------------------------------------------------------------------
# standing_grants payload
# ---------------------------------------------------------------------------

_STANDING_GRANT_KEYS = frozenset({"version", "effects"})
_STANDING_GRANT_EFFECT_KEYS = frozenset({"max"})


def _is_non_negative_int_value(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_standing_grants(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a versioned ``standing_grants`` payload.

    The grant names a subset of :data:`STANDING_GRANT_EFFECTS` and, for each, a
    ``max`` bound (a non-negative integer, or null for unbounded). Every
    declared key SHALL be present and no unknown key is accepted. Raises
    :class:`StandingGrantShapeError` on a malformed payload and
    :class:`StandingGrantVersionError` on a newer-than-supported version. New
    writes are strict: an unversioned payload is rejected here.
    """
    if not isinstance(payload, Mapping):
        raise StandingGrantShapeError(
            f"standing_grants must be a JSON object, got {type(payload).__name__}"
        )
    if "version" not in payload:
        raise StandingGrantShapeError(
            "standing_grants payload is missing its 'version' key"
        )
    version = payload["version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise StandingGrantShapeError(
            f"standing_grants payload 'version' must be an integer, got {version!r}"
        )
    if version > STANDING_GRANT_SCHEMA_VERSION:
        raise StandingGrantVersionError(
            f"standing_grants version {version} is newer than supported version "
            f"{STANDING_GRANT_SCHEMA_VERSION}; reinstall the matching runtime"
        )
    if version < STANDING_GRANT_SCHEMA_VERSION:
        raise StandingGrantShapeError(
            f"standing_grants version {version} is older than supported version "
            f"{STANDING_GRANT_SCHEMA_VERSION}"
        )
    extra = sorted(set(payload) - _STANDING_GRANT_KEYS)
    if extra:
        raise StandingGrantShapeError(
            "standing_grants payload has unsupported key(s): " + ", ".join(extra)
        )
    if "effects" not in payload:
        raise StandingGrantShapeError(
            "standing_grants payload is missing its 'effects' key"
        )
    effects = payload["effects"]
    if not isinstance(effects, Mapping):
        raise StandingGrantShapeError(
            f"standing_grants 'effects' must be a JSON object, got "
            f"{type(effects).__name__}"
        )
    normalized: dict[str, dict[str, Any]] = {}
    for effect, bound in effects.items():
        if effect not in STANDING_GRANT_EFFECTS:
            raise StandingGrantShapeError(
                f"standing_grants names unknown effect {effect!r}; expected one of "
                + ", ".join(STANDING_GRANT_EFFECTS)
            )
        if not isinstance(bound, Mapping):
            raise StandingGrantShapeError(
                f"standing_grants effect {effect!r} must be an object, got "
                f"{type(bound).__name__}"
            )
        unknown = sorted(set(bound) - _STANDING_GRANT_EFFECT_KEYS)
        if unknown:
            raise StandingGrantShapeError(
                f"standing_grants effect {effect!r} has unsupported key(s): "
                + ", ".join(unknown)
            )
        if "max" not in bound:
            raise StandingGrantShapeError(
                f"standing_grants effect {effect!r} is missing its 'max' bound"
            )
        maximum = bound["max"]
        if maximum is not None and not _is_non_negative_int_value(maximum):
            raise StandingGrantShapeError(
                f"standing_grants effect {effect!r} 'max' must be a non-negative "
                f"integer or null, got {maximum!r}"
            )
        normalized[effect] = {"max": maximum}
    return {
        "version": STANDING_GRANT_SCHEMA_VERSION,
        "effects": {
            effect: normalized[effect]
            for effect in STANDING_GRANT_EFFECTS
            if effect in normalized
        },
    }


def encode_standing_grants(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a versioned ``standing_grants`` payload for a write."""
    return validate_standing_grants(payload)


def decode_standing_grants(value: Any) -> dict[str, Any]:
    """Decode a stored ``standing_grants`` payload, tagged like the others.

    ``state`` is ``versioned`` (normalized payload under ``payload``) or
    ``legacy_unversioned`` (the original value preserved and never interpreted
    as a current grant). A consumer fails closed and authorizes no effect.
    """
    if not isinstance(value, Mapping) or "version" not in value:
        return {
            "state": "legacy_unversioned",
            "field": STANDING_GRANT_FIELD,
            "payload": value,
        }
    return {
        "state": "versioned",
        "field": STANDING_GRANT_FIELD,
        "payload": validate_standing_grants(value),
    }


def standing_grants_value(policy: Mapping[str, Any] | None) -> Any:
    """Return the stored grant payload from *policy*, or ``None`` when absent.

    The grant is the operator-established standing permission, so it is read
    either as a top-level policy key or from the protected ``authority_config``
    payload, whichever shape the ledger holds.
    """
    if not isinstance(policy, Mapping):
        return None
    if STANDING_GRANT_FIELD in policy:
        return policy[STANDING_GRANT_FIELD]
    authority = policy.get("authority_config")
    if isinstance(authority, Mapping) and STANDING_GRANT_FIELD in authority:
        return authority[STANDING_GRANT_FIELD]
    return None


def standing_grant_state(policy: Mapping[str, Any] | None) -> str:
    """Return ``versioned`` or ``legacy_unversioned`` for *policy*'s grant."""
    return str(decode_standing_grants(standing_grants_value(policy))["state"])


#: Namespace prefix for the durable per-effect grant consumption counter, which
#: is stored in the existing ``incident_attempts`` table so it survives
#: ``opsx-plan reset`` like every other incident signature count.
GRANT_SIGNATURE_PREFIX = "standing_grant|"


def grant_effect_consumption(ledger: Any, job_id: int, effect: str) -> int:
    """Return the durable count of granted *effect* consumptions for a job."""
    return int(
        ledger.incident_attempt_count(
            int(job_id), signature=f"{GRANT_SIGNATURE_PREFIX}{effect}"
        )
    )


def record_grant_effect_consumption(ledger: Any, job_id: int, effect: str) -> int:
    """Durably record one granted *effect* consumption and return the new count."""
    return int(
        ledger.record_incident_attempt(
            int(job_id), signature=f"{GRANT_SIGNATURE_PREFIX}{effect}"
        )
    )


# ---------------------------------------------------------------------------
# Policy routing and fail-closed classification
# ---------------------------------------------------------------------------


def _decoded_payload(policy: Mapping[str, Any], field: str) -> tuple[str, Any]:
    """Return ``(state, payload)`` for one policy field.

    Accepts either a tagged decoder result (as produced by the ledger) or a raw
    payload value. A raw payload without a ``version`` key is classified
    ``legacy_unversioned``.
    """
    value = policy.get(field) if isinstance(policy, Mapping) else None
    if isinstance(value, Mapping) and value.get("state") in ("versioned", "legacy_unversioned"):
        return str(value["state"]), value.get("payload")
    if isinstance(value, Mapping) and "version" in value:
        return "versioned", value
    return "legacy_unversioned", value


def budget_policy_state(policy: Mapping[str, Any]) -> dict[str, str]:
    """Return the ``budgets``/``deadlines`` classification for *policy*.

    Mirrors ``model_policy_state``: each value is ``versioned`` or
    ``legacy_unversioned``. A consumer of a legacy payload fails closed for
    supervised dispatch rather than defaulting to unlimited spend.
    """
    budgets_state, _ = _decoded_payload(policy, "budgets")
    deadlines_state, _ = _decoded_payload(policy, "deadlines")
    return {"budgets": budgets_state, "deadlines": deadlines_state}


def policy_block_reason(policy: Mapping[str, Any]) -> str | None:
    """Return the reason supervised dispatch is blocked by *policy*, or ``None``.

    A ``legacy_unversioned`` payload (or an invalid one) carries no enforceable
    limits, so supervised dispatch fails closed with a named reason until an
    explicit operator revision records versioned payloads.
    """
    budgets_state, budgets_payload = _decoded_payload(policy, "budgets")
    deadlines_state, deadlines_payload = _decoded_payload(policy, "deadlines")
    if budgets_state == "legacy_unversioned":
        return (
            "budgets is legacy_unversioned and carries no enforceable limits; "
            "an explicit operator revision is required"
        )
    if deadlines_state == "legacy_unversioned":
        return (
            "deadlines is legacy_unversioned and carries no enforceable limits; "
            "an explicit operator revision is required"
        )
    try:
        validate_budgets(budgets_payload)
        validate_deadlines(deadlines_payload)
    except BudgetVersionError:
        raise
    except BudgetError as exc:
        return f"budget policy is invalid: {exc}"
    return None


def enforce_policy(policy: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate and return the ``(budgets, deadlines)`` payloads for *policy*.

    Raises the named budget error when the policy is legacy or malformed, so a
    gate never proceeds on absent limits. A newer-than-supported version
    propagates as :class:`BudgetVersionError`.
    """
    reason = policy_block_reason(policy)
    if reason is not None:
        raise BudgetShapeError(reason)
    _, budgets_payload = _decoded_payload(policy, "budgets")
    _, deadlines_payload = _decoded_payload(policy, "deadlines")
    return validate_budgets(budgets_payload), validate_deadlines(deadlines_payload)


# ---------------------------------------------------------------------------
# Reservation estimation and limit decisions (pure)
# ---------------------------------------------------------------------------


def reservation_estimate(rate_per_mtok: float, *, envelope: int = DEFAULT_TOKEN_CAP_ENVELOPE,
                         headroom: float = HEADROOM_MULTIPLIER) -> float:
    """Return ``rate x envelope x headroom`` in USD for one dispatch.

    ``rate_per_mtok`` is the worst-case per-million-token rate for the pinned
    model. The result is the conservative reserved estimate: because observed
    usage is known only after dispatch, a hard limit's overshoot is bounded by
    at most one in-flight action's headroom.
    """
    if not _is_non_negative_number(rate_per_mtok):
        raise BudgetShapeError(
            f"pricing rate must be a non-negative number, got {rate_per_mtok!r}"
        )
    return float(rate_per_mtok) * (int(envelope) / 1_000_000.0) * float(headroom)


def check_per_action(budgets_payload: Mapping[str, Any], *, reserved_cost_usd: float,
                     reserved_elapsed_minutes: float) -> None:
    """Refuse a reservation that would exceed a per-action limit.

    Raises :class:`BudgetExhaustedError` naming the exhausted limit.
    """
    per_action_cost = budgets_payload.get("per_action_cost_usd")
    if per_action_cost is not None and reserved_cost_usd > per_action_cost:
        raise BudgetExhaustedError(
            f"per_action_cost_usd exhausted: reserved ${reserved_cost_usd:.6f} "
            f"> limit ${per_action_cost:.6f}"
        )
    per_action_elapsed = budgets_payload.get("per_action_elapsed_minutes")
    if per_action_elapsed is not None and reserved_elapsed_minutes > per_action_elapsed:
        raise BudgetExhaustedError(
            f"per_action_elapsed_minutes exhausted: reserved "
            f"{reserved_elapsed_minutes:.6f}m > limit {per_action_elapsed:.6f}m"
        )


def check_total(budgets_payload: Mapping[str, Any], consumption: Mapping[str, Any], *,
                reserved_cost_usd: float, reserved_elapsed_minutes: float) -> None:
    """Refuse a reservation that would exceed a job-total limit.

    ``consumption`` is a job-consumption mapping (see
    :func:`sum_reservations`). Raises :class:`BudgetExhaustedError` naming the
    exhausted limit.
    """
    total_cost = budgets_payload.get("total_cost_usd")
    if total_cost is not None:
        projected = float(consumption.get("cost_usd", 0.0)) + reserved_cost_usd
        if projected > total_cost:
            raise BudgetExhaustedError(
                f"total_cost_usd exhausted: consumption ${consumption.get('cost_usd', 0.0):.6f} "
                f"+ reserved ${reserved_cost_usd:.6f} > limit ${total_cost:.6f}"
            )
    total_elapsed = budgets_payload.get("total_elapsed_minutes")
    if total_elapsed is not None:
        projected = (
            float(consumption.get("elapsed_minutes", 0.0)) + reserved_elapsed_minutes
        )
        if projected > total_elapsed:
            raise BudgetExhaustedError(
                f"total_elapsed_minutes exhausted: consumption "
                f"{consumption.get('elapsed_minutes', 0.0):.6f}m + reserved "
                f"{reserved_elapsed_minutes:.6f}m > limit {total_elapsed:.6f}m"
            )


# ---------------------------------------------------------------------------
# Retention, reconciliation, and exhaustion predicates (pure)
# ---------------------------------------------------------------------------


def observation_retains(observation_state: str) -> bool:
    """True when *observation_state* keeps the reservation rather than releasing it."""
    return observation_state in RETAINING_OBSERVATION_STATES


def classify_reservation_state(observation_state: str) -> str:
    """Return the ``reservation_state`` an observation implies.

    ``unknown``/``interrupted`` retain; every other state is ``reconciled``.
    """
    if observation_state not in OBSERVATION_STATES:
        raise BudgetShapeError(
            f"unknown observation_state {observation_state!r}; expected one of "
            f"{', '.join(OBSERVATION_STATES)}"
        )
    return "retained" if observation_retains(observation_state) else "reconciled"


def is_reconciled(reservation: Mapping[str, Any]) -> bool:
    return reservation.get("state") == "reconciled"


def is_retained(reservation: Mapping[str, Any]) -> bool:
    return reservation.get("state") == "retained"


def reservation_charge(reservation: Mapping[str, Any]) -> dict[str, float]:
    """Return the ``(cost_usd, elapsed_minutes)`` a reservation charges a job.

    A reconciled reservation charges its observed amounts; a reserved or
    retained reservation charges its reserved estimate (unresolved consumption
    is never treated as free).
    """
    if is_reconciled(reservation):
        return {
            "cost_usd": float(reservation.get("observed_cost_usd") or 0.0),
            "elapsed_minutes": float(reservation.get("observed_elapsed_minutes") or 0.0),
        }
    return {
        "cost_usd": float(reservation.get("reserved_cost_usd") or 0.0),
        "elapsed_minutes": float(reservation.get("reserved_elapsed_minutes") or 0.0),
    }


def sum_reservations(reservations: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Derive a job's consumption from its reservation records.

    Returns reconciled observed amounts plus reserved/retained estimates, with
    the component totals broken out so a caller can audit them.
    """
    reconciled_cost = 0.0
    reconciled_elapsed = 0.0
    reserved_cost = 0.0
    reserved_elapsed = 0.0
    retained_cost = 0.0
    retained_elapsed = 0.0
    count = 0
    for reservation in reservations:
        count += 1
        charge = reservation_charge(reservation)
        if is_reconciled(reservation):
            reconciled_cost += charge["cost_usd"]
            reconciled_elapsed += charge["elapsed_minutes"]
        elif is_retained(reservation):
            retained_cost += charge["cost_usd"]
            retained_elapsed += charge["elapsed_minutes"]
        else:
            reserved_cost += charge["cost_usd"]
            reserved_elapsed += charge["elapsed_minutes"]
    return {
        "cost_usd": reconciled_cost + reserved_cost + retained_cost,
        "elapsed_minutes": reconciled_elapsed + reserved_elapsed + retained_elapsed,
        "reconciled_cost_usd": reconciled_cost,
        "reconciled_elapsed_minutes": reconciled_elapsed,
        "reserved_cost_usd": reserved_cost,
        "reserved_elapsed_minutes": reserved_elapsed,
        "retained_cost_usd": retained_cost,
        "retained_elapsed_minutes": retained_elapsed,
        "reservation_count": count,
    }


# ---------------------------------------------------------------------------
# Reservation engine (state transitions against a supplied ledger handle)
# ---------------------------------------------------------------------------


def reserve(
    ledger: Any,
    *,
    job_id: int,
    action_id: int,
    role: str,
    requested_model: str,
    reserved_cost_usd: float,
    reserved_elapsed_minutes: float,
    policy: Mapping[str, Any],
    pricing_catalog_version: str | None = None,
) -> int:
    """Enforce the limits, then durably insert a reservation before dispatch.

    The estimate is computed at the orchestrator boundary (pricing catalog x
    token-cap envelope x headroom) and passed in as plain data. Raises
    :class:`BudgetExhaustedError` naming the exhausted limit, or the policy's
    named shape/version error for a legacy or malformed payload. The
    reservation row is written (and committed) before this returns; a write
    failure propagates so the caller blocks the dispatch rather than running
    unaccounted.
    """
    budgets_payload, _ = enforce_policy(policy)
    if not _is_non_negative_number(reserved_cost_usd):
        raise BudgetShapeError(
            f"reserved cost must be a non-negative number, got {reserved_cost_usd!r}"
        )
    if not _is_non_negative_number(reserved_elapsed_minutes):
        raise BudgetShapeError(
            "reserved elapsed minutes must be a non-negative number, got "
            f"{reserved_elapsed_minutes!r}"
        )
    check_per_action(
        budgets_payload,
        reserved_cost_usd=float(reserved_cost_usd),
        reserved_elapsed_minutes=float(reserved_elapsed_minutes),
    )
    consumption = ledger.consumption_for_job(job_id)
    check_total(
        budgets_payload,
        consumption,
        reserved_cost_usd=float(reserved_cost_usd),
        reserved_elapsed_minutes=float(reserved_elapsed_minutes),
    )
    return int(
        ledger.insert_reservation(
            job_id,
            action_id=action_id,
            role=role,
            requested_model=requested_model,
            reserved_cost_usd=float(reserved_cost_usd),
            reserved_elapsed_minutes=float(reserved_elapsed_minutes),
            pricing_catalog_version=pricing_catalog_version,
        )
    )


def reconcile(
    ledger: Any,
    *,
    reservation_id: int,
    observation_state: str,
    observed_input_tokens: int | None = None,
    observed_output_tokens: int | None = None,
    observed_cached_tokens: int | None = None,
    observed_reasoning_tokens: int | None = None,
    observed_cost_usd: float | None = None,
    observed_elapsed_minutes: float | None = None,
) -> str:
    """Write observed usage against a reservation, deduplicating repeats.

    Returns the resulting ``reservation_state``. An ``unknown`` or
    ``interrupted`` observation retains the reservation at its reserved
    estimate. A reservation that is already reconciled is left unchanged
    (its state is returned), so a duplicate completion or usage record never
    double-bills.
    """
    if observation_state not in OBSERVATION_STATES:
        raise BudgetShapeError(
            f"unknown observation_state {observation_state!r}; expected one of "
            f"{', '.join(OBSERVATION_STATES)}"
        )
    if observation_retains(observation_state):
        ledger.retain_reservation(reservation_id)
        return "retained"
    ledger.reconcile_reservation(
        reservation_id,
        observed_input_tokens=observed_input_tokens,
        observed_output_tokens=observed_output_tokens,
        observed_cached_tokens=observed_cached_tokens,
        observed_reasoning_tokens=observed_reasoning_tokens,
        observed_cost_usd=observed_cost_usd,
        observed_elapsed_minutes=observed_elapsed_minutes,
    )
    return "reconciled"


# ---------------------------------------------------------------------------
# Execution deadline accounting (pure)
# ---------------------------------------------------------------------------


def check_execution_deadline(deadlines_payload: Mapping[str, Any], *,
                             execution_elapsed_minutes: float) -> None:
    """Refuse when execution-elapsed time reaches ``execution_deadline_minutes``.

    Human-wait duration is excluded by construction (it never accrues to
    execution elapsed), so this check never charges a wait against the
    deadline.
    """
    limit = deadlines_payload.get("execution_deadline_minutes")
    if limit is not None and execution_elapsed_minutes >= limit:
        raise BudgetExhaustedError(
            f"execution_deadline_minutes exhausted: execution elapsed "
            f"{execution_elapsed_minutes:.6f}m >= limit {limit:.6f}m"
        )


# ---------------------------------------------------------------------------
# Incident attempt signatures (pure decisions)
# ---------------------------------------------------------------------------


def incident_signature(*, kind: str, change_id: str, stage: str,
                       discriminator: str = "") -> str:
    """Return the stable content hash identifying one incident class.

    ``sha256(kind | change_id | stage | failure discriminator)`` is enough to
    identify "the same incident" without embedding volatile detail such as
    timestamps or round numbers.
    """
    material = "|".join(
        (str(kind), str(change_id), str(stage), str(discriminator))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def max_incident_attempts(policy: Mapping[str, Any]) -> int | None:
    """Return the policy's bounded incident-attempt limit, or ``None``."""
    budgets_payload, _ = enforce_policy(policy)
    return budgets_payload.get(MAX_INCIDENT_ATTEMPTS_FIELD)


def record_incident_attempt(ledger: Any, *, job_id: int, signature: str,
                            policy: Mapping[str, Any]) -> int:
    """Increment and return the attempt count for ``(job_id, signature)``.

    Refuses further identical attempts with
    :class:`BoundedAttemptsExceededError` once the policy's
    ``max_incident_attempts`` is reached. The count is durable and survives
    ``opsx-plan reset`` because it lives in the external supervisor ledger.
    """
    limit = max_incident_attempts(policy)
    current = int(ledger.incident_attempt_count(job_id, signature))
    if limit is not None and current >= limit:
        raise BoundedAttemptsExceededError(
            f"incident signature {signature} reached max_incident_attempts "
            f"({limit}); an operator revision is required to attempt it again"
        )
    return int(ledger.record_incident_attempt(job_id, signature=signature))


# ---------------------------------------------------------------------------
# Bounded backoff and terminal human blockers (pure schedule, injected I/O)
# ---------------------------------------------------------------------------


def backoff_delay(attempt: int) -> float:
    """Return the capped exponential delay for 1-based *attempt*.

    ``min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_CAP_SECONDS)``.
    """
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    return min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_CAP_SECONDS)


BACKOFF_SCHEDULE: tuple[float, ...] = tuple(
    backoff_delay(attempt) for attempt in range(1, BACKOFF_MAX_ATTEMPTS + 1)
)


def run_with_bounded_backoff(
    operation: Callable[[], Any],
    *,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
    should_retry: Callable[[BaseException], bool] | None = None,
    max_attempts: int = BACKOFF_MAX_ATTEMPTS,
) -> Any:
    """Run *operation* with bounded exponential backoff on transient failures.

    Only failures accepted by *should_retry* (default: all) are retried.
    ``on_retry(attempt, delay, error)`` is invoked before each sleep so a
    caller can durably record the retry. Exhausting the bound re-raises the
    last error, which the caller surfaces as a blocked state rather than
    retrying indefinitely.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    predicate = should_retry or (lambda _exc: True)
    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            if not predicate(exc):
                raise
            last_error = exc
            if attempt >= max_attempts:
                break
            delay = backoff_delay(attempt)
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            sleep(delay)
    assert last_error is not None
    raise last_error


def blocker_state(reason: str, *, operator_action: str) -> dict[str, Any]:
    """Build an actionable terminal human-blocker record.

    A genuine human blocker is never retried or self-repaired; the record names
    the required operator action.
    """
    return {
        "state": "blocked",
        "reason": reason,
        "operator_action": operator_action,
        "retryable": False,
    }


# ---------------------------------------------------------------------------
# Operator-only policy revision
# ---------------------------------------------------------------------------


def operator_budget_increase(
    ledger: Any,
    *,
    job_id: int,
    revision: int,
    policy: Mapping[str, Any],
    operator: str,
) -> int:
    """Record an explicit operator budget/deadline revision.

    Budget and deadline values change only through this path, under the
    ledger's existing revision-increment rule. ``operator`` must be non-empty:
    no worker, agent, reset, or exhaustion response may raise or clear a limit,
    and a revision applies only to subsequent reservations because previously
    reconciled or retained consumption is never rewritten.
    """
    if not isinstance(operator, str) or not operator.strip():
        raise BudgetShapeError(
            "an explicit operator identity is required to revise a budget"
        )
    return int(
        ledger.revise_policy(
            job_id, revision=revision, policy=policy, operator=operator.strip()
        )
    )


__all__ = [
    "BUDGET_SCHEMA_VERSION",
    "STANDING_GRANT_SCHEMA_VERSION",
    "STANDING_GRANT_FIELD",
    "STANDING_GRANT_EFFECTS",
    "GRANT_SIGNATURE_PREFIX",
    "DEFAULT_TOKEN_CAP_ENVELOPE",
    "HEADROOM_MULTIPLIER",
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_CAP_SECONDS",
    "BACKOFF_MAX_ATTEMPTS",
    "BACKOFF_SCHEDULE",
    "BUDGET_LIMIT_FIELDS",
    "BUDGET_FIELDS",
    "MAX_INCIDENT_ATTEMPTS_FIELD",
    "DEADLINE_LIMIT_FIELDS",
    "DEADLINE_FIELDS",
    "OBSERVATION_STATES",
    "RESERVATION_STATES",
    "RETAINING_OBSERVATION_STATES",
    "BudgetError",
    "BudgetVersionError",
    "BudgetShapeError",
    "UnknownPricingError",
    "BudgetExhaustedError",
    "BoundedAttemptsExceededError",
    "StandingGrantError",
    "StandingGrantVersionError",
    "StandingGrantShapeError",
    "validate_budgets",
    "encode_budgets",
    "decode_budgets",
    "validate_deadlines",
    "encode_deadlines",
    "decode_deadlines",
    "validate_standing_grants",
    "encode_standing_grants",
    "decode_standing_grants",
    "standing_grants_value",
    "standing_grant_state",
    "grant_effect_consumption",
    "record_grant_effect_consumption",
    "budget_policy_state",
    "policy_block_reason",
    "enforce_policy",
    "reservation_estimate",
    "check_per_action",
    "check_total",
    "observation_retains",
    "classify_reservation_state",
    "is_reconciled",
    "is_retained",
    "reservation_charge",
    "sum_reservations",
    "reserve",
    "reconcile",
    "check_execution_deadline",
    "incident_signature",
    "max_incident_attempts",
    "record_incident_attempt",
    "backoff_delay",
    "run_with_bounded_backoff",
    "blocker_state",
    "operator_budget_increase",
]
