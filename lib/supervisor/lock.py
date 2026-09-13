"""Worktree execution lock: kernel-arbitrated mutual exclusion for mutating runs.

This module owns the *ephemeral* worktree execution lock, which is distinct
from a supervised job's permanent ledger ownership. It is stdlib-only and
importable without side effects: importing it neither parses arguments, spawns
a process, nor touches ``.opsx-plan/``.

Design:

- ``.opsx-plan/execution.lock`` is a dedicated, never-renamed inode that the
  holder ``fcntl.flock(LOCK_EX | LOCK_NB)``s for the command's lifetime. The
  kernel releases it when the holder dies, so a free flock is the necessary
  arbitration for a new acquirer, but its release alone is not proof of
  quiescence while a matching live process identity remains.
- ``.opsx-plan/execution-lock.json`` is the fencing record. It is written with
  atomic temp-file-then-rename and carries the owner label, owner kind, the
  process identity (pid, process start time, boot identity), host, and a
  ``state`` that is ``held`` while the owner holds the lock and rewritten to
  ``released`` (identity preserved) before the flock is released.
- A bare PID is never proof of liveness or ownership. A record from a different
  boot is always stale. On the current boot a holder is *live* while a live
  process matches the recorded identity (pid, process start time, and boot
  identity) — whether or not the flock is still held — so take-over of any live
  recorded identity is refused even after the flock has been released. When
  either identity discriminator is unavailable, liveness cannot be established
  and the free kernel lock alone arbitrates.

Fencing persistence is an optional, explicitly supplied ledger handle. This
module never constructs or opens a ``Ledger``: an ordinary, unsupervised run
acquires the lock with files only and carries no backend dependency.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from lib.supervisor import ledger as ledger_module

OPSX_PLAN_DIR = ".opsx-plan"
EXECUTION_LOCK_NAME = "execution.lock"
EXECUTION_RECORD_NAME = "execution-lock.json"
RECORD_VERSION = 1

HELD = "held"
RELEASED = "released"
OWNER_KINDS = ("ordinary", "supervised")

_IDENTITY_FIELDS = ("pid", "process_start", "boot_id")


class LockError(Exception):
    """Base class for worktree-execution-lock failures."""


class LockContentionError(LockError):
    """The worktree execution lock is already held by another process."""


class SupervisedOwnershipError(LockError):
    """The worktree lock is held by (or reserved for) a supervised execution."""


class LockReleaseError(LockError):
    """Releasing the lock failed to persist its released state or event.

    The kernel-held flock is still released and the descriptor closed before
    this is raised, so exclusion is not leaked; the failure is surfaced so a
    caller never treats a non-durable release as success.
    """


# ---------------------------------------------------------------------------
# Process identity
# ---------------------------------------------------------------------------


def boot_identity() -> str | None:
    """Return the host boot identity, or ``None`` where the platform lacks it."""
    try:
        text = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8")
    except OSError:
        return None
    text = text.strip()
    return text or None


def process_start_time(pid: int) -> float | None:
    """Return ``/proc/<pid>/stat`` field 22 (start time), or ``None``.

    The ``comm`` field (2) may itself contain spaces and parentheses, so the
    parse resumes after the *last* ``)``; the first whitespace field there is
    field 3, making start time (field 22) index 19.
    """
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    end = raw.rfind(")")
    if end == -1:
        return None
    fields = raw[end + 2:].split()
    if len(fields) <= 19:
        return None
    try:
        return float(fields[19])
    except ValueError:
        return None


def _host_name() -> str | None:
    try:
        return socket.gethostname() or None
    except OSError:
        return None


def current_identity() -> dict[str, Any]:
    """Capture the calling process's identity record fields."""
    pid = os.getpid()
    return {
        "pid": pid,
        "process_start": process_start_time(pid),
        "boot_id": boot_identity(),
        "host": _host_name(),
    }


def record_identity(record: Any) -> tuple[Any, Any, Any]:
    """Return the ``(pid, process_start, boot_id)`` identity of *record*."""
    if not isinstance(record, dict):
        return (None, None, None)
    return tuple(record.get(field) for field in _IDENTITY_FIELDS)


