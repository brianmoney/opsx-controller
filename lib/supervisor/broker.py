"""Supervision broker: durable, revision-bound approval authority.

The broker is the sole authority that releases approval and acceptance gates
for a registered supervised job. It is a pure, standard-library-only module: it
takes an open ledger connection plus an authenticated-principal descriptor and
records durable receipts or raises a named refusal. The endpoint handlers and
the CLI mediation shim are thin adapters over this surface.

Design rules enforced here:

- Standard library only (``tomllib``/``json``/``hashlib``), no import of
  another runtime package; cross-module references go through the module
  object (see ``lib.supervisor`` import discipline).
- Receipts are append-only ledger transactions bound to a checkpoint (gate
  kind plus change id) and to a *material revision* hashed from the
  gate-relevant fields of the protected manifest snapshot, the snapshot's
  identity hash, and the current explicit policy revision. Worker-writable
  repo files never participate.
- Human-only gates release only through an operator receipt; delegated gates
  release only through the scoped service action. A worker-domain attempt is
  refused with :class:`BrokerMediationError` and records nothing.
- Every committed receipt transaction regenerates the JSON projection through
  the installed service writer, so endpoint-recorded receipts cannot leave the
  projection stale.
- No receipt path acquires or waits for the worktree execution lock.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

# Authenticated principal roles. The kernel reports the peer identity; this
# module only consumes the resulting trusted descriptor.
OPERATOR = "operator"
SERVICE = "service"
WORKER = "worker"
PRINCIPAL_ROLES = (OPERATOR, SERVICE, WORKER)

# Resolved gate authorities.
HUMAN_ONLY = "human-only"
DELEGATED = "delegated"

# Receipt kinds this module records through the broker surface.
APPROVAL = "approval"
ACCEPTANCE = "acceptance"
RESET = "reset"
PAUSE = "pause"
STEER = "steer"

# The service-reset policy bound key. A service reset requires an explicit
# policy value; the broker never invents an unlimited allowance.
SERVICE_RESET_BOUND_KEY = "max_service_resets"

# The gate-relevant subset of a change's manifest entry (task 2.2).
GATE_FIELD_KEYS = (
    "phase",
    "pause_before",
    "pause_before_human_only",
    "review_created",
    "depends_on",
)


class BrokerError(Exception):
    """Base class for broker refusals."""


class BrokerMediationError(BrokerError):
    """A worker-domain or unauthenticated mutation attempt in a registered job."""


class StaleMaterialError(BrokerError):
    """A relied-upon receipt no longer matches the current material revision."""


class BrokerUnavailableError(BrokerError):
    """A registered job exists but the broker path cannot be reached (fail closed)."""


@dataclass(frozen=True)
class BrokerPrincipal:
    """A trusted, authenticated caller identity.

    The kernel-reported peer identity is reduced to this descriptor by the
    transport layer; nothing in the worker's environment can construct one.
    """

    role: str
    name: str = ""
    uid: int | None = None

    def __post_init__(self) -> None:
        if self.role not in PRINCIPAL_ROLES:
            raise BrokerError(
                f"unknown principal role {self.role!r}; expected one of "
                f"{', '.join(PRINCIPAL_ROLES)}"
            )


# ---------------------------------------------------------------------------
# Protected snapshot parsing and material hashing
# ---------------------------------------------------------------------------


def parse_snapshot(content: str | None) -> dict[str, Any]:
    """Parse protected manifest *content* into ``{"plan", "changes"}``.

    The registered snapshot is the plan-manifest text (TOML); JSON is accepted
    as a fallback so a caller that stores a serialized form is not stranded.
    An unparseable snapshot is a named :class:`BrokerError`, never silently
    treated as an empty manifest.
    """
    if not content:
        raise BrokerError("protected manifest snapshot content is missing")
    data: Any
    try:
        import tomllib

        data = tomllib.loads(content)
    except Exception:
        try:
            data = json.loads(content)
        except Exception as exc:
            raise BrokerError(
                f"protected manifest snapshot is not parseable: {exc}"
            ) from exc
    if not isinstance(data, Mapping):
        raise BrokerError("protected manifest snapshot must be a mapping")
    plan = data.get("plan", {})
    if not isinstance(plan, Mapping):
        plan = {}
    raw_changes = data.get("changes", [])
    changes: dict[str, Mapping[str, Any]] = {}
    if isinstance(raw_changes, Sequence) and not isinstance(raw_changes, (str, bytes)):
        for entry in raw_changes:
            if isinstance(entry, Mapping) and entry.get("id"):
                cid = str(entry["id"])
                if cid in changes:
                    raise BrokerError(
                        f"protected manifest snapshot has duplicate change id {cid!r}"
                    )
                changes[cid] = entry
    return {"plan": dict(plan), "changes": changes}


def _normalize_phase(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise BrokerError("manifest phase must be an integer, not a boolean")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_depends_on(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise BrokerError("manifest depends_on must be a list of change ids")
    return [str(item) for item in value]


def gate_fields(snapshot: Mapping[str, Any], change_id: str) -> dict[str, Any]:
    """Return the gate-relevant subset for *change_id* from the protected snapshot.

    Resolves ``pause_before_human_only`` with the *same* strict validation the
    manifest loader applies (``lib.orchestrator.planref``): absent on a gated
    change resolves human-only; explicit ``false`` delegates; an ungated change
    carries no authority. A non-boolean value, or ``true`` on a change that is
    not gated with ``pause_before = true``, is invalid — such a protected
    snapshot fails closed with a named :class:`BrokerError` rather than being
    normalized into an ungated (and therefore trivially dispatchable) change.
    Only these fields participate in the material revision, so an unrelated
    manifest edit cannot invalidate a receipt.
    """
    changes = snapshot.get("changes", {}) if isinstance(snapshot, Mapping) else {}
    if change_id not in changes:
        raise BrokerError(
            f"change {change_id!r} is not present in the protected manifest snapshot"
        )
    entry = changes[change_id]
    raw_gate = entry.get("pause_before", False)
    if not isinstance(raw_gate, bool):
        raise BrokerError(
            f"change {change_id!r} pause_before must be a boolean"
        )
    pause_before = raw_gate
    raw_human = entry.get("pause_before_human_only")
    # Apply the manifest loader's strict validation to the protected snapshot:
    # a non-boolean value, and ``true`` on a change that is not gated, are
    # invalid. Fail closed with a named BrokerError rather than normalizing an
    # invalid protected snapshot into a different authority.
    if raw_human is not None and not isinstance(raw_human, bool):
        raise BrokerError(
            f"change {change_id!r} pause_before_human_only must be a boolean"
        )
    if not pause_before:
        if raw_human is True:
            raise BrokerError(
                f"change {change_id!r} pause_before_human_only = true requires "
                "pause_before = true on the same change"
            )
        human_only = False
    elif raw_human is None:
        human_only = True
    else:
        human_only = raw_human
    plan = snapshot.get("plan", {}) if isinstance(snapshot, Mapping) else {}
    return {
        "phase": _normalize_phase(entry.get("phase")),
        "pause_before": pause_before,
        "pause_before_human_only": human_only,
        "review_created": bool(plan.get("review_created", True)),
        "depends_on": _normalize_depends_on(entry.get("depends_on")),
    }


def resolved_authority(fields: Mapping[str, Any]) -> str | None:
    """Return ``human-only``, ``delegated``, or ``None`` for an ungated change."""
    if not fields.get("pause_before"):
        return None
    return HUMAN_ONLY if fields.get("pause_before_human_only") else DELEGATED


# ---------------------------------------------------------------------------
# Protected-snapshot selection (P<N>, --all, --failed)
# ---------------------------------------------------------------------------


_PHASE_ARG_RE = re.compile(r"P(\d+)\Z")


def snapshot_change_ids(snapshot: Mapping[str, Any]) -> list[str]:
    """Return the protected snapshot's registered change ids in declared order.

    The snapshot's ``[[changes]]`` entries preserve the operator's authored
    order (the ledger stores the manifest text verbatim), so registered-job
    batch selection reads order, phases, and membership from here and never
    from the repo-writable plan.
    """
    changes = snapshot.get("changes", {}) if isinstance(snapshot, Mapping) else {}
    if not isinstance(changes, Mapping):
        raise BrokerError("protected manifest snapshot changes must be a mapping")
    ordered: list[str] = []
    for change_id, entry in changes.items():
        if not isinstance(entry, Mapping):
            raise BrokerError(
                f"protected manifest snapshot entry {change_id!r} is not a mapping"
            )
        ordered.append(str(change_id))
    return ordered


def snapshot_change_order(snapshot: Mapping[str, Any]) -> list[str]:
    """Alias for :func:`snapshot_change_ids` naming its ordering intent."""
    return snapshot_change_ids(snapshot)


def snapshot_phase(
    snapshot: Mapping[str, Any], change_id: str
) -> int | None:
    """Return *change_id*'s protected phase value, or ``None`` when unphased.

    Reads only the protected snapshot; a worker edit to the repo plan's phase
    value cannot redirect a ``P<N>`` batch.
    """
    changes = snapshot.get("changes", {}) if isinstance(snapshot, Mapping) else {}
    if change_id not in changes:
        return None
    entry = changes[change_id]
    if not isinstance(entry, Mapping):
        return None
    return _normalize_phase(entry.get("phase"))


def snapshot_plan_value(snapshot: Mapping[str, Any], key: str, default: Any) -> Any:
    """Return a ``[plan]`` value from the protected snapshot, or *default*."""
    plan = snapshot.get("plan", {}) if isinstance(snapshot, Mapping) else {}
    if not isinstance(plan, Mapping):
        return default
    return plan.get(key, default)


def snapshot_review_created(snapshot: Mapping[str, Any]) -> bool:
    """Return the protected snapshot's ``review_created`` flag.

    Batch acceptance selection uses the protected plan value, never the
    repo-writable plan, so a worker cannot disable the acceptance gate.
    """
    return bool(snapshot_plan_value(snapshot, "review_created", True))


def snapshot_change_gate(
    snapshot: Mapping[str, Any], change_id: str
) -> tuple[bool, bool]:
    """Return ``(pause_before, human_only)`` for *change_id* from the snapshot.

    Resolves the same three flag-semantics cases as :func:`gate_fields`, so
    registered-job batch selection never consults the repo plan to decide
    whether a change is gated or which authority releases it.
    """
    fields = gate_fields(snapshot, change_id)
    return bool(fields["pause_before"]), bool(fields["pause_before_human_only"])


def load_protected_snapshot(conn: Any, job_id: int) -> dict[str, Any]:
    """Return the current protected snapshot parsed for *job_id*.

    A registered job without stored snapshot content, or with an unparseable
    one, raises :class:`BrokerError`: registered selection must fail closed
    rather than silently falling back to the repo plan.
    """
    policy = conn.current_policy(job_id)
    snapshot_hash = policy["manifest_snapshot_hash"]
    content = conn.manifest_snapshot(job_id, snapshot_hash)
    if content is None:
        raise BrokerError(
            f"job {job_id} has no protected manifest snapshot for hash "
            f"{snapshot_hash!r}; re-register the job with content"
        )
    return parse_snapshot(content)


def selected_snapshot_changes(
    conn: Any, job_id: int, args: Iterable[str]
) -> list[str] | None:
    """Resolve CLI selection *args* against the protected snapshot.

    Mirrors the CLI's selection grammar — ``P<N>`` expands to every protected
    change in that phase, an exact id selects itself — but every fact comes
    from the protected snapshot. A phase with no matches and an unknown id
    return ``None`` so the caller reports the refusal and exits non-zero.
    """
    snapshot = load_protected_snapshot(conn, job_id)
    order = snapshot_change_ids(snapshot)
    membership = set(order)
    resolved: list[str] = []
    for arg in args:
        match = _PHASE_ARG_RE.fullmatch(str(arg))
        if match:
            phase = int(match.group(1))
            matched = [
                cid for cid in order if snapshot_phase(snapshot, cid) == phase
            ]
            if not matched:
                return None
            resolved.extend(matched)
        elif str(arg) in membership:
            resolved.append(str(arg))
        else:
            return None
    return resolved


def material_hash(
    change_id: str,
    fields: Mapping[str, Any],
    snapshot_hash: str,
    policy_revision: int,
) -> str:
    """Return ``H(change_id, gate_fields, snapshot_hash, policy_revision)``.

    The hash is canonical (sorted keys, compact separators) and covers only the
    material gate inputs, so an unrelated update preserves receipt validity
    while a changed gate input or explicit revision invalidates it.
    """
    canonical = json.dumps(
        {
            "change_id": change_id,
            "gate_fields": {key: fields[key] for key in GATE_FIELD_KEYS},
            "snapshot_hash": snapshot_hash,
            "policy_revision": int(policy_revision),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def checkpoint_for(kind: str, change_id: str) -> str:
    """Return the checkpoint identity for a gate kind and change id."""
    return f"{kind}:{change_id}"


def _allowed_authorities(authority: str | None) -> frozenset[str]:
    if authority == HUMAN_ONLY:
        return frozenset({OPERATOR})
    if authority == DELEGATED:
        return frozenset({SERVICE, "delegated"})
    return frozenset()


# ---------------------------------------------------------------------------
# Snapshot/current-revision reading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterialState:
    """The current protected material state for a job and change."""

    snapshot_hash: str
    policy_revision: int
    fields: dict[str, Any]

    @property
    def authority(self) -> str | None:
        return resolved_authority(self.fields)


def material_state(conn: Any, job_id: int, change_id: str) -> MaterialState:
    """Read the current protected material state for *change_id*.

    Reads the policy revision and the protected snapshot content from the
    ledger; the repo plan is never consulted.
    """
    policy = conn.current_policy(job_id)
    snapshot_hash = policy["manifest_snapshot_hash"]
    content = conn.manifest_snapshot(job_id, snapshot_hash)
    if content is None:
        raise BrokerError(
            f"job {job_id} has no protected manifest snapshot for hash "
            f"{snapshot_hash!r}; re-register the job with content"
        )
    snapshot = parse_snapshot(content)
    return MaterialState(
        snapshot_hash=snapshot_hash,
        policy_revision=int(policy["revision"]),
        fields=gate_fields(snapshot, change_id),
    )


# ---------------------------------------------------------------------------
# Recording receipts
# ---------------------------------------------------------------------------

# The trusted-service projection writer. The service installs exactly one
# writer at boot (``lib.orchestrator.supervision.install_projection_writer``)
# that regenerates the JSON projection (approvals, acceptance flags, change
# records) from broker and ledger state; every receipt transaction invokes it
# automatically. There is no optional per-call callback, so a recorded receipt
# can never silently leave the projection stale.
ProjectionWriter = Callable[[Any, int], None]

_PROJECTION_WRITER: ProjectionWriter | None = None


def set_projection_writer(writer: ProjectionWriter | None) -> ProjectionWriter | None:
    """Install the trusted-service projection writer, returning the previous one.

    ``None`` clears it. The writer is called as ``writer(ledger, job_id)`` after
    each committed receipt transaction.
    """
    global _PROJECTION_WRITER
    previous = _PROJECTION_WRITER
    _PROJECTION_WRITER = writer
    return previous


def projection_writer() -> ProjectionWriter | None:
    """Return the installed trusted-service projection writer, or ``None``."""
    return _PROJECTION_WRITER


def require_projection_writer() -> ProjectionWriter:
    """Return the installed service projection writer, or fail closed.

    Every committed receipt transaction must be followed by a projection
    regeneration, so a missing writer is a service misconfiguration rather than
    a reason to skip projecting: a registered job with a reachable broker but no
    projection writer would otherwise record authority records the legacy read
    paths can never observe. Raising :class:`BrokerUnavailableError` keeps the
    mediated path fail-closed.
    """
    writer = _PROJECTION_WRITER
    if writer is None:
        raise BrokerUnavailableError(
            "no service projection writer is installed; the trusted service must "
            "install one before recording broker receipts so the JSON projection "
            "cannot be left stale"
        )
    return writer


def regenerate_projection(conn: Any, job_id: int) -> None:
    """Regenerate the JSON projection from broker state after a transaction.

    The trusted service installs exactly one writer at boot (see
    :func:`set_projection_writer`); every committed receipt transaction invokes
    it here. There is no optional per-call callback: when no writer is installed
    the broker fails closed with :class:`BrokerUnavailableError` instead of
    committing an authority record whose projection would silently stay stale.
    """
    require_projection_writer()(conn, job_id)


@dataclass(frozen=True)
class RecordedReceipt:
    """A receipt as recorded, for projection and reporting."""

    receipt_id: int
    change_id: str
    kind: str
    checkpoint: str
    material_hash: str
    authority: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.receipt_id,
            "change_id": self.change_id,
            "kind": self.kind,
            "checkpoint": self.checkpoint,
            "material_hash": self.material_hash,
            "authority": self.authority,
        }


def record_approval(
    conn: Any,
    job_id: int,
    *,
    principal: BrokerPrincipal,
    change_ids: Iterable[str],
    kind: str = APPROVAL,
    detail: str | None = None,
) -> list[RecordedReceipt]:
    """Record approval/acceptance receipts for *change_ids* in one transaction.

    Only the operator principal may release an approval/acceptance gate. Each
    affected change must resolve to human-only authority (a delegated gate is
    released through the scoped service action, not by an operator receipt) and
    must currently await the gate. The whole batch is one durable transaction,
    after which the JSON projection is regenerated from broker state.
    """
    if principal.role != OPERATOR:
        raise BrokerMediationError(
            f"principal {principal.name or principal.uid!r} may not record an "
            f"{kind} receipt; only the operator path releases a human-only gate"
        )
    if kind not in (APPROVAL,):
        raise BrokerError(f"record_approval does not record kind {kind!r}")
    conn.get_job(job_id)
    seen: set[str] = set()
    pending: list[RecordedReceipt] = []
    rows: list[Mapping[str, Any]] = []
    for change_id in change_ids:
        if change_id in seen:
            continue
        seen.add(change_id)
        state = material_state(conn, job_id, change_id)
        if state.authority != HUMAN_ONLY:
            raise BrokerMediationError(
                f"change {change_id} resolves to {state.authority!r}; a "
                "delegated gate is released only through the scoped service "
                "action, not an operator approval"
            )
        checkpoint = checkpoint_for(kind, change_id)
        digest = material_hash(
            change_id, state.fields, state.snapshot_hash, state.policy_revision
        )
        pending.append(
            RecordedReceipt(
                receipt_id=0,
                change_id=change_id,
                kind=kind,
                checkpoint=checkpoint,
                material_hash=digest,
                authority=OPERATOR,
            )
        )
        rows.append(
            {
                "change_id": change_id,
                "kind": kind,
                "checkpoint": checkpoint,
                "material_hash": digest,
                "authority": OPERATOR,
                "actor_principal": principal.name or str(principal.uid or ""),
                "detail": detail,
            }
        )
    if not rows:
        return []
    # Fail closed *before* committing: a receipt must never be recorded without
    # a projection writer to regenerate the JSON projection from broker state.
    require_projection_writer()
    receipt_ids = conn.record_receipts(job_id, rows)
    recorded = [
        RecordedReceipt(
            receipt_id=receipt_id,
            change_id=item.change_id,
            kind=item.kind,
            checkpoint=item.checkpoint,
            material_hash=item.material_hash,
            authority=item.authority,
        )
        for receipt_id, item in zip(receipt_ids, pending)
    ]
    regenerate_projection(conn, job_id)
    return recorded


def record_acceptance(
    conn: Any,
    job_id: int,
    *,
    principal: BrokerPrincipal,
    change_ids: Iterable[str],
) -> list[RecordedReceipt]:
    """Record acceptance receipts (operator authority) in one transaction.

    Acceptance is not a ``pause_before`` gate, so no human-only/delegated
    authority resolution applies; only the operator principal may accept. The
    batch is one durable transaction, after which the JSON projection is
    regenerated from broker state.
    """
    if principal.role != OPERATOR:
        raise BrokerMediationError(
            f"principal {principal.name or principal.uid!r} may not record an "
            "acceptance receipt; only the operator path may accept"
        )
    conn.get_job(job_id)
    seen: set[str] = set()
    pending: list[RecordedReceipt] = []
    rows: list[Mapping[str, Any]] = []
    for change_id in change_ids:
        if change_id in seen:
            continue
        seen.add(change_id)
        state = material_state(conn, job_id, change_id)
        checkpoint = checkpoint_for(ACCEPTANCE, change_id)
        digest = material_hash(
            change_id, state.fields, state.snapshot_hash, state.policy_revision
        )
        pending.append(
            RecordedReceipt(
                receipt_id=0, change_id=change_id, kind=ACCEPTANCE,
                checkpoint=checkpoint, material_hash=digest, authority=OPERATOR,
            )
        )
        rows.append(
            {
                "change_id": change_id,
                "kind": ACCEPTANCE,
                "checkpoint": checkpoint,
                "material_hash": digest,
                "authority": OPERATOR,
                "actor_principal": principal.name or str(principal.uid or ""),
                "detail": "operator acceptance",
            }
        )
    if not rows:
        return []
    # Fail closed *before* committing (see :func:`record_approval`).
    require_projection_writer()
    receipt_ids = conn.record_receipts(job_id, rows)
    recorded = [
        RecordedReceipt(
            receipt_id=receipt_id, change_id=item.change_id, kind=item.kind,
            checkpoint=item.checkpoint, material_hash=item.material_hash,
            authority=item.authority,
        )
        for receipt_id, item in zip(receipt_ids, pending)
    ]
    regenerate_projection(conn, job_id)
    return recorded


def release_delegated_gate(
    conn: Any,
    job_id: int,
    *,
    principal: BrokerPrincipal,
    change_id: str,
) -> RecordedReceipt:
    """Record a delegated approval receipt through the scoped service action.

    Accepted only when the change's resolved authority is delegated and the
    requesting principal is the scoped service identity. A worker principal is
    refused with :class:`BrokerMediationError` and records nothing. A committed
    receipt regenerates the JSON projection from broker state.
    """
    if principal.role != SERVICE:
        raise BrokerMediationError(
            "release_delegated_gate is available only to the scoped job service "
            "identity"
        )
    conn.get_job(job_id)
    state = material_state(conn, job_id, change_id)
    if state.authority != DELEGATED:
        raise BrokerMediationError(
            f"change {change_id} resolves to {state.authority!r}, not delegated; "
            "the scoped service action cannot release it"
        )
    checkpoint = checkpoint_for(APPROVAL, change_id)
    digest = material_hash(
        change_id, state.fields, state.snapshot_hash, state.policy_revision
    )
    # Fail closed *before* committing (see :func:`record_approval`).
    require_projection_writer()
    receipt_id = conn.record_receipt(
        job_id,
        change_id=change_id,
        kind=APPROVAL,
        checkpoint=checkpoint,
        material_hash=digest,
        authority=SERVICE,
        actor_principal=principal.name or str(principal.uid or ""),
        detail="delegated scoped service release",
    )
    recorded = RecordedReceipt(
        receipt_id=receipt_id, change_id=change_id, kind=APPROVAL,
        checkpoint=checkpoint, material_hash=digest, authority=SERVICE,
    )
    regenerate_projection(conn, job_id)
    return recorded


def reset_change(
    conn: Any,
    job_id: int,
    *,
    principal: BrokerPrincipal,
    change_id: str,
    policy_bound: Mapping[str, Any] | None = None,
) -> RecordedReceipt:
    """Record a bounded reset receipt through an authorized path.

    An operator reset is always authorized. A service reset requires an
    explicit policy bound (:data:`SERVICE_RESET_BOUND_KEY`) and is refused once
    the bound is reached. A worker-domain reset is refused with
    :class:`BrokerMediationError` and records nothing. A committed receipt
    regenerates the JSON projection from broker state.
    """
    if principal.role == WORKER:
        raise BrokerMediationError(
            "worker-domain processes may not reset a change in a registered "
            "supervised job; a reset is an operator or policy-bound service action"
        )
    if principal.role not in (OPERATOR, SERVICE):
        raise BrokerMediationError(
            f"principal role {principal.role!r} may not reset a registered job"
        )
    conn.get_job(job_id)
    state = material_state(conn, job_id, change_id)
    authority = principal.role
    detail = "operator reset"
    if principal.role == SERVICE:
        if not isinstance(policy_bound, Mapping) or SERVICE_RESET_BOUND_KEY not in policy_bound:
            raise BrokerError(
                "a service reset requires an explicit policy bound "
                f"({SERVICE_RESET_BOUND_KEY})"
            )
        bound = policy_bound[SERVICE_RESET_BOUND_KEY]
        if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
            raise BrokerError(
                f"policy bound {SERVICE_RESET_BOUND_KEY} must be a non-negative integer"
            )
        used = sum(
            1
            for row in conn.receipts_for_change(job_id, change_id, kind=RESET)
            if row["authority"] == SERVICE
        )
        if used >= bound:
            raise BrokerError(
                f"service reset bound reached for {change_id} "
                f"({used}/{bound}); the operator must reset explicitly"
            )
        detail = f"policy-bound service reset ({used + 1}/{bound})"
    checkpoint = checkpoint_for(RESET, change_id)
    digest = material_hash(
        change_id, state.fields, state.snapshot_hash, state.policy_revision
    )
    # Fail closed *before* committing (see :func:`record_approval`).
    require_projection_writer()
    receipt_id = conn.record_receipt(
        job_id,
        change_id=change_id,
        kind=RESET,
        checkpoint=checkpoint,
        material_hash=digest,
        authority=authority,
        actor_principal=principal.name or str(principal.uid or ""),
        detail=detail,
    )
    recorded = RecordedReceipt(
        receipt_id=receipt_id, change_id=change_id, kind=RESET,
        checkpoint=checkpoint, material_hash=digest, authority=authority,
    )
    regenerate_projection(conn, job_id)
    return recorded


def record_pause_or_steer(
    conn: Any,
    job_id: int,
    *,
    principal: BrokerPrincipal,
    change_id: str,
    kind: str,
    detail: str | None = None,
) -> RecordedReceipt:
    """Record a durable ``pause`` or ``steer`` receipt bound to the change.

    A worker principal is refused with :class:`BrokerMediationError` and records
    nothing; only the operator or the scoped service identity may record one.
    The receipt path never acquires the execution lock. A committed receipt
    regenerates the JSON projection from broker state.
    """
    if kind not in (PAUSE, STEER):
        raise BrokerError(f"unknown pause/steer kind: {kind}")
    if principal.role == WORKER:
        raise BrokerMediationError(
            "a worker-domain process may not record a pause or steer request "
            "except through its scoped service action"
        )
    if principal.role not in (OPERATOR, SERVICE):
        raise BrokerMediationError(
            f"principal role {principal.role!r} may not record a {kind} request"
        )
    conn.get_job(job_id)
    state = material_state(conn, job_id, change_id)
    checkpoint = checkpoint_for(kind, change_id)
    digest = material_hash(
        change_id, state.fields, state.snapshot_hash, state.policy_revision
    )
    # Fail closed *before* committing (see :func:`record_approval`).
    require_projection_writer()
    receipt_id = conn.record_receipt(
        job_id,
        change_id=change_id,
        kind=kind,
        checkpoint=checkpoint,
        material_hash=digest,
        authority=principal.role,
        actor_principal=principal.name or str(principal.uid or ""),
        detail=detail,
    )
    recorded = RecordedReceipt(
        receipt_id=receipt_id, change_id=change_id, kind=kind,
        checkpoint=checkpoint, material_hash=digest, authority=principal.role,
    )
    regenerate_projection(conn, job_id)
    return recorded


# ---------------------------------------------------------------------------
# Gate resolution and resume revalidation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateResolution:
    """The resolver's answer for one change."""

    change_id: str
    dispatchable: bool
    reason: str
    authority: str | None
    checkpoint: str | None
    material_hash: str
    receipt_id: int | None = None


