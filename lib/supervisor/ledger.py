"""Durable supervisor ledger: schema-versioned SQLite storage for supervision.

This module is the storage contract every later supervision change builds on.
It records supervised jobs, actions, incidents, journal evidence, dispatch
records, and one protected, explicitly-revised job-policy record per job.

Design rules enforced here:

- Standard library only (``sqlite3``), no runtime-package imports.
- A single SQLite file, WAL journal mode, foreign keys on, and an explicit
  schema version stored in ``PRAGMA user_version``.
- Forward-only migrations: an older ledger migrates in one transaction; a
  ledger newer than the code raises :class:`LedgerVersionError` without being
  modified.
- The ledger path is trusted only when it resolves outside any repository
  worktree (no ``.git`` ancestor) and outside any ``.opsx-plan/`` directory.
- Journal-before-side-effects: an action intent is committed in its own
  transaction before any side effect; an outcome that cannot be confirmed is
  marked ``uncertain`` and reconciled from recorded evidence before it can be
  completed or replayed. No exactly-once claims are made: replay is expected
  to deduplicate and re-observe.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from lib.supervisor import clock
from lib.supervisor import model_policy

CURRENT_SCHEMA_VERSION = 1
CURRENT_POLICY_VERSION = 1

JOB_STATES = ("registered", "active", "paused", "completed", "failed", "cancelled")
TERMINAL_JOB_STATES = ("completed", "failed", "cancelled")

# Journal states for an action. ``intent`` is committed before any side
# effect; ``dispatched`` means the side effect was attempted; ``uncertain``
# means the outcome could not be confirmed; ``reconciled`` means recorded
# evidence closed an uncertain action; ``completed``/``failed`` are terminal.
JOURNAL_STATES = (
    "intent",
    "dispatched",
    "uncertain",
    "reconciled",
    "completed",
    "failed",
)

REQUIRED_POLICY_FIELDS = (
    "authority_config",
    "model_selection",
    "inexpensive_allowlist",
    "manifest_snapshot_hash",
    "budgets",
    "deadlines",
)


class LedgerError(Exception):
    """Base class for supervisor-ledger failures."""


class LedgerVersionError(LedgerError):
    """The ledger's recorded schema version is newer than this code supports."""


# The design document refers to the same failure as ``SchemaVersionError``;
# keep both names so either spelling is importable.
SchemaVersionError = LedgerVersionError


class TrustedLocationError(LedgerError):
    """The configured ledger path resolves inside a worktree or ``.opsx-plan/``."""


class PolicyRevisionError(LedgerError):
    """A policy write violated explicit, monotonically increasing revisions."""


class DuplicateJobError(LedgerError):
    """A worktree already has an active supervised job."""


class JournalStateError(LedgerError):
    """An action transition was attempted from an invalid journal state."""


class UnknownRecordError(LedgerError):
    """A referenced job, action, or incident does not exist."""


def _utcnow() -> str:
    # Resolve through the owning module object so a rebound ``clock.utcnow``
    # is observed here (see the package's import discipline).
    return clock.utcnow()


# ---------------------------------------------------------------------------
# Path handling
# ---------------------------------------------------------------------------


def _canonical(path: os.PathLike[str] | str) -> Path:
    """Canonicalize *path*: expand ``~``, absolutize, resolve symlinks and ``..``.

    ``Path.resolve(strict=False)`` resolves the existing prefix (so symlinks
    and relative spellings cannot smuggle a location) and normalizes the
    remaining, non-existent components.
    """
    expanded = Path(os.path.expanduser(str(path)))
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return expanded.resolve(strict=False)


def _assert_trusted_location(db_path: Path) -> None:
    """Refuse a ledger path inside a worktree or under ``.opsx-plan/``.

    Raises :class:`TrustedLocationError` before anything is created.
    """
    for candidate in (db_path, *db_path.parents):
        if candidate.name == ".opsx-plan":
            raise TrustedLocationError(
                f"ledger location {db_path} is under a .opsx-plan/ directory; "
                "the supervisor ledger must live in external service-owned storage"
            )
        if (candidate / ".git").exists():
            raise TrustedLocationError(
                f"ledger location {db_path} resolves inside a repository worktree "
                f"({candidate}); the supervisor ledger must live outside any "
                "writable worktree"
            )


