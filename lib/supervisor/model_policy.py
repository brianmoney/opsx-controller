"""Versioned supervised model-policy schema and pure predicates.

This module owns the content schema of the protected job policy's
``model_selection`` and ``inexpensive_allowlist`` payloads, the dispatch
identity record shape, and the pure fail-closed decisions the later
supervision changes apply.

Design rules enforced here:

- Standard library only. It imports **no** runtime package, including
  ``lib.models``; it operates on JSON-compatible plain mappings (dicts,
  lists, strings, ints, ``None``) and returns plain mappings and small
  decision records. Adapter-aware consumers pass resolver results in as data.
- Pure: no I/O, no clock, no database access.
- Forward-only versioning: a recorded nested payload version newer than
  :data:`MODEL_POLICY_VERSION` raises :class:`ModelPolicyVersionError` rather
  than being silently interpreted. This constant is independent of the outer
  ledger ``policy_version`` column; both currently equal 1 by coincidence.
- Pre-policy (unversioned) payloads read as ``legacy_unversioned``: a JSON
  value with no ``version`` key is returned unmodified and is never
  interpreted as a current payload. A consumer treats it as carrying no pins
  and fails closed.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# The single nested-payload version constant for both the model-selection and
# allowlist payloads. Independent of the outer ledger ``policy_version``.
MODEL_POLICY_VERSION = 1

# The frontier supervisor role: a recorded policy role exempt from the
# inexpensive allowlist but classified as budget-counted.
SUPERVISOR_ROLE = "supervisor"

# Every supervised dispatch role must resolve to an allowlisted inexpensive
# model. The legacy ``controller`` compile role is deliberately absent.
SUPERVISED_DISPATCH_ROLES: tuple[str, ...] = (
    "implementer",
    "reviewer",
    "archiver",
    "supervised_author",
    "acceptance_reviewer",
    "fixer",
    "verifier",
    "implementer_escalation",
)

# Every role a supervised job's policy may pin.
POLICY_ROLES: tuple[str, ...] = (SUPERVISOR_ROLE,) + SUPERVISED_DISPATCH_ROLES

# The standard stage-to-role mapping. A job records only stages it uses.
STANDARD_STAGE_MAPPING: dict[str, str] = {
    "create": "supervised_author",
    "implement": "implementer",
    "review": "reviewer",
    "archive": "archiver",
    "acceptance": "acceptance_reviewer",
    "fix": "fixer",
    "verify": "verifier",
    "escalate": "implementer_escalation",
}

OBSERVATION_STATES: tuple[str, ...] = ("requested", "observed", "unknown", "interrupted")
RESERVATION_STATES: tuple[str, ...] = ("reserved", "retained", "reconciled")


class ModelPolicyError(Exception):
    """A model-policy payload is malformed or has an unsupported shape."""


class ModelPolicyVersionError(ModelPolicyError):
    """A recorded model-policy version is newer than this code supports."""


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_version(value: Mapping[str, Any]) -> int:
    if "version" not in value:
        raise ModelPolicyError("model-policy payload is missing its 'version' key")
    version = value["version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise ModelPolicyError(
            f"model-policy payload 'version' must be an integer, got {version!r}"
        )
    if version > MODEL_POLICY_VERSION:
        raise ModelPolicyVersionError(
            f"model-policy version {version} is newer than supported version "
            f"{MODEL_POLICY_VERSION}; reinstall the matching runtime"
        )
    if version < MODEL_POLICY_VERSION:
        raise ModelPolicyError(
            f"model-policy version {version} is older than supported version "
            f"{MODEL_POLICY_VERSION}"
        )
    return version


# ---------------------------------------------------------------------------
# model_selection
# ---------------------------------------------------------------------------


def encode_model_selection(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a versioned ``model_selection`` payload.

    Raises :class:`ModelPolicyError` on a malformed payload and
    :class:`ModelPolicyVersionError` on a newer-than-supported version. New
    writes are strict: an unversioned payload is rejected.
    """
    if not isinstance(payload, Mapping):
        raise ModelPolicyError(
            f"model_selection must be a JSON object, got {type(payload).__name__}"
        )
    _check_version(payload)

    roles = payload.get("roles")
    if not isinstance(roles, Mapping) or not roles:
        raise ModelPolicyError("model_selection 'roles' must be a non-empty object")
    normalized_roles: dict[str, str] = {}
    for role, model in roles.items():
        if not _is_nonempty_str(role):
            raise ModelPolicyError("model_selection role names must be non-empty strings")
        role = role.strip()
        if role not in POLICY_ROLES:
            raise ModelPolicyError(
                f"model_selection role '{role}' is not a supervised policy role; "
                f"expected one of {', '.join(POLICY_ROLES)}"
            )
        if not _is_nonempty_str(model):
            raise ModelPolicyError(
                f"model_selection role '{role}' must pin a non-empty exact "
                f"model identifier"
            )
        normalized_roles[role] = model.strip()

    stages = payload.get("stages")
    if not isinstance(stages, Mapping) or not stages:
        raise ModelPolicyError("model_selection 'stages' must be a non-empty object")
    normalized_stages: dict[str, str] = {}
    for stage, role in stages.items():
        if not _is_nonempty_str(stage):
            raise ModelPolicyError("model_selection stage names must be non-empty strings")
        stage = stage.strip()
        if not _is_nonempty_str(role):
            raise ModelPolicyError(
                f"model_selection stage '{stage}' must name a non-empty role"
            )
        role = role.strip()
        expected_role = STANDARD_STAGE_MAPPING.get(stage)
        if expected_role is None:
            raise ModelPolicyError(
                f"model_selection stage '{stage}' is not a supported supervised "
                f"stage; expected one of {', '.join(sorted(STANDARD_STAGE_MAPPING))}"
            )
        if role != expected_role:
            raise ModelPolicyError(
                f"model_selection stage '{stage}' must map to "
                f"'{expected_role}', not '{role}'"
            )
        if role not in normalized_roles:
            raise ModelPolicyError(
                f"model_selection stage '{stage}' names role '{role}', which has "
                f"no entry in 'roles'"
            )
        normalized_stages[stage] = role

    return {
        "version": MODEL_POLICY_VERSION,
        "roles": normalized_roles,
        "stages": normalized_stages,
    }


