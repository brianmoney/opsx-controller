"""Supervisor runtime package.

Durable supervision storage for plan execution. The package owns the
schema-versioned SQLite supervisor ledger that records supervised jobs,
actions, incidents, and protected job policy.

Importing any submodule here must not parse arguments, spawn a process, or
touch ``.opsx-plan/`` — the package is imported by tests and, in later
changes, by supervision tooling, and it depends only on the Python standard
library. It never imports another runtime package (``lib.orchestrator``,
``lib.metrics``, ``lib.pricing``, ``lib.models``), so its dependency graph is
acyclic by construction.
"""

from __future__ import annotations

from lib.supervisor.ledger import (
    CURRENT_POLICY_VERSION,
    CURRENT_SCHEMA_VERSION,
    JOURNAL_STATES,
    JOB_STATES,
    TERMINAL_JOB_STATES,
    DuplicateJobError,
    JournalStateError,
    Ledger,
    LedgerError,
    LedgerVersionError,
    PolicyRevisionError,
    SchemaVersionError,
    SupervisorLedger,
    TrustedLocationError,
    UnknownRecordError,
    default_ledger_path,
    open_ledger,
)
from lib.supervisor import budgets as budgets

__all__ = [
    "CURRENT_POLICY_VERSION",
    "CURRENT_SCHEMA_VERSION",
    "JOURNAL_STATES",
    "JOB_STATES",
    "TERMINAL_JOB_STATES",
    "DuplicateJobError",
    "JournalStateError",
    "Ledger",
    "LedgerError",
    "LedgerVersionError",
    "PolicyRevisionError",
    "SchemaVersionError",
    "SupervisorLedger",
    "TrustedLocationError",
    "UnknownRecordError",
    "budgets",
    "default_ledger_path",
    "open_ledger",
]