def _matching_receipt(
    conn: Any,
    job_id: int,
    *,
    change_id: str,
    kind: str,
    checkpoint: str,
    digest: str,
    allowed: frozenset[str],
) -> Any | None:
    rows = conn.receipts_for_change(job_id, change_id, kind=kind)
    for row in reversed(rows):
        if row["checkpoint"] != checkpoint:
            continue
        if row["material_hash"] != digest:
            continue
        if allowed and row["authority"] not in allowed:
            continue
        return row
    return None


def resolve_gate(conn: Any, job_id: int, change_id: str) -> GateResolution:
    """Return whether *change_id* is dispatchable under broker receipts.

    A change is dispatchable when it is ungated, or when a receipt exists whose
    checkpoint matches and whose ``material_hash`` equals the current material
    revision. The resolved authority is enforced: a human-only gate requires an
    operator receipt and a delegated gate requires the scoped service receipt.
    """
    state = material_state(conn, job_id, change_id)
    digest = material_hash(
        change_id, state.fields, state.snapshot_hash, state.policy_revision
    )
    if state.authority is None:
        return GateResolution(
            change_id=change_id, dispatchable=True, reason="ungated",
            authority=None, checkpoint=None, material_hash=digest,
        )
    checkpoint = checkpoint_for(APPROVAL, change_id)
    allowed = _allowed_authorities(state.authority)
    matching = _matching_receipt(
        conn, job_id, change_id=change_id, kind=APPROVAL,
        checkpoint=checkpoint, digest=digest, allowed=allowed,
    )
    if matching is None:
        return GateResolution(
            change_id=change_id, dispatchable=False,
            reason=f"awaiting {state.authority} approval",
            authority=state.authority, checkpoint=checkpoint, material_hash=digest,
        )
    return GateResolution(
        change_id=change_id, dispatchable=True,
        reason="receipt satisfies the gate",
        authority=state.authority, checkpoint=checkpoint, material_hash=digest,
        receipt_id=int(matching["id"]),
    )