def holder_is_live(record: Any) -> bool:
    """Return whether *record* names a live holder on the current boot.

    Liveness requires a held record whose recorded boot identity matches the
    current boot *and* whose recorded process start time matches a live process.
    A bare PID is never sufficient. When either identity discriminator is
    unavailable (the recorded start time, this platform's boot identity, or the
    observed process start time), identity liveness cannot be established, so
    this predicate returns ``False`` and the kernel-held flock is the only
    arbitration.
    """
    if not isinstance(record, dict):
        return False
    if record.get("state") != HELD:
        return False
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    recorded_boot = record.get("boot_id")
    current_boot = boot_identity()
    if recorded_boot is None or current_boot is None:
        # Without both boot identities a matching PID/start time cannot be
        # attributed to this boot, so identity liveness is unknown: fall back
        # to the kernel-held flock.
        return False
    if recorded_boot != current_boot:
        # A record from a different boot is always stale.
        return False
    start = record.get("process_start")
    if start is None:
        return False
    observed = process_start_time(pid)
    if observed is None:
        return False
    try:
        return float(observed) == float(start)
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def lock_file_path(worktree: os.PathLike[str] | str) -> Path:
    return Path(worktree) / OPSX_PLAN_DIR / EXECUTION_LOCK_NAME


def record_path(worktree: os.PathLike[str] | str) -> Path:
    return Path(worktree) / OPSX_PLAN_DIR / EXECUTION_RECORD_NAME


