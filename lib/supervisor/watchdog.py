"""Deterministic watchdog: job classification and bounded reconstitution.

This module is the client-neutral core of supervision-after-restart. It owns

- the three independent watchdog signals (liveness, progress, deadline),
- the pure classification vocabulary (``live``, ``quiet``, ``stalled``,
  ``dead``, ``expected_human_wait``), and
- a thin :class:`Watchdog` runner whose reconnect and re-drive steps are
  injected callables.

Design rules:

- Standard library only. Like the rest of :mod:`lib.supervisor` it never
  imports ``lib.orchestrator``, ``lib.metrics``, ``lib.pricing``, or
  ``lib.models``, and it performs no side effect at import time.
- Classification is a pure function of durable ledger state, the recorded
  fencing identity, the clock, and the worktree lock probe. It never dispatches,
  repairs, or mutates state.
- Liveness comes from the recorded fencing identity
  (:func:`owner_identity_is_live` over the ``(pid, process_start, boot_id)``
  triple), never a bare process id, and never merely from whether the fencing
  record still claims the lock: a released record whose identity is still live
  is still a live owner.
- Reconstitution happens only after verified quiescence and only for a job
  that already had a prior execution; restart attempts are bounded by the
  job's incident-attempt limit under a stable restart signature.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from lib.supervisor import broker as broker_mod
from lib.supervisor import budgets as budgets_mod
from lib.supervisor import clock as clock_mod
from lib.supervisor import ledger as ledger_mod
from lib.supervisor import lock as lock_mod
from lib.supervisor import session_bridge as session_bridge_mod

# ---------------------------------------------------------------------------
# Tuning constants (module-level and injectable in tests)
# ---------------------------------------------------------------------------

# How long a job may go without durable progress before the progress signal is
# false. Tuned to the action timeout rather than the whole stage.
PROGRESS_WINDOW_SECONDS = 300.0

# How long a job may go without durable progress before it is stalled rather
# than merely quiet.
STALL_THRESHOLD_SECONDS = 1800.0

# How many recent watchdog events the read-only report surfaces.
RECENT_EVENT_LIMIT = 20

# Classification vocabulary. Exactly one is reported per job.
CLASS_LIVE = "live"
CLASS_QUIET = "quiet"
CLASS_STALLED = "stalled"
CLASS_DEAD = "dead"
CLASS_EXPECTED_HUMAN_WAIT = "expected_human_wait"
CLASSIFICATIONS = (
    CLASS_LIVE,
    CLASS_QUIET,
    CLASS_STALLED,
    CLASS_DEAD,
    CLASS_EXPECTED_HUMAN_WAIT,
)

# Durable watchdog event kinds. Only classification *transitions* and
# reconstitution events are appended, so the table cannot grow on every idle
# tick.
EVENT_CLASSIFICATION = "classification"
EVENT_RECONNECTED = "reconnected"
EVENT_RECONSTITUTED = "reconstituted"
EVENT_BLOCKED = "blocked"
EVENT_BOUNDED = "bounded"
EVENT_SURFACED = "surfaced"
EVENT_KINDS = (
    EVENT_CLASSIFICATION,
    EVENT_RECONNECTED,
    EVENT_RECONSTITUTED,
    EVENT_BLOCKED,
    EVENT_BOUNDED,
    EVENT_SURFACED,
)

# Reconstitution decision vocabulary (not part of the classification).
ACTION_NONE = "none"
ACTION_RECONNECT = "reconnect"
ACTION_RECONSTITUTE = "reconstitute"
ACTION_SURFACE = "surface"
ACTION_WAIT = "wait"
ACTION_BLOCKED = "blocked"
ACTIONS = (
    ACTION_NONE,
    ACTION_RECONNECT,
    ACTION_RECONSTITUTE,
    ACTION_SURFACE,
    ACTION_WAIT,
    ACTION_BLOCKED,
)

# Stable restart-attempt signature prefix. The same string is the key the
# durable ``incident_attempts`` counter uses, so the job's
# ``max_incident_attempts`` bound applies and the count survives
# ``opsx-plan reset``.
RESTART_SIGNATURE_PREFIX = "watchdog_restart:"


def restart_signature(plan_key: str) -> str:
    """Return the stable restart-attempt signature for *plan_key*."""
    return f"{RESTART_SIGNATURE_PREFIX}{plan_key}"


# ---------------------------------------------------------------------------
# Named errors
# ---------------------------------------------------------------------------


class WatchdogError(Exception):
    """Base class for watchdog failures."""


class BoundedRestartsExceeded(WatchdogError):
    """Automatic reconstitution is refused: the restart bound was reached."""


class WatchdogRefused(WatchdogError):
    """A pre-condition for reconstitution failed (for example, no quiescence)."""


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_seconds(earlier: Any, now: Any) -> float | None:
    """Return non-negative seconds from *earlier* to *now*, or ``None``."""
    start = _parse_iso(earlier)
    end = _parse_iso(now)
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def _add_seconds(value: Any, seconds: float) -> str | None:
    """Return *value* shifted forward by *seconds* as an ISO string."""
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    return (parsed + timedelta(seconds=float(seconds))).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class JobSignals:
    """The three independent signals plus the timestamps they were derived from.

    ``liveness``, ``progress``, and ``deadline`` are computed independently and
    are never inferred from one another. ``expected_human_wait`` is durable
    wait state, and ``quiesced`` is the verified-quiescence precondition for
    reconstitution (kernel-held flock free and no live process matching the
    recorded fencing identity). ``lock_free`` is the bare flock probe and
    ``owner_held`` reports whether the latest fencing record still claims the
    lock; together with ``liveness`` they distinguish a released record whose
    identity is still live from a healthy held owner.
    """

    job_id: int
    state: str
    liveness: bool
    progress: bool
    deadline: bool
    expected_human_wait: bool
    quiesced: bool
    lock_free: bool
    owner_held: bool
    last_progress_at: str | None
    progress_age_seconds: float | None
    progress_window_seconds: float
    stall_threshold_seconds: float
    execution_elapsed_minutes: float
    deadline_limit_minutes: float | None
    owner_pid: int | None
    owner_process_start: float | None
    owner_boot_id: str | None
    owner_label: str | None
    open_human_wait_id: int | None
    observed_at: str


def recorded_owner(ledger: Any, job_id: int) -> dict[str, Any] | None:
    """Return the job's latest recorded execution identity from its fencing records.

    The identity is the durable ``(pid, process_start, boot_id)`` triple of the
    latest fencing record, and it is preserved for every fencing event: a
    ``released`` or ``fenced`` record still names the prior owner even though it
    no longer holds the lock. ``held`` distinguishes a still-``acquired`` record
    from a released one; ``state`` keeps the shape
    :func:`lib.supervisor.lock.holder_is_live` requires (that predicate requires
    a held record). Liveness for reconstitution must instead use
    :func:`owner_identity_is_live`, which checks the recorded identity whether
    or not the record is still held.
    """
    records = ledger.list_fencing(int(job_id))
    if not records:
        return None
    last = records[-1]
    held = str(last["event"]) == "acquired"
    return {
        "state": lock_mod.HELD if held else lock_mod.RELEASED,
        "held": held,
        "event": str(last["event"]),
        "pid": last["pid"],
        "process_start": last["process_start"],
        "boot_id": last["boot_id"],
        "owner": last["owner"],
        "created_at": last["created_at"],
    }


def _latest_progress_at(ledger: Any, job: Any) -> str | None:
    """Return the maximum durable timestamp across the job's journal.

    Candidate timestamps span the job row, its actions and dispatches, its
    incidents, and its recorded evidence. Every candidate is considered; the
    maximum parseable value wins, so a job that has advanced anywhere in its
    durable journal has recent progress.
    """
    job_id = int(job["id"])
    candidates: list[Any] = [job["created_at"], job["updated_at"]]
    for action in ledger.list_actions(job_id):
        candidates.extend(
            [action["intent_at"], action["dispatched_at"], action["updated_at"]]
        )
        for evidence in ledger.list_evidence(int(action["id"])):
            candidates.append(evidence["recorded_at"])
    for incident in ledger.list_incidents(job_id):
        candidates.extend([incident["created_at"], incident["updated_at"]])
    latest: datetime | None = None
    latest_raw: str | None = None
    for candidate in candidates:
        parsed = _parse_iso(candidate)
        if parsed is None:
            continue
        if latest is None or parsed > latest:
            latest = parsed
            latest_raw = str(candidate)
    return latest_raw


def execution_elapsed_minutes(ledger: Any, job_id: int, *, now: Any = None) -> float:
    """Accumulate execution-elapsed minutes from the job's dispatch intervals.

    Human-wait duration is never a dispatch interval, so it is excluded by
    construction rather than subtracted afterwards.
    """
    end_now = _parse_iso(now) if now is not None else None
    if end_now is None:
        end_now = _parse_iso(clock_mod.utcnow())
    total = 0.0
    for interval in ledger.dispatch_intervals(int(job_id)):
        started = _parse_iso(interval["started_at"])
        if started is None:
            continue
        ended = _parse_iso(interval["ended_at"]) if interval["ended_at"] else end_now
        if ended is None:
            continue
        total += max(0.0, (ended - started).total_seconds() / 60.0)
    return total


def lock_is_free(worktree: Any) -> bool:
    """Return whether the kernel-held execution lock is currently free.

    A best-effort, read-only probe: it opens the existing lock file and tries a
    non-blocking exclusive flock, releasing it immediately. A missing lock file
    means nothing holds the lock.
    """
    if worktree is None:
        return True
    path = lock_mod.lock_file_path(worktree)
    if not path.exists():
        return True
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        return True
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - release is best effort
            pass
        return True
    finally:
        os.close(fd)


def job_worktree(job: Any) -> Path | None:
    """Return the job's absolute worktree path.

    The ledger stores the worktree relative to the repository root, so a
    relative value is resolved against ``repo_root`` rather than the current
    working directory.
    """
    raw = job["worktree_path"] if "worktree_path" in job.keys() else None
    if not raw:
        return None
    path = Path(str(raw))
    if not path.is_absolute():
        root = job["repo_root"] if "repo_root" in job.keys() else None
        if root:
            path = Path(str(root)) / path
    return path


def owner_identity_is_live(owner: Any) -> bool:
    """Return whether *owner*'s recorded identity matches a live process.

    Unlike :func:`lib.supervisor.lock.holder_is_live`, this does **not** require
    the fencing record to still be ``held``. A released or fenced record whose
    ``(pid, process_start, boot_id)`` triple still matches a live process on the
    current boot is still a live prior owner, so reconstitution must refuse and
    surface it rather than replace it. When either identity discriminator is
    unavailable, identity liveness cannot be established and this returns
    ``False`` so only the kernel-held flock arbitrates.
    """
    if not isinstance(owner, Mapping):
        return False
    pid = owner.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    recorded_boot = owner.get("boot_id")
    current_boot = lock_mod.boot_identity()
    if recorded_boot is None or current_boot is None:
        return False
    if recorded_boot != current_boot:
        # A record from a different boot is always stale.
        return False
    start = owner.get("process_start")
    if start is None:
        return False
    observed = lock_mod.process_start_time(pid)
    if observed is None:
        return False
    try:
        return float(observed) == float(start)
    except (TypeError, ValueError):
        return False


def verify_quiescence(worktree: Any, owner: Any) -> bool:
    """Return whether the prior worker is verified quiesced.

    Verified quiescence requires *both* that the kernel-held flock is free and
    that no live process matches the recorded fencing identity. A free flock
    alone is not sufficient, and neither is a released fencing record: the prior
    owner may have released or inherited away its descriptor while its process
    is still alive, so a still-live recorded identity is never safe to replace.
    """
    return lock_is_free(worktree) and not owner_identity_is_live(owner)


def derive_signals(
    ledger: Any,
    job: Any,
    *,
    now: Any = None,
    progress_window_seconds: float = PROGRESS_WINDOW_SECONDS,
    stall_threshold_seconds: float = STALL_THRESHOLD_SECONDS,
) -> JobSignals:
    """Derive the three independent signals for *job* from durable state."""
    job_id = int(job["id"])
    observed_at = str(now) if now is not None else clock_mod.utcnow()
    owner = recorded_owner(ledger, job_id)
    liveness = bool(owner) and owner_identity_is_live(owner)

    waits = ledger.open_waits(job_id, kind="human")
    expected_human_wait = bool(waits)
    open_human_wait_id = int(waits[-1]["id"]) if waits else None

    last_progress_at = _latest_progress_at(ledger, job)
    progress_age = _age_seconds(last_progress_at, observed_at)
    progress = progress_age is not None and progress_age <= progress_window_seconds

    elapsed = execution_elapsed_minutes(ledger, job_id, now=observed_at)
    policy = ledger.current_policy(job_id)
    deadlines = policy["deadlines"]
    limit = deadlines.get("execution_deadline_minutes")
    try:
        budgets_mod.check_execution_deadline(
            deadlines, execution_elapsed_minutes=elapsed
        )
        deadline = False
    except budgets_mod.BudgetExhaustedError:
        deadline = True

    worktree = job_worktree(job)
    lock_free = lock_is_free(worktree)
    quiesced = lock_free and not liveness
    owner_held = bool(owner.get("held")) if owner else False

    return JobSignals(
        job_id=job_id,
        state=str(job["state"]),
        liveness=liveness,
        progress=progress,
        deadline=deadline,
        expected_human_wait=expected_human_wait,
        quiesced=quiesced,
        lock_free=lock_free,
        owner_held=owner_held,
        last_progress_at=last_progress_at,
        progress_age_seconds=progress_age,
        progress_window_seconds=float(progress_window_seconds),
        stall_threshold_seconds=float(stall_threshold_seconds),
        execution_elapsed_minutes=elapsed,
        deadline_limit_minutes=limit,
        owner_pid=owner.get("pid") if owner else None,
        owner_process_start=owner.get("process_start") if owner else None,
        owner_boot_id=owner.get("boot_id") if owner else None,
        owner_label=owner.get("owner") if owner else None,
        open_human_wait_id=open_human_wait_id,
        observed_at=observed_at,
    )


# ---------------------------------------------------------------------------
# Classification (pure)
# ---------------------------------------------------------------------------


def classify(signals: JobSignals) -> str:
    """Classify *signals* into exactly one watchdog class.

    Precedence: an open human wait wins outright; a live owner is ``live`` when
    progress is fresh, ``quiet`` short of the stall threshold, and ``stalled``
    beyond it or once the execution deadline is reached; an owner that is not
    live is only ``dead`` when it is verified quiesced.
    """
    if signals.expected_human_wait:
        return CLASS_EXPECTED_HUMAN_WAIT
    if signals.liveness:
        if signals.progress:
            return CLASS_LIVE
        if signals.deadline or _beyond_stall(signals):
            return CLASS_STALLED
        return CLASS_QUIET
    if not signals.quiesced:
        return CLASS_STALLED
    if signals.deadline:
        return CLASS_STALLED
    return CLASS_DEAD


def _beyond_stall(signals: JobSignals) -> bool:
    age = signals.progress_age_seconds
    if age is None:
        return False
    return age >= signals.stall_threshold_seconds


# ---------------------------------------------------------------------------
# Read-only observation
# ---------------------------------------------------------------------------


def _signals_payload(signals: JobSignals) -> dict[str, Any]:
    return {
        "liveness": signals.liveness,
        "progress": signals.progress,
        "deadline": signals.deadline,
        "progress_age_seconds": signals.progress_age_seconds,
        "last_progress_at": signals.last_progress_at,
        "execution_elapsed_minutes": signals.execution_elapsed_minutes,
        "deadline_limit_minutes": signals.deadline_limit_minutes,
        "quiesced": signals.quiesced,
        "lock_free": signals.lock_free,
        "owner_held": signals.owner_held,
        "owner_pid": signals.owner_pid,
        "owner_process_start": signals.owner_process_start,
        "owner_boot_id": signals.owner_boot_id,
        "owner_label": signals.owner_label,
        "progress_window_seconds": signals.progress_window_seconds,
        "stall_threshold_seconds": signals.stall_threshold_seconds,
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class JobAssessment:
    """One job's watchdog assessment for a tick (no decision is implied)."""

    job_id: int
    classification: str
    action: str
    reason: str
    blocked: bool
    signals: JobSignals
    restart_attempts: int
    restart_limit: int | None
    next_allowed_at: str | None
    prior_owner: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "classification": self.classification,
            "action": self.action,
            "reason": self.reason,
            "blocked": self.blocked,
            "signals": _signals_payload(self.signals),
            "restart_attempts": self.restart_attempts,
            "restart_limit": self.restart_limit,
            "next_allowed_at": self.next_allowed_at,
            "prior_owner": self.prior_owner,
        }