def is_dispatchable(conn: Any, job_id: int, change_id: str) -> bool:
    """Convenience predicate over :func:`resolve_gate`."""
    return resolve_gate(conn, job_id, change_id).dispatchable


def assert_dispatchable(conn: Any, job_id: int, change_id: str) -> GateResolution:
    """Return the resolution or raise :class:`StaleMaterialError`/`BrokerError`."""
    resolution = resolve_gate(conn, job_id, change_id)
    if not resolution.dispatchable:
        raise StaleMaterialError(
            f"change {change_id} is not dispatchable: {resolution.reason}"
        )
    return resolution


def relied_upon_receipts(conn: Any, job_id: int) -> list[Any]:
    """Return every approval/acceptance receipt a job currently relies upon.

    The latest receipt per ``(change_id, kind)`` whose checkpoint and material
    hash still match the current revision is "relied upon"; an older receipt
    superseded by a newer one is not. This is the revalidation input.
    """
    rows = conn.receipts_after(job_id, 0)
    latest: dict[tuple[str, str], Any] = {}
    for row in rows:
        latest[(row["change_id"], row["kind"])] = row
    return list(latest.values())


def revalidate_receipts(
    conn: Any,
    job_id: int,
    *,
    change_ids: Iterable[str] | None = None,
    record_incident: bool = True,
) -> list[dict[str, Any]]:
    """Revalidate relied-upon receipts against the current material revision.

    Any relied-upon receipt whose material hash no longer matches the current
    revision re-arms its gate. When *record_incident* is set, each stale gate
    is recorded as a durable incident in the job's incident flow. Returns the
    stale records; a caller about to dispatch raises
    :class:`StaleMaterialError` rather than dispatching.
    """
    wanted = None if change_ids is None else set(change_ids)
    stale: list[dict[str, Any]] = []
    for row in relied_upon_receipts(conn, job_id):
        change_id = row["change_id"]
        if wanted is not None and change_id not in wanted:
            continue
        try:
            state = material_state(conn, job_id, change_id)
            digest = material_hash(
                change_id, state.fields, state.snapshot_hash, state.policy_revision
            )
        except BrokerError:
            digest = None
        if digest == row["material_hash"]:
            continue
        stale.append(
            {
                "change_id": change_id,
                "kind": row["kind"],
                "receipt_id": int(row["id"]),
                "receipt_material_hash": row["material_hash"],
                "current_material_hash": digest,
            }
        )
    if stale and record_incident:
        for record in stale:
            conn.record_incident(
                job_id,
                kind="stale_material",
                summary=(
                    f"{record['kind']} receipt {record['receipt_id']} for "
                    f"{record['change_id']} no longer matches the current "
                    "material revision; the gate is re-armed"
                ),
            )
    return stale