def decode_model_selection(value: Any) -> dict[str, Any]:
    """Decode a stored ``model_selection`` payload.

    Returns a tagged plain result: ``state`` is ``"versioned"`` (with the
    normalized payload under ``payload``) or ``"legacy_unversioned"`` (with
    the original value preserved under ``payload`` and never interpreted as a
    current policy).
    """
    if not isinstance(value, Mapping) or "version" not in value:
        return {"state": "legacy_unversioned", "field": "model_selection", "payload": value}
    return {
        "state": "versioned",
        "field": "model_selection",
        "payload": encode_model_selection(value),
    }


# ---------------------------------------------------------------------------
# inexpensive_allowlist
# ---------------------------------------------------------------------------


def encode_allowlist(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a versioned ``inexpensive_allowlist`` payload."""
    if not isinstance(payload, Mapping):
        raise ModelPolicyError(
            f"inexpensive_allowlist must be a JSON object, got {type(payload).__name__}"
        )
    _check_version(payload)

    models = payload.get("models")
    if not isinstance(models, list):
        raise ModelPolicyError("inexpensive_allowlist 'models' must be an array")
    normalized: list[str] = []
    for entry in models:
        if not _is_nonempty_str(entry):
            raise ModelPolicyError(
                "inexpensive_allowlist entries must be non-empty, non-whitespace "
                "exact identifier strings"
            )
        normalized.append(entry.strip())

    source = payload.get("source")
    if not _is_nonempty_str(source):
        raise ModelPolicyError(
            "inexpensive_allowlist 'source' must be a non-empty source description"
        )

    return {"version": MODEL_POLICY_VERSION, "models": normalized, "source": source.strip()}


def decode_allowlist(value: Any) -> dict[str, Any]:
    """Decode a stored ``inexpensive_allowlist`` payload.

    Tagged exactly like :func:`decode_model_selection`. The old list-shaped
    allowlist has no ``version`` key and is classified ``legacy_unversioned``.
    """
    if not isinstance(value, Mapping) or "version" not in value:
        return {"state": "legacy_unversioned", "field": "inexpensive_allowlist", "payload": value}
    return {
        "state": "versioned",
        "field": "inexpensive_allowlist",
        "payload": encode_allowlist(value),
    }


# ---------------------------------------------------------------------------
# Dispatch identity record
# ---------------------------------------------------------------------------


def validate_dispatch_identity(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a dispatch identity record.

    The record is action/evidence data, distinct from the insert-only policy
    payload. Raises :class:`ModelPolicyError` on a malformed record.
    """
    if not isinstance(record, Mapping):
        raise ModelPolicyError(
            f"dispatch identity must be a JSON object, got {type(record).__name__}"
        )
    action_id = record.get("action_id")
    if not isinstance(action_id, int) or isinstance(action_id, bool):
        raise ModelPolicyError("dispatch identity 'action_id' must be an integer")

    role = record.get("role")
    if role not in POLICY_ROLES:
        raise ModelPolicyError(
            f"dispatch identity role {role!r} is not a policy role; expected one "
            f"of {', '.join(POLICY_ROLES)}"
        )

    requested = record.get("requested_model")
    if not _is_nonempty_str(requested):
        raise ModelPolicyError(
            "dispatch identity 'requested_model' must be a non-empty exact identifier"
        )
    requested = requested.strip()

    observed = record.get("observed_model")
    if observed is not None and not _is_nonempty_str(observed):
        raise ModelPolicyError(
            "dispatch identity 'observed_model' must be a non-empty exact "
            "identifier or null"
        )
    if observed is not None:
        observed = observed.strip()

    observation_state = record.get("observation_state")
    if observation_state not in OBSERVATION_STATES:
        raise ModelPolicyError(
            f"dispatch identity 'observation_state' must be one of "
            f"{', '.join(OBSERVATION_STATES)}"
        )

    reservation_state = record.get("reservation_state")
    if reservation_state not in RESERVATION_STATES:
        raise ModelPolicyError(
            f"dispatch identity 'reservation_state' must be one of "
            f"{', '.join(RESERVATION_STATES)}"
        )

    return {
        "action_id": action_id,
        "role": role,
        "requested_model": requested,
        "observed_model": observed,
        "observation_state": observation_state,
        "reservation_state": reservation_state,
    }


def requested_matches_pin(record: Mapping[str, Any], model_selection: Mapping[str, Any]) -> bool:
    """Return True when *record*'s ``requested_model`` equals its role's pin.

    A requested identity that does not equal the exact policy pin is a named
    policy block elsewhere; this predicate never repairs it by selecting
    another role or model.
    """
    roles = model_selection.get("roles", {}) if isinstance(model_selection, Mapping) else {}
    pin = roles.get(record.get("role")) if isinstance(roles, Mapping) else None
    return pin is not None and record.get("requested_model") == pin


def mismatch(record: Mapping[str, Any]) -> bool:
    """True only when ``observed_model`` is non-null and differs from requested."""
    observed = record.get("observed_model")
    if observed is None:
        return False
    return observed != record.get("requested_model")


def retain_on_unknown(record: Mapping[str, Any]) -> bool:
    """True when an unknown/interrupted observation keeps the reservation.

    An unresolved consumption is never treated as free: the reservation is
    classified ``retained`` rather than released.
    """
    return record.get("observation_state") in ("unknown", "interrupted")


# ---------------------------------------------------------------------------
# Consumer-side pre-dispatch policy check
# ---------------------------------------------------------------------------


def _decoded_payload(policy: Mapping[str, Any], field: str) -> tuple[str, Any]:
    """Return ``(state, payload)`` for one policy field.

    Accepts either a tagged decoder result (as produced by the ledger) or a
    raw payload value. A raw payload without a ``version`` key is classified
    ``legacy_unversioned``.
    """
    value = policy.get(field) if isinstance(policy, Mapping) else None
    if isinstance(value, Mapping) and value.get("state") in ("versioned", "legacy_unversioned"):
        return str(value["state"]), value.get("payload")
    if isinstance(value, Mapping) and "version" in value:
        return "versioned", value
    return "legacy_unversioned", value


def _warnings_for_role(role: str, warnings: Iterable[str]) -> list[str]:
    """Return only the syntax warnings that name *role*.

    Resolver warnings are free-form ``"<role>: <detail>"`` strings. An
    unrelated role's warning must not make this role unavailable, so a warning
    is scoped to *role* only when its leading token before the first ``:``
    equals the role exactly.
    """
    scoped: list[str] = []
    for warning in warnings:
        text = warning.strip()
        prefix, sep, _ = text.partition(":")
        if sep and prefix.strip() == role:
            scoped.append(text)
    return scoped


def _block(role: str, reason: str, *, model: str | None, budget_counted: bool = False) -> dict[str, Any]:
    return {
        "allowed": False,
        "role": role,
        "model": model,
        "reason": reason,
        "budget_counted": budget_counted,
    }


def check_dispatch(
    policy: Mapping[str, Any],
    *,
    role: str,
    resolved_model: str | None,
    syntax_warnings: Iterable[str] = (),
) -> dict[str, Any]:
    """Pure pre-dispatch policy check.

    Given the decoded job *policy*, a *role*, its *resolved_model* (or
    ``None``), and the resolver's identifier-syntax *syntax_warnings* as data,
    return a plain decision: ``allowed`` plus a named ``reason`` when blocked.

    The ``supervisor`` role is exempt from the allowlist check and classified
    ``budget_counted``. Every supervised dispatch role must be resolved,
    identifier-syntax valid, allowlisted, and equal to its exact policy pin.
    There is no fallback or inheritance, and the legacy ``controller`` role is
    not a supervised dispatch role.
    """
    warnings = _warnings_for_role(role, syntax_warnings)
    is_supervisor = role == SUPERVISOR_ROLE
    if role not in POLICY_ROLES:
        return _block(role, f"role '{role}' is not a supervised policy role", model=resolved_model)

    selection_state, selection = _decoded_payload(policy, "model_selection")
    allowlist_state, allowlist = _decoded_payload(policy, "inexpensive_allowlist")

    if selection_state == "legacy_unversioned":
        return _block(
            role,
            "model_selection is legacy_unversioned and carries no pins; an "
            "explicit operator revision is required",
            model=resolved_model,
            budget_counted=is_supervisor,
        )

    if not isinstance(selection, Mapping):
        return _block(
            role, "model_selection is not a valid policy payload",
            model=resolved_model, budget_counted=is_supervisor,
        )
    try:
        selection = encode_model_selection(selection)
    except ModelPolicyError as exc:
        return _block(
            role, f"model_selection is invalid: {exc}",
            model=resolved_model, budget_counted=is_supervisor,
        )

    if resolved_model is None:
        return _block(
            role, f"role '{role}' is unresolved", model=None,
            budget_counted=is_supervisor,
        )

    if warnings:
        return _block(
            role,
            f"role '{role}' resolved model '{resolved_model}' is unavailable: "
            f"{'; '.join(warnings)}",
            model=resolved_model,
            budget_counted=is_supervisor,
        )

    if not is_supervisor:
        if allowlist_state == "legacy_unversioned":
            return _block(
                role,
                "inexpensive_allowlist is legacy_unversioned and carries no "
                "entries; an explicit operator revision is required",
                model=resolved_model,
            )
        try:
            allowlist = encode_allowlist(allowlist)
        except ModelPolicyError as exc:
            return _block(role, f"inexpensive_allowlist is invalid: {exc}", model=resolved_model)
        if resolved_model not in allowlist["models"]:
            return _block(
                role,
                f"role '{role}' resolved model '{resolved_model}' is not on the "
                f"inexpensive allowlist",
                model=resolved_model,
            )

    if not requested_matches_pin({"role": role, "requested_model": resolved_model}, selection):
        return _block(
            role,
            f"role '{role}' resolved model '{resolved_model}' differs from its "
            f"exact model_selection pin",
            model=resolved_model,
            budget_counted=is_supervisor,
        )

    return {
        "allowed": True,
        "role": role,
        "model": resolved_model,
        "reason": None,
        "budget_counted": is_supervisor,
    }


__all__ = [
    "MODEL_POLICY_VERSION",
    "SUPERVISOR_ROLE",
    "SUPERVISED_DISPATCH_ROLES",
    "POLICY_ROLES",
    "STANDARD_STAGE_MAPPING",
    "OBSERVATION_STATES",
    "RESERVATION_STATES",
    "ModelPolicyError",
    "ModelPolicyVersionError",
    "encode_model_selection",
    "decode_model_selection",
    "encode_allowlist",
    "decode_allowlist",
    "validate_dispatch_identity",
    "requested_matches_pin",
    "mismatch",
    "retain_on_unknown",
    "check_dispatch",
]