class Watchdog:
    """The deterministic watchdog over a job projection.

    ``reconnect(ledger, job_id) -> bool`` and ``redrive(ledger, job_id)`` are
    injected so the package stays client-neutral; the orchestrator supplies its
    session-bridge and engine adapters in production.
    """

    def __init__(
        self,
        ledger: Any,
        *,
        repo: Any = None,
        reconnect: Callable[[Any, int], Any] | None = None,
        redrive: Callable[[Any, int], Any] | None = None,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], str] | None = None,
        progress_window_seconds: float = PROGRESS_WINDOW_SECONDS,
        stall_threshold_seconds: float = STALL_THRESHOLD_SECONDS,
    ) -> None:
        self.ledger = ledger
        self.repo = Path(str(repo)) if repo is not None else None
        self._reconnect = reconnect
        self._redrive = redrive
        self._sleep = sleep or time.sleep
        self._now = now or clock_mod.utcnow
        self.progress_window_seconds = float(progress_window_seconds)
        self.stall_threshold_seconds = float(stall_threshold_seconds)

    # -- public entry points ----------------------------------------------

    def signals_for(self, job: Any, *, now: Any = None) -> JobSignals:
        return derive_signals(
            self.ledger,
            job,
            now=now,
            progress_window_seconds=self.progress_window_seconds,
            stall_threshold_seconds=self.stall_threshold_seconds,
        )

    def assess(self, job: Any, *, boot: bool = False, record: bool = True) -> JobAssessment:
        """Evaluate one job and (when *record*) append its durable events."""
        signals = self.signals_for(job)
        classification = classify(signals)
        if record and (boot or self._classification_changed(signals.job_id, classification)):
            self._append(
                signals.job_id,
                EVENT_CLASSIFICATION,
                classification,
                f"classified {classification}",
                detail=json.dumps(
                    {"live": signals.liveness, "progress": signals.progress,
                     "deadline": signals.deadline,
                     "human_wait": signals.expected_human_wait},
                    sort_keys=True,
                ),
            )
        decision = self._decide(job, signals, classification, boot=boot, record=record)
        return JobAssessment(
            job_id=signals.job_id,
            classification=classification,
            action=decision["action"],
            reason=decision["reason"],
            blocked=decision["blocked"],
            signals=signals,
            restart_attempts=decision["restart_attempts"],
            restart_limit=decision["restart_limit"],
            next_allowed_at=decision["next_allowed_at"],
            prior_owner=decision["prior_owner"],
        )

    def tick(self, *, boot: bool = False) -> list[JobAssessment]:
        """Evaluate every registered non-terminal job once."""
        assessments: list[JobAssessment] = []
        for job in self.ledger.list_jobs():
            if str(job["state"]) in ledger_mod.TERMINAL_JOB_STATES:
                # A terminal job is untouched: no classification action,
                # reconnect, or reconstitution.
                continue
            assessments.append(self.assess(job, boot=boot))
        return assessments

    def boot_scan(self) -> list[JobAssessment]:
        """Reconcile every non-terminal job on service start.

        Boot reconciliation records each job's classification and attempts
        reconnect before any respawn. Terminal jobs are left untouched.
        """
        return self.tick(boot=True)

    def run(self, *, interval: float = 5.0, once: bool = False,
            sleep: Callable[[float], None] | None = None) -> list[JobAssessment]:
        """Run the deterministic loop (or exactly one tick with *once*)."""
        sleeper = sleep or self._sleep
        results = self.tick()
        if once:
            return results
        while True:
            sleeper(float(interval))
            results = self.tick()
        return results  # pragma: no cover - unreachable without stop injection

    # -- read-only report --------------------------------------------------

    def report(self, job: Any) -> dict[str, Any]:
        """Build the read-only watchdog observation state for *job*.

        Computes the classification and signals and reads recent events without
        mutating the ledger, acquiring the execution lock, or requiring a live
        service.
        """
        signals = self.signals_for(job)
        classification = classify(signals)
        decision = self._decide(
            job, signals, classification, boot=False, record=False, perform=False
        )
        events = [
            {
                "id": int(row["id"]),
                "kind": str(row["kind"]),
                "classification": row["classification"],
                "reason": row["reason"],
                "detail": row["detail"],
                "created_at": row["created_at"],
            }
            for row in self.ledger.list_watchdog_events(
                signals.job_id, limit=RECENT_EVENT_LIMIT
            )
        ]
        return {
            "classification": classification,
            "signals": _signals_payload(signals),
            "action": decision["action"],
            "reason": decision["reason"],
            "blocked": decision["blocked"],
            "restart": {
                "signature": restart_signature(
                    job_plan_key(self.ledger, job)
                ),
                "attempts": decision["restart_attempts"],
                "limit": decision["restart_limit"],
                "next_allowed_at": decision["next_allowed_at"],
            },
            "prior_owner": decision["prior_owner"],
            "recent_events": events,
        }

    # -- decision order ----------------------------------------------------

    def _decide(
        self,
        job: Any,
        signals: JobSignals,
        classification: str,
        *,
        boot: bool,
        record: bool,
        perform: bool = True,
    ) -> dict[str, Any]:
        """Apply the fixed decision order and return a decision record."""
        job_id = signals.job_id
        policy = self.ledger.current_policy(job_id)
        signature = restart_signature(job_plan_key(self.ledger, job))
        attempts = self.ledger.incident_attempt_count(job_id, signature)
        limit = budgets_mod.max_incident_attempts(policy)

        base: dict[str, Any] = {
            "action": ACTION_NONE,
            "reason": "",
            "blocked": False,
            "restart_attempts": attempts,
            "restart_limit": limit,
            "next_allowed_at": None,
            "prior_owner": signals.owner_label,
        }

        # 1. A terminal job is a no-op (defensive; tick filters these out).
        if signals.state in ledger_mod.TERMINAL_JOB_STATES:
            return {**base, "reason": "terminal job"}

        # 2. An open human wait is normal durable state: take no action at all.
        if signals.expected_human_wait:
            return {
                **base,
                "reason": (
                    f"expected human wait {signals.open_human_wait_id} is open; "
                    "no model, dispatch, recovery, or reconstitution action"
                ),
            }

        # 3. An unreconciled uncertain action blocks every reconstitution.
        uncertain = self.ledger.list_uncertain_actions(job_id)
        if uncertain:
            reason = (
                f"{len(uncertain)} unreconciled uncertain action(s) block "
                "reconstitution until evidence reconciles them"
            )
            if record:
                self._append_once(
                    job_id, EVENT_BLOCKED, classification, reason,
                    detail=json.dumps(
                        {"action_ids": [int(row["id"]) for row in uncertain]},
                        sort_keys=True,
                    ),
                )
            return {**base, "action": ACTION_BLOCKED, "reason": reason, "blocked": True}

        # 4. Reconnect and adopt a recorded live session before any respawn.
        if self._should_attempt_reconnect(signals, classification, boot=boot):
            if not perform:
                return {
                    **base,
                    "action": ACTION_RECONNECT,
                    "reason": (
                        "a recorded session would be reconnected and adopted "
                        "before any respawn"
                    ),
                }
            if self._attempt_reconnect(job_id, classification, record=record):
                return {
                    **base,
                    "action": ACTION_RECONNECT,
                    "reason": "adopted the job's recorded live session",
                }

        # 5. A live owner is surfaced, never interrupted. A released fencing
        #    record whose recorded identity is still live is a blocking hazard:
        #    a free lock alone must never make it safe to replace.
        if signals.liveness:
            label = signals.owner_label or "unknown"
            if not signals.owner_held:
                reason = (
                    f"prior owner {label} no longer holds the lock but its "
                    "recorded identity still matches a live process on this "
                    "boot; reconstitution is refused and the live prior owner "
                    "is surfaced"
                )
                if record:
                    self._append_once(
                        job_id, EVENT_BLOCKED, classification, reason
                    )
                return {
                    **base,
                    "action": ACTION_SURFACE,
                    "reason": reason,
                    "blocked": True,
                }
            reason = f"owner {label} is live (classification {classification})"
            if record and classification in (CLASS_STALLED, CLASS_QUIET):
                self._append_once(job_id, EVENT_SURFACED, classification, reason)
            return {**base, "action": ACTION_SURFACE, "reason": reason}

        # 6. Only a dead, quiesced job that already had a prior execution is a
        #    reconstitution candidate.
        if classification == CLASS_DEAD and self._had_prior_execution(job_id):
            if not perform:
                return self._preview_reconstitution(
                    job, signals, classification, base
                )
            try:
                outcome = self._reconstitute(job, signals, classification)
            except BoundedRestartsExceeded as exc:
                reason = str(exc)
                if record:
                    self._append_once(job_id, EVENT_BOUNDED, classification, reason)
                return {
                    **base,
                    "action": ACTION_BLOCKED,
                    "reason": reason,
                    "blocked": True,
                }
            except budgets_mod.BoundedAttemptsExceededError as exc:
                reason = str(exc)
                if record:
                    self._append_once(job_id, EVENT_BOUNDED, classification, reason)
                return {
                    **base,
                    "action": ACTION_BLOCKED,
                    "reason": reason,
                    "blocked": True,
                }
            except WatchdogRefused as exc:
                reason = str(exc)
                if record:
                    self._append_once(job_id, EVENT_BLOCKED, classification, reason)
                return {
                    **base,
                    "action": ACTION_BLOCKED,
                    "reason": reason,
                    "blocked": True,
                }
            return {**base, **outcome}

        # 7. Anything else (not live, not yet verified dead) is surfaced.
        reason = f"no action; classification {classification}"
        if record and classification == CLASS_STALLED:
            self._append_once(job_id, EVENT_SURFACED, classification, reason)
        return {**base, "action": ACTION_SURFACE, "reason": reason}

    # -- reconnect ---------------------------------------------------------

    def _should_attempt_reconnect(
        self, signals: JobSignals, classification: str, *, boot: bool
    ) -> bool:
        if self._reconnect is None:
            return False
        linkage = session_bridge_mod.primary_session_linkage(
            self.ledger, signals.job_id
        )
        if linkage is None:
            return False
        if boot:
            return True
        return classification in (CLASS_DEAD, CLASS_STALLED)

    def _attempt_reconnect(
        self, job_id: int, classification: str, *, record: bool
    ) -> bool:
        assert self._reconnect is not None
        try:
            adopted = bool(self._reconnect(self.ledger, int(job_id)))
        except Exception as exc:  # noqa: BLE001 - a failed probe is not fatal
            if record:
                self._append_once(
                    job_id, EVENT_BLOCKED, classification,
                    f"session reconnect probe failed: {exc}",
                )
            return False
        if adopted and record:
            self._append_once(
                job_id, EVENT_RECONNECTED, classification,
                "reconnected to and adopted the recorded live session",
            )
        return adopted

    # -- reconstitution ----------------------------------------------------

    def _had_prior_execution(self, job_id: int) -> bool:
        return bool(self.ledger.list_fencing(int(job_id)))

    def _preview_reconstitution(
        self, job: Any, signals: JobSignals, classification: str,
        base: dict[str, Any],
    ) -> dict[str, Any]:
        """Compute the reconstitution decision without performing any action.

        Used for read-only observation: it reads the durable attempt count and
        the last reconstitution event and reports the bound, the backoff wait,
        or the intended reconstitution without recording an attempt or calling
        the re-drive adapter.
        """
        job_id = signals.job_id
        policy = self.ledger.current_policy(job_id)
        signature = restart_signature(job_plan_key(self.ledger, job))
        limit = budgets_mod.max_incident_attempts(policy)
        count = self.ledger.incident_attempt_count(job_id, signature)
        if limit is not None and count >= limit:
            return {
                **base,
                "action": ACTION_BLOCKED,
                "reason": (
                    f"restart attempts for {signature} reached "
                    f"max_incident_attempts ({limit}); further automatic "
                    "reconstitution is refused and an operator revision is "
                    "required"
                ),
                "blocked": True,
            }
        next_allowed_at: str | None = None
        last = self.ledger.latest_watchdog_event(job_id, kind=EVENT_RECONSTITUTED)
        if last is not None:
            delay = budgets_mod.backoff_delay(count + 1)
            next_allowed_at = _add_seconds(str(last["created_at"]), delay)
            now_dt = _parse_iso(self._now())
            next_dt = _parse_iso(next_allowed_at)
            if now_dt is not None and next_dt is not None and now_dt < next_dt:
                return {
                    **base,
                    "action": ACTION_WAIT,
                    "reason": (
                        f"bounded backoff: next reconstitution attempt is not "
                        f"allowed before {next_allowed_at}"
                    ),
                    "next_allowed_at": next_allowed_at,
                }
        return {
            **base,
            "action": ACTION_RECONSTITUTE,
            "reason": "a dead, quiesced job would be reconstituted",
            "next_allowed_at": next_allowed_at,
        }

    def _reconstitute(
        self, job: Any, signals: JobSignals, classification: str
    ) -> dict[str, Any]:
        job_id = signals.job_id
        policy = self.ledger.current_policy(job_id)
        signature = restart_signature(job_plan_key(self.ledger, job))
        limit = budgets_mod.max_incident_attempts(policy)
        count = self.ledger.incident_attempt_count(job_id, signature)

        if limit is not None and count >= limit:
            raise BoundedRestartsExceeded(
                f"restart attempts for {signature} reached "
                f"max_incident_attempts ({limit}); further automatic "
                "reconstitution is refused and an operator revision is required"
            )

        now = self._now()
        next_allowed_at: str | None = None
        last = self.ledger.latest_watchdog_event(job_id, kind=EVENT_RECONSTITUTED)
        if last is not None:
            delay = budgets_mod.backoff_delay(count + 1)
            last_at = str(last["created_at"])
            next_allowed_at = _add_seconds(last_at, delay)
            now_dt = _parse_iso(now)
            next_dt = _parse_iso(next_allowed_at)
            if now_dt is not None and next_dt is not None and now_dt < next_dt:
                return {
                    "action": ACTION_WAIT,
                    "reason": (
                        f"bounded backoff: next reconstitution attempt is not "
                        f"allowed before {next_allowed_at}"
                    ),
                    "next_allowed_at": next_allowed_at,
                }

        owner = recorded_owner(self.ledger, job_id)
        worktree = job_worktree(job)
        if not verify_quiescence(worktree, owner):
            raise WatchdogRefused(
                "refusing to reconstitute: the prior owner is not verified "
                "quiesced (a live process still matches the recorded fencing "
                "identity or the kernel-held lock is still held)"
            )

        new_count = budgets_mod.record_incident_attempt(
            self.ledger, job_id=job_id, signature=signature, policy=policy
        )
        delay = budgets_mod.backoff_delay(new_count)
        next_allowed_at = _add_seconds(now, delay)
        prior_owner = str(owner.get("owner")) if owner else None
        detail = json.dumps(
            {
                "prior_owner": prior_owner,
                "prior_pid": owner.get("pid") if owner else None,
                "prior_process_start": owner.get("process_start") if owner else None,
                "prior_boot_id": owner.get("boot_id") if owner else None,
                "next_allowed_at": next_allowed_at,
            },
            sort_keys=True,
        )
        self._append(
            job_id,
            EVENT_RECONSTITUTED,
            classification,
            "reconstituting a dead, quiesced job after the bounded backoff",
            detail=detail,
        )
        if self._redrive is not None:
            try:
                self._redrive(self.ledger, job_id)
            except Exception as exc:  # noqa: BLE001 - surfaced as blocking state
                reason = f"reconstitution drive failed: {exc}"
                self._append_once(
                    job_id, EVENT_BLOCKED, classification, reason
                )
                return {
                    "action": ACTION_BLOCKED,
                    "reason": reason,
                    "blocked": True,
                    "next_allowed_at": next_allowed_at,
                }
        return {
            "action": ACTION_RECONSTITUTE,
            "reason": (
                f"reconstituted attempt {new_count}"
                + (f" replacing prior owner {prior_owner}" if prior_owner else "")
            ),
            "restart_attempts": new_count,
            "next_allowed_at": next_allowed_at,
        }

    # -- event persistence -------------------------------------------------

    def _classification_changed(self, job_id: int, classification: str) -> bool:
        last = self.ledger.latest_watchdog_event(job_id, kind=EVENT_CLASSIFICATION)
        return last is None or str(last["classification"]) != classification

    def _append(
        self,
        job_id: int,
        kind: str,
        classification: str | None,
        reason: str,
        *,
        detail: str | None = None,
    ) -> int:
        return int(
            self.ledger.record_watchdog_event(
                int(job_id),
                kind=kind,
                classification=classification,
                reason=reason,
                detail=detail,
            )
        )

    def _append_once(
        self,
        job_id: int,
        kind: str,
        classification: str | None,
        reason: str,
        *,
        detail: str | None = None,
    ) -> int | None:
        """Append *kind* only when it is not already the newest such event."""
        last = self.ledger.latest_watchdog_event(int(job_id), kind=kind)
        if (
            last is not None
            and str(last["reason"]) == str(reason)
            and last["classification"] == classification
        ):
            return None
        return self._append(
            job_id, kind, classification, reason, detail=detail
        )