def assert_resume_clear(
    conn: Any,
    job_id: int,
    *,
    change_ids: Iterable[str] | None = None,
) -> None:
    """Raise :class:`StaleMaterialError` when any relied-upon receipt is stale."""
    stale = revalidate_receipts(conn, job_id, change_ids=change_ids)
    if stale:
        names = ", ".join(record["change_id"] for record in stale)
        raise StaleMaterialError(
            f"stale material revision for {names}; the gate is re-armed and the "
            "change must be approved again"
        )


# ---------------------------------------------------------------------------
# Durable wake-up
# ---------------------------------------------------------------------------


@dataclass
class ReceiptWakeTracker:
    """Durable per-job receipt high-water tracking.

    The append is the durable record; this tracker only remembers how far a
    consumer has scanned. A restart constructs a fresh tracker (high water 0)
    and rescans every receipt, so a missed in-process notify loses nothing.
    """

    high_water: int = 0

    def scan(self, conn: Any, job_id: int) -> list[Any]:
        """Return receipts with ``id > high_water`` and advance the mark."""
        rows = conn.receipts_after(job_id, self.high_water)
        if rows:
            self.high_water = max(int(row["id"]) for row in rows)
        return rows

    def notify(self, conn: Any, job_id: int) -> list[Any]:
        """An immediate scan triggered by a notify; the scan is authoritative.

        No execution lock is acquired on this path.
        """
        return self.scan(conn, job_id)