def read_record(worktree: os.PathLike[str] | str) -> dict[str, Any] | None:
    """Read the fencing record, tolerating a missing or unparseable file.

    A missing or unparseable record is treated as an unknown ordinary owner;
    exclusion still holds because it is the kernel-held flock that arbitrates.
    """
    path = record_path(worktree)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _write_record(worktree: os.PathLike[str] | str, record: dict[str, Any]) -> None:
    path = record_path(worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Fencing persistence
# ---------------------------------------------------------------------------


def _persist_fencing(
    ledger: Any, job_id: int | None, event: str, owner: str, record: dict[str, Any]
) -> None:
    """Journal one fencing event through an explicitly supplied ledger.

    No ledger is constructed here; when *ledger* or *job_id* is absent the
    event is simply not persisted (the ordinary, backend-free path).
    """
    if ledger is None or job_id is None:
        return
    if event not in ledger_module.FENCING_EVENTS:
        raise LockError(f"unknown fencing event: {event}")
    ledger.record_fencing(
        job_id,
        event=event,
        owner=owner,
        pid=record.get("pid"),
        process_start=record.get("process_start"),
        boot_id=record.get("boot_id"),
        host=record.get("host"),
    )


# ---------------------------------------------------------------------------
# Handle and acquisition
# ---------------------------------------------------------------------------


class LockHandle:
    """A held worktree execution lock.

    Released explicitly or automatically when the enclosing context exits.
    Release rewrites the fencing record to ``released`` (preserving identity)
    before the kernel-held flock is released, then journals the ``released``
    event.
    """

    def __init__(
        self,
        *,
        worktree: Path,
        owner: str,
        owner_kind: str,
        job_id: int | None,
        record: dict[str, Any],
        ledger: Any,
        fd: int,
    ) -> None:
        self.worktree = worktree
        self.owner = owner
        self.owner_kind = owner_kind
        self.job_id = job_id
        self.record = record
        self.ledger = ledger
        self.fd = fd
        self._released = False

    @property
    def identity(self) -> tuple[Any, Any, Any]:
        return record_identity(self.record)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        record = dict(self.record)
        record["state"] = RELEASED
        record["released_at"] = _utcnow()

        failures: list[str] = []
        try:
            try:
                _write_record(self.worktree, record)
            except Exception as exc:  # noqa: BLE001 - surfaced below
                failures.append(f"fencing record write failed: {exc}")
            try:
                _persist_fencing(
                    self.ledger, self.job_id, "released", self.owner, record
                )
            except Exception as exc:  # noqa: BLE001 - surfaced below
                failures.append(f"ledger release event failed: {exc}")
        finally:
            # Always preserve cleanup: the kernel-held lock and descriptor are
            # released even when persistence failed, so exclusion is never
            # leaked. The failure is reported after cleanup, below.
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass
        if failures:
            raise LockReleaseError(
                f"lock release for {self.worktree} was not durable: "
                + "; ".join(failures)
            )


def _contention_error(worktree: Path, prior: Any) -> LockError:
    if isinstance(prior, dict) and prior.get("owner_kind") == "supervised":
        return SupervisedOwnershipError(
            f"worktree {worktree} is owned by a supervised execution "
            f"({prior.get('owner')!r}, job {prior.get('job_id')}); refusing "
            "to proceed"
        )
    holder = prior.get("owner") if isinstance(prior, dict) else None
    detail = f" by {holder!r}" if holder else ""
    return LockContentionError(
        f"worktree {worktree} execution lock is already held{detail}; refusing "
        "to proceed"
    )


@contextmanager
def acquire(
    worktree: os.PathLike[str] | str,
    *,
    owner: str,
    owner_kind: str = "ordinary",
    job_id: int | None = None,
    ledger: Any = None,
) -> Iterator[LockHandle]:
    """Acquire the worktree execution lock as a context manager.

    Fail-fast: contention raises :class:`SupervisedOwnershipError` when the
    recorded holder is supervised and :class:`LockContentionError` otherwise.
    Acquisition requires a free kernel-held flock and a quiesced prior
    identity; a differing prior identity whose record is still ``held`` and
    still live is refused with a named error even when the flock is free, and
    only a non-live held record is journaled as ``fenced`` before the new
    ``held`` record is written.
    """
    worktree_path = Path(worktree)
    opsx_dir = worktree_path / OPSX_PLAN_DIR
    opsx_dir.mkdir(parents=True, exist_ok=True)
    identity = current_identity()

    fd = os.open(
        str(opsx_dir / EXECUTION_LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o644
    )
    handle: LockHandle | None = None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise _contention_error(worktree_path, read_record(worktree_path)) from exc

        prior = read_record(worktree_path)

        # Single-supervised-job invariant at the execution layer: refuse a
        # supervised acquisition that finds a *live* supervised record for a
        # different job id, regardless of registration state.
        if (
            owner_kind == "supervised"
            and job_id is not None
            and isinstance(prior, dict)
            and prior.get("owner_kind") == "supervised"
            and prior.get("job_id") not in (None, job_id)
            and holder_is_live(prior)
        ):
            raise SupervisedOwnershipError(
                f"worktree {worktree_path} is owned by a supervised execution "
                f"({prior.get('owner')!r}, job {prior.get('job_id')}); refusing "
                f"to start a second supervised execution (job {job_id})"
            )

        takeover = (
            isinstance(prior, dict)
            and prior.get("state") == HELD
            and record_identity(prior) != record_identity(identity)
        )
        if takeover:
            # A free kernel-held flock is necessary but not sufficient proof of
            # quiescence: the prior owner may merely have released or inherited
            # away its descriptor while its process is still alive. Refuse for
            # every owner kind until the recorded identity is also quiesced.
            if holder_is_live(prior):
                raise _contention_error(worktree_path, prior)
            _persist_fencing(
                ledger, job_id, "fenced", str(prior.get("owner", "unknown")), prior
            )

        record: dict[str, Any] = {
            "version": RECORD_VERSION,
            "state": HELD,
            "owner": owner,
            "owner_kind": owner_kind,
            "job_id": job_id,
            "pid": identity["pid"],
            "process_start": identity["process_start"],
            "boot_id": identity["boot_id"],
            "host": identity["host"],
            "acquired_at": _utcnow(),
            "released_at": None,
        }
        _write_record(worktree_path, record)
        handle = LockHandle(
            worktree=worktree_path,
            owner=owner,
            owner_kind=owner_kind,
            job_id=job_id,
            record=record,
            ledger=ledger,
            fd=fd,
        )
        _persist_fencing(ledger, job_id, "acquired", owner, record)
        yield handle
    finally:
        if handle is not None:
            handle.release()
        else:
            try:
                os.close(fd)
            except OSError:
                pass