def job_plan_key(ledger: Any, job: Any) -> str:
    """Return the stable plan key used in the restart attempt signature."""
    try:
        snapshot = ledger.current_manifest_snapshot(int(job["id"]))
        if snapshot:
            parsed = broker_mod.parse_snapshot(snapshot)
            plan = parsed.get("plan") if isinstance(parsed, Mapping) else None
            name = plan.get("name") if isinstance(plan, Mapping) else None
            if name:
                return str(name)
    except Exception:  # noqa: BLE001 - a missing snapshot is durable-state absence
        pass
    return str(job["run_id"])


__all__ = [
    "ACTIONS",
    "ACTION_BLOCKED",
    "ACTION_NONE",
    "ACTION_RECONNECT",
    "ACTION_RECONSTITUTE",
    "ACTION_SURFACE",
    "ACTION_WAIT",
    "BoundedRestartsExceeded",
    "CLASSIFICATIONS",
    "CLASS_DEAD",
    "CLASS_EXPECTED_HUMAN_WAIT",
    "CLASS_LIVE",
    "CLASS_QUIET",
    "CLASS_STALLED",
    "EVENT_BLOCKED",
    "EVENT_BOUNDED",
    "EVENT_CLASSIFICATION",
    "EVENT_KINDS",
    "EVENT_RECONNECTED",
    "EVENT_RECONSTITUTED",
    "EVENT_SURFACED",
    "JobAssessment",
    "JobSignals",
    "PROGRESS_WINDOW_SECONDS",
    "RECENT_EVENT_LIMIT",
    "RESTART_SIGNATURE_PREFIX",
    "STALL_THRESHOLD_SECONDS",
    "Watchdog",
    "WatchdogError",
    "WatchdogRefused",
    "classify",
    "derive_signals",
    "execution_elapsed_minutes",
    "job_plan_key",
    "job_worktree",
    "lock_is_free",
    "owner_identity_is_live",
    "recorded_owner",
    "restart_signature",
    "verify_quiescence",
]