def pending_receipts(conn: Any, job_id: int, high_water: int = 0) -> list[Any]:
    """Return a job's receipts above *high_water* (the boot/wake scan)."""
    return conn.receipts_after(job_id, high_water)


__all__ = [
    "ACCEPTANCE",
    "APPROVAL",
    "BrokerError",
    "BrokerMediationError",
    "BrokerPrincipal",
    "BrokerUnavailableError",
    "DELEGATED",
    "GATE_FIELD_KEYS",
    "GateResolution",
    "HUMAN_ONLY",
    "MaterialState",
    "OPERATOR",
    "PAUSE",
    "PRINCIPAL_ROLES",
    "ProjectionWriter",
    "ReceiptWakeTracker",
    "RecordedReceipt",
    "RESET",
    "SERVICE",
    "SERVICE_RESET_BOUND_KEY",
    "STEER",
    "StaleMaterialError",
    "WORKER",
    "assert_dispatchable",
    "assert_resume_clear",
    "checkpoint_for",
    "gate_fields",
    "is_dispatchable",
    "load_protected_snapshot",
    "material_hash",
    "material_state",
    "parse_snapshot",
    "pending_receipts",
    "projection_writer",
    "record_acceptance",
    "record_approval",
    "record_pause_or_steer",
    "regenerate_projection",
    "release_delegated_gate",
    "require_projection_writer",
    "relied_upon_receipts",
    "reset_change",
    "resolve_gate",
    "resolved_authority",
    "revalidate_receipts",
    "selected_snapshot_changes",
    "set_projection_writer",
    "snapshot_change_gate",
    "snapshot_change_ids",
    "snapshot_change_order",
    "snapshot_phase",
    "snapshot_plan_value",
    "snapshot_review_created",
]