def default_ledger_path(home: os.PathLike[str] | str | None = None) -> Path:
    """Return the default external ledger path for this OS user.

    The concrete service-owned directory is finalized with the
    service-packaging change; this default is already outside any repository
    worktree, which is the invariant the trust boundary needs.
    """
    base = Path(home) if home is not None else Path.home()
    return base / ".local" / "share" / "opsx-controller" / "supervisor" / "supervisor.sqlite3"


def repository_relative(path: os.PathLike[str] | str, repository_root: os.PathLike[str] | str) -> str:
    """Return *path* as repository-relative data.

    Repository references are stored relative to a separately-recorded root so
    moving the checkout does not orphan ledger rows.
    """
    root = _canonical(repository_root)
    target = _canonical(path)
    return os.path.relpath(str(target), str(root))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_V1_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        repo_root TEXT NOT NULL,
        worktree_path TEXT NOT NULL,
        state TEXT NOT NULL,
        owner TEXT NOT NULL,
        owner_principal TEXT,
        owner_host TEXT,
        owner_boot_id TEXT,
        high_water_incident INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_worktree
        ON jobs (repo_root, worktree_path)
        WHERE state NOT IN ('completed', 'failed', 'cancelled')
    """,
    """
    CREATE TABLE IF NOT EXISTS actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL REFERENCES jobs (id),
        run_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        state TEXT NOT NULL,
        intent_at TEXT NOT NULL,
        dispatched_at TEXT,
        updated_at TEXT NOT NULL,
        detail TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_actions_job ON actions (job_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_actions_run ON actions (run_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS dispatches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action_id INTEGER NOT NULL REFERENCES actions (id),
        job_id INTEGER NOT NULL REFERENCES jobs (id),
        session_id TEXT,
        process_id TEXT,
        detail TEXT,
        dispatched_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_dispatches_action ON dispatches (action_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS incidents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL REFERENCES jobs (id),
        run_id TEXT,
        kind TEXT NOT NULL,
        state TEXT NOT NULL,
        summary TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_incidents_job ON incidents (job_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action_id INTEGER NOT NULL REFERENCES actions (id),
        kind TEXT NOT NULL,
        payload TEXT,
        recorded_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_evidence_action ON evidence (action_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS job_policies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL REFERENCES jobs (id),
        revision INTEGER NOT NULL,
        policy_version INTEGER NOT NULL,
        is_current INTEGER NOT NULL DEFAULT 1,
        authority_config TEXT NOT NULL,
        model_selection TEXT NOT NULL,
        inexpensive_allowlist TEXT NOT NULL,
        manifest_snapshot_hash TEXT NOT NULL,
        budgets TEXT NOT NULL,
        deadlines TEXT NOT NULL,
        operator TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (job_id, revision)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_job_policies_current
        ON job_policies (job_id)
        WHERE is_current = 1
    """,
    """
    CREATE TRIGGER IF NOT EXISTS job_policies_guard_update
        BEFORE UPDATE ON job_policies
        FOR EACH ROW
        WHEN NOT (
            OLD.is_current = 1 AND NEW.is_current = 0
            AND NEW.job_id = OLD.job_id
            AND NEW.revision = OLD.revision
            AND NEW.policy_version = OLD.policy_version
            AND NEW.authority_config = OLD.authority_config
            AND NEW.model_selection = OLD.model_selection
            AND NEW.inexpensive_allowlist = OLD.inexpensive_allowlist
            AND NEW.manifest_snapshot_hash = OLD.manifest_snapshot_hash
            AND NEW.budgets = OLD.budgets
            AND NEW.deadlines = OLD.deadlines
            AND NEW.operator = OLD.operator
            AND NEW.created_at = OLD.created_at
        )
    BEGIN
        SELECT RAISE(ABORT, 'job_policies rows are insert-only');
    END
    """,
)


def _migrate_0_to_1(conn: sqlite3.Connection) -> None:
    """Bootstrap the version-1 schema (idempotent)."""
    for statement in _SCHEMA_V1_STATEMENTS:
        conn.execute(statement)


MIGRATIONS: dict[int, Any] = {
    1: _migrate_0_to_1,
}


def _read_user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


def _write_user_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(f"PRAGMA user_version = {int(version)}")


def _after_job_insert(conn: sqlite3.Connection, job_id: int) -> None:
    """Interruption-simulation seam between the job and policy inserts.

    Production is a no-op. Tests patch this to raise inside
    :meth:`Ledger.register_job`'s transaction so they can assert that an
    interrupted multi-record write leaves no partial state on reopen.
    """


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class Ledger:
    """A schema-versioned SQLite supervisor ledger.

    The constructor opens (and, when absent, creates) the single ledger file
    at *path*. Schema bootstrap or forward migration runs in one transaction;
    a newer-than-supported ledger raises :class:`LedgerVersionError` before
    the file is touched.
    """

    def __init__(
        self,
        path: os.PathLike[str] | str | None = None,
        *,
        repository_root: os.PathLike[str] | str | None = None,
        create: bool = True,
        busy_timeout: int = 5000,
    ) -> None:
        requested = Path(path) if path is not None else default_ledger_path()
        db_path = _canonical(requested)
        if db_path.is_dir():
            db_path = db_path / "supervisor.sqlite3"

        _assert_trusted_location(db_path)

        self.path = db_path
        self.repository_root = (
            str(_canonical(repository_root)) if repository_root is not None else None
        )
        self._busy_timeout = busy_timeout

        if not create and not db_path.exists():
            raise LedgerError(f"ledger does not exist: {db_path}")

        if not db_path.exists():
            db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            str(db_path), isolation_level=None, timeout=busy_timeout / 1000.0
        )
        self._conn.row_factory = sqlite3.Row
        try:
            self._bootstrap()
        except Exception:
            self._conn.close()
            raise

    # -- lifecycle ---------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying connection (exposed for migrations and diagnostics)."""
        return self._conn

    def _bootstrap(self) -> None:
        # Read the recorded version before any write. A newer ledger must not
        # be modified, so the version check precedes every schema write.
        version = _read_user_version(self._conn)
        if version > CURRENT_SCHEMA_VERSION:
            raise LedgerVersionError(
                f"ledger schema version {version} is newer than supported "
                f"version {CURRENT_SCHEMA_VERSION}; reinstall the matching runtime"
            )

        self._conn.execute("PRAGMA busy_timeout = %d" % int(self._busy_timeout))
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")

        if version < CURRENT_SCHEMA_VERSION:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for target in range(version + 1, CURRENT_SCHEMA_VERSION + 1):
                    migrate = MIGRATIONS.get(target)
                    if migrate is None:
                        raise LedgerError(f"no migration registered for schema version {target}")
                    migrate(self._conn)
                _write_user_version(self._conn, CURRENT_SCHEMA_VERSION)
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def pragma(self, name: str) -> Any:
        """Return a scalar ``PRAGMA <name>`` value for the open connection."""
        row = self._conn.execute(f"PRAGMA {name}").fetchone()
        return row[0] if row is not None else None

    def schema_version(self) -> int:
        return _read_user_version(self._conn)

    # -- jobs --------------------------------------------------------------

    def register_job(
        self,
        *,
        run_id: str,
        worktree: os.PathLike[str] | str,
        repository_root: os.PathLike[str] | str | None = None,
        owner: str,
        policy: Mapping[str, Any],
        operator: str,
        owner_principal: str | None = None,
        owner_host: str | None = None,
        owner_boot_id: str | None = None,
        policy_version: int = CURRENT_POLICY_VERSION,
    ) -> int:
        """Register a supervised job with its initial policy revision.

        The job row and policy revision 1 are written in a single transaction.
        A second registration for a worktree that already has an active job
        raises :class:`DuplicateJobError` and leaves the existing job
        unchanged.
        """
        root = repository_root if repository_root is not None else self.repository_root
        if root is None:
            raise LedgerError("register_job requires a repository_root")
        repo_root = str(_canonical(root))
        worktree_rel = repository_relative(worktree, repo_root)

        fields = self._validate_policy(policy)
        now = _utcnow()

        existing = self._conn.execute(
            "SELECT id FROM jobs WHERE repo_root = ? AND worktree_path = ? "
            "AND state NOT IN ('completed', 'failed', 'cancelled')",
            (repo_root, worktree_rel),
        ).fetchone()
        if existing is not None:
            raise DuplicateJobError(
                f"worktree {worktree_rel} already has active supervised job "
                f"{existing['id']}"
            )

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            cursor = self._conn.execute(
                """
                INSERT INTO jobs (
                    run_id, repo_root, worktree_path, state, owner,
                    owner_principal, owner_host, owner_boot_id,
                    high_water_incident, created_at, updated_at
                ) VALUES (?, ?, ?, 'registered', ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    run_id, repo_root, worktree_rel, owner,
                    owner_principal, owner_host, owner_boot_id, now, now,
                ),
            )
            job_id = int(cursor.lastrowid)
            _after_job_insert(self._conn, job_id)
            self._insert_policy(
                self._conn, job_id=job_id, revision=1,
                policy_version=policy_version, fields=fields, operator=operator,
                now=now, is_current=1,
            )
            _ = self._conn.execute(
                "UPDATE jobs SET state = 'active', updated_at = ? WHERE id = ?",
                (now, job_id),
            )
        except sqlite3.IntegrityError as exc:
            self._conn.execute("ROLLBACK")
            if "idx_jobs_active_worktree" in str(exc) or "UNIQUE" in str(exc):
                raise DuplicateJobError(
                    f"worktree {worktree_rel} already has an active supervised job"
                ) from exc
            raise
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")
        return job_id

    def get_job(self, job_id: int) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise UnknownRecordError(f"no such job: {job_id}")
        return row

    def find_job_by_worktree(
        self, worktree: os.PathLike[str] | str, repository_root: os.PathLike[str] | str | None = None
    ) -> sqlite3.Row | None:
        root = repository_root if repository_root is not None else self.repository_root
        if root is None:
            raise LedgerError("find_job_by_worktree requires a repository_root")
        worktree_rel = repository_relative(worktree, root)
        return self._conn.execute(
            "SELECT * FROM jobs WHERE repo_root = ? AND worktree_path = ?",
            (str(_canonical(root)), worktree_rel),
        ).fetchone()

    def list_jobs(self, *, state: str | None = None) -> list[sqlite3.Row]:
        if state is None:
            return list(self._conn.execute("SELECT * FROM jobs ORDER BY id"))
        return list(self._conn.execute("SELECT * FROM jobs WHERE state = ? ORDER BY id", (state,)))

    def set_job_state(self, job_id: int, state: str) -> None:
        if state not in JOB_STATES:
            raise LedgerError(f"unknown job state: {state}")
        self.get_job(job_id)
        with self._transaction():
            _ = self._conn.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE id = ?",
                (state, _utcnow(), job_id),
            )

    # -- policy ------------------------------------------------------------

    @staticmethod
    def _validate_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
        missing = [field for field in REQUIRED_POLICY_FIELDS if field not in policy]
        if missing:
            raise PolicyRevisionError(
                "policy is missing required field(s): " + ", ".join(missing)
            )
        fields = {field: policy[field] for field in REQUIRED_POLICY_FIELDS}
        # Route the model-policy fields through the shared schema so new
        # writes are strict and versioned. New writes may not create
        # unversioned legacy payloads.
        fields["model_selection"] = model_policy.encode_model_selection(
            fields["model_selection"]
        )
        fields["inexpensive_allowlist"] = model_policy.encode_allowlist(
            fields["inexpensive_allowlist"]
        )
        return fields

    def _insert_policy(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: int,
        revision: int,
        policy_version: int,
        fields: Mapping[str, Any],
        operator: str,
        now: str,
        is_current: int,
    ) -> int:
        encoded = {
            field: json.dumps(fields[field]) for field in REQUIRED_POLICY_FIELDS
        }
        cursor = conn.execute(
            """
            INSERT INTO job_policies (
                job_id, revision, policy_version, is_current,
                authority_config, model_selection, inexpensive_allowlist,
                manifest_snapshot_hash, budgets, deadlines, operator, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id, revision, policy_version, is_current,
                encoded["authority_config"], encoded["model_selection"],
                encoded["inexpensive_allowlist"], encoded["manifest_snapshot_hash"],
                encoded["budgets"], encoded["deadlines"], operator, now,
            ),
        )
        return int(cursor.lastrowid)

    def revise_policy(
        self,
        job_id: int,
        *,
        revision: int,
        policy: Mapping[str, Any],
        operator: str,
        policy_version: int = CURRENT_POLICY_VERSION,
    ) -> int:
        """Insert a new explicit policy revision, superseding the prior one.

        *revision* must be exactly one greater than the current revision. Any
        other value raises :class:`PolicyRevisionError` and changes nothing.
        """
        self.get_job(job_id)
        fields = self._validate_policy(policy)
        current = self.current_policy(job_id)
        expected = int(current["revision"]) + 1
        if revision != expected:
            raise PolicyRevisionError(
                f"policy revision {revision} is not the next revision "
                f"({expected}); policy changes require an explicit increment"
            )
        now = _utcnow()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            _ = self._conn.execute(
                "UPDATE job_policies SET is_current = 0 WHERE job_id = ? AND is_current = 1",
                (job_id,),
            )
            policy_id = self._insert_policy(
                self._conn, job_id=job_id, revision=revision,
                policy_version=policy_version, fields=fields, operator=operator,
                now=now, is_current=1,
            )
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")
        return policy_id

    @staticmethod
    def _decode_policy(row: sqlite3.Row) -> dict[str, Any]:
        record = {
            "id": int(row["id"]),
            "job_id": int(row["job_id"]),
            "revision": int(row["revision"]),
            "policy_version": int(row["policy_version"]),
            "is_current": bool(row["is_current"]),
            "operator": row["operator"],
            "created_at": row["created_at"],
        }
        for field in REQUIRED_POLICY_FIELDS:
            record[field] = json.loads(row[field])

        # Route the model-policy fields through the shared decoders so a newer
        # recorded nested version raises ModelPolicyVersionError, and a stored
        # value with no 'version' key is classified legacy_unversioned rather
        # than rejected or reinterpreted. Preserve the raw field value.
        selection = model_policy.decode_model_selection(record["model_selection"])
        allowlist = model_policy.decode_allowlist(record["inexpensive_allowlist"])
        record["model_policy_state"] = {
            "model_selection": selection["state"],
            "inexpensive_allowlist": allowlist["state"],
        }
        return record

    def current_policy(self, job_id: int) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM job_policies WHERE job_id = ? AND is_current = 1",
            (job_id,),
        ).fetchone()
        if row is None:
            raise UnknownRecordError(f"no current policy for job {job_id}")
        return self._decode_policy(row)

    def policy_revision(self, job_id: int, revision: int) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM job_policies WHERE job_id = ? AND revision = ?",
            (job_id, revision),
        ).fetchone()
        if row is None:
            raise UnknownRecordError(f"no policy revision {revision} for job {job_id}")
        return self._decode_policy(row)

    def list_policy_revisions(self, job_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM job_policies WHERE job_id = ? ORDER BY revision",
            (job_id,),
        ).fetchall()
        return [self._decode_policy(row) for row in rows]

    # -- incidents ---------------------------------------------------------

    def record_incident(
        self,
        job_id: int,
        *,
        kind: str,
        summary: str = "",
        state: str = "open",
        run_id: str | None = None,
    ) -> int:
        self.get_job(job_id)
        now = _utcnow()
        with self._transaction():
            cursor = self._conn.execute(
                """
                INSERT INTO incidents (job_id, run_id, kind, state, summary, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, run_id, kind, state, summary, now, now),
            )
            incident_id = int(cursor.lastrowid)
            _ = self._conn.execute(
                "UPDATE jobs SET high_water_incident = ?, updated_at = ? WHERE id = ?",
                (incident_id, now, job_id),
            )
        return incident_id

    def get_incident(self, incident_id: int) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
        if row is None:
            raise UnknownRecordError(f"no such incident: {incident_id}")
        return row

    def list_incidents(self, job_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM incidents WHERE job_id = ? ORDER BY id", (job_id,)
            )
        )

    # -- journal: intents, dispatch, evidence, uncertainty -----------------

    def begin_action(
        self,
        job_id: int,
        *,
        kind: str,
        run_id: str,
        detail: str | None = None,
    ) -> int:
        """Commit an action intent in its own transaction before any effect."""
        self.get_job(job_id)
        now = _utcnow()
        with self._transaction():
            cursor = self._conn.execute(
                """
                INSERT INTO actions (job_id, run_id, kind, state, intent_at, updated_at, detail)
                VALUES (?, ?, ?, 'intent', ?, ?, ?)
                """,
                (job_id, run_id, kind, now, now, detail),
            )
            action_id = int(cursor.lastrowid)
        return action_id

    def get_action(self, action_id: int) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM actions WHERE id = ?", (action_id,)
        ).fetchone()
        if row is None:
            raise UnknownRecordError(f"no such action: {action_id}")
        return row

    def list_actions(self, job_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM actions WHERE job_id = ? ORDER BY id", (job_id,)
            )
        )

    def query_actions_by_run(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM actions WHERE run_id = ? ORDER BY id", (run_id,)
            )
        )

    def dispatch_action(
        self,
        action_id: int,
        *,
        session_id: str | None = None,
        process_id: str | None = None,
        detail: str | None = None,
    ) -> int:
        """Record a dispatch and mark the action dispatched, transactionally.

        The transition is guarded inside one ``BEGIN IMMEDIATE`` transaction.
        A terminal (``completed`` or ``failed``) action is never dispatched: it
        has already reached an outcome. An ``uncertain`` action must be
        reconciled with recorded evidence before it can be dispatched.
        """
        now = _utcnow()
        with self._transaction():
            action = self.get_action(action_id)
            state = action["state"]
            if state in ("completed", "failed"):
                raise JournalStateError(
                    f"action {action_id} is terminal ({state}); it is not dispatched"
                )
            if state == "uncertain":
                raise JournalStateError(
                    f"action {action_id} is uncertain; record reconciling evidence "
                    "before dispatching it"
                )
            if state not in ("intent", "dispatched", "reconciled"):
                raise JournalStateError(
                    f"action {action_id} cannot be dispatched from state {state!r}"
                )
            cursor = self._conn.execute(
                """
                INSERT INTO dispatches (action_id, job_id, session_id, process_id, detail, dispatched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (action_id, int(action["job_id"]), session_id, process_id, detail, now),
            )
            dispatch_id = int(cursor.lastrowid)
            result = self._conn.execute(
                "UPDATE actions SET state = 'dispatched', dispatched_at = ?, updated_at = ? "
                "WHERE id = ? AND state = ?",
                (now, now, action_id, state),
            )
            if result.rowcount != 1:
                raise JournalStateError(
                    f"action {action_id} changed state during dispatch; retry"
                )
        return dispatch_id

    def mark_uncertain(self, action_id: int, *, detail: str | None = None) -> None:
        """Mark an action explicitly uncertain when its outcome is unconfirmed."""
        now = _utcnow()
        with self._transaction():
            action = self.get_action(action_id)
            state = action["state"]
            if state not in ("intent", "dispatched"):
                raise JournalStateError(
                    f"action {action_id} cannot be marked uncertain from state "
                    f"{state!r}"
                )
            result = self._conn.execute(
                "UPDATE actions SET state = 'uncertain', updated_at = ?, "
                "detail = COALESCE(?, detail) WHERE id = ? AND state = ?",
                (now, detail, action_id, state),
            )
            if result.rowcount != 1:
                raise JournalStateError(
                    f"action {action_id} changed state while marking uncertain; retry"
                )

    def record_evidence(
        self, action_id: int, *, kind: str, payload: Mapping[str, Any] | str | None = None
    ) -> int:
        """Record evidence; reconcile an uncertain action in the same transaction."""
        encoded = payload if isinstance(payload, str) or payload is None else json.dumps(payload)
        now = _utcnow()
        with self._transaction():
            action = self.get_action(action_id)
            state = action["state"]
            cursor = self._conn.execute(
                "INSERT INTO evidence (action_id, kind, payload, recorded_at) VALUES (?, ?, ?, ?)",
                (action_id, kind, encoded, now),
            )
            evidence_id = int(cursor.lastrowid)
            if state == "uncertain":
                result = self._conn.execute(
                    "UPDATE actions SET state = 'reconciled', updated_at = ? "
                    "WHERE id = ? AND state = 'uncertain'",
                    (now, action_id),
                )
                if result.rowcount != 1:
                    raise JournalStateError(
                        f"action {action_id} changed state while reconciling; retry"
                    )
        return evidence_id

    def list_evidence(self, action_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM evidence WHERE action_id = ? ORDER BY id", (action_id,)
            )
        )

    def complete_action(self, action_id: int) -> None:
        """Mark an action complete. Uncertain actions need reconciliation first."""
        now = _utcnow()
        with self._transaction():
            action = self.get_action(action_id)
            state = action["state"]
            if state == "uncertain":
                raise JournalStateError(
                    f"action {action_id} is uncertain; record reconciling evidence "
                    "before completing it"
                )
            if state == "intent":
                raise JournalStateError(
                    f"action {action_id} has only an intent; dispatch it before completing"
                )
            if state in ("completed", "failed"):
                raise JournalStateError(
                    f"action {action_id} is already terminal ({state})"
                )
            result = self._conn.execute(
                "UPDATE actions SET state = 'completed', updated_at = ? "
                "WHERE id = ? AND state = ?",
                (now, action_id, state),
            )
            if result.rowcount != 1:
                raise JournalStateError(
                    f"action {action_id} changed state while completing; retry"
                )

    def fail_action(self, action_id: int, *, detail: str | None = None) -> None:
        """Mark an action failed. ``failed`` is terminal.

        An ``uncertain`` action must not be recorded as failed without
        evidence: uncertainty is resolved by reconciling evidence, not by
        declaring an unconfirmed outcome failed. Reconciliation records what
        was observed (including a confirmed failure) and the observed outcome
        is then applied with :meth:`complete_action` or a reconciled
        :meth:`fail_action`. A terminal (``completed`` or ``failed``) action
        cannot transition again.
        """
        now = _utcnow()
        with self._transaction():
            action = self.get_action(action_id)
            state = action["state"]
            if state == "uncertain":
                raise JournalStateError(
                    f"action {action_id} is uncertain; record reconciling evidence "
                    "before failing it"
                )
            if state in ("completed", "failed"):
                raise JournalStateError(
                    f"action {action_id} is already terminal ({state})"
                )
            result = self._conn.execute(
                "UPDATE actions SET state = 'failed', updated_at = ?, "
                "detail = COALESCE(?, detail) WHERE id = ? AND state = ?",
                (now, detail, action_id, state),
            )
            if result.rowcount != 1:
                raise JournalStateError(
                    f"action {action_id} changed state while failing; retry"
                )

    def replay_action(
        self,
        action_id: int,
        *,
        session_id: str | None = None,
        process_id: str | None = None,
    ) -> int:
        """Re-dispatch an action, deduplicating and re-observing its effects.

        Uncertain actions must be reconciled with evidence first; completed and
        failed actions are terminal and are never replayed. The journal makes
        no exactly-once claim, so replay is a fresh dispatch that callers must
        deduplicate.
        """
        action = self.get_action(action_id)
        state = action["state"]
        if state == "uncertain":
            raise JournalStateError(
                f"action {action_id} is uncertain; record reconciling evidence "
                "before replaying it"
            )
        if state in ("completed", "failed"):
            raise JournalStateError(
                f"action {action_id} is already terminal ({state})"
            )
        if state == "intent":
            raise JournalStateError(
                f"action {action_id} has only an intent; dispatch it instead of replaying"
            )
        return self.dispatch_action(
            action_id, session_id=session_id, process_id=process_id,
            detail="replay",
        )

    # -- transactions ------------------------------------------------------

    def _transaction(self) -> "_Transaction":
        return _Transaction(self._conn)


class _Transaction:
    """Context manager wrapping one explicit ``BEGIN IMMEDIATE`` transaction."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> "_Transaction":
        self._conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self._conn.execute("COMMIT")
        else:
            self._conn.execute("ROLLBACK")


# ``SupervisorLedger`` is the descriptive public name for the same type.
SupervisorLedger = Ledger


def open_ledger(
    path: os.PathLike[str] | str | None = None,
    *,
    repository_root: os.PathLike[str] | str | None = None,
    create: bool = True,
    busy_timeout: int = 5000,
) -> Ledger:
    """Open or create a supervisor ledger at *path*."""
    return Ledger(
        path, repository_root=repository_root, create=create, busy_timeout=busy_timeout
    )


def _iter_module_files() -> Iterable[Path]:  # pragma: no cover - test utility
    package = Path(__file__).resolve().parent
    return sorted(package.glob("*.py"))
