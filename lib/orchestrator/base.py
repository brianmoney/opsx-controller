"""Foundational orchestrator primitives: logging, timestamps, plan status.

Zero dependencies on any other orchestrator module — everything else may
import this one.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DONE = "done"
PENDING = "pending"
RUNNING = "running"
FAILED = "failed"
SKIPPED = "skipped"

ARCHIVE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")
TASK_RE = re.compile(r"^- \[(?P<done>[ xX])\]\s+")
# A task line whose text ends with "(manual)" (case-insensitive, trailing
# whitespace tolerated) is an operator-only manual task. Every consumer
# classifies tasks with this same marker so implement, review, and archive
# gates treat a task identically.
MANUAL_TASK_RE = re.compile(r"\(manual\)\s*$", re.IGNORECASE)


def classify_task_line(line: str) -> str:
    """Classify a raw tasks-file line as ``manual`` or ``automatable``.

    Callers must only pass lines already matched by ``TASK_RE``; the marker
    is detected on the raw line so wrapped task text still classifies.
    """
    return "manual" if MANUAL_TASK_RE.search(line) else "automatable"

ADAPTER_CLIENTS = {
    "opencode": "opencode",
    "claude-code": "claude",
    "codex-cli": "codex",
}

# Populated by the entrypoint's `_ensure_runtime_modules()` immediately after
# `lib.orchestrator` becomes importable. That function runs before the
# package exists on sys.path, so it cannot set this attribute directly at
# computation time — it assigns here right after the import succeeds. Empty
# until then; nothing in this package reads it at import time.
_RUNTIME_ROOTS: tuple[Path, ...] = ()

# Adapter defaults. Both fields accept a {change} placeholder and may be
# overridden in the [plan] table. Verify the invoke command for your client
# version before an unattended run.
ADAPTER_DEFAULTS = {
    "opencode": {
        "state_file": ".opencode/opsx-controller/{change}.json",
        "implement_invoke": (
            'opencode run --agent opsx-implementer --model "$OPSX_IMPLEMENTER_MODEL" '
            '--variant "$OPSX_IMPLEMENTER_VARIANT"'
        ),
        "review_invoke": (
            'opencode run --agent opsx-reviewer --model "$OPSX_REVIEWER_MODEL" '
            '--variant "$OPSX_REVIEWER_VARIANT"'
        ),
        "archive_invoke": (
            'opencode run --agent opsx-archiver --model "$OPSX_ARCHIVER_MODEL" '
            '--variant "$OPSX_ARCHIVER_VARIANT"'
        ),
    },
    "claude-code": {
        "state_file": ".claude/opsx-controller/{change}.json",
        "implement_invoke": (
            'claude -p --agent opsx-implementer --model "$OPSX_IMPLEMENTER_MODEL" '
            "--permission-mode bypassPermissions --output-format json"
        ),
        "review_invoke": (
            'claude -p --agent opsx-reviewer --model "$OPSX_REVIEWER_MODEL" '
            "--permission-mode bypassPermissions --output-format json"
        ),
        "archive_invoke": (
            'claude -p --agent opsx-archiver --model "$OPSX_ARCHIVER_MODEL" '
            "--permission-mode bypassPermissions --output-format json"
        ),
    },
    "codex-cli": {
        "state_file": ".opsx-controller/{change}.json",
    },
}


class PlanError(Exception):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[opsx-plan {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def ensure_own_root_on_syspath() -> None:
    """Re-add this package's own runtime root to sys.path if missing.

    Mirrors the entrypoint's own startup `sys.path` bootstrap, for modules
    that may be imported and called in isolation (e.g. a stage-recording
    call path) without that bootstrap having already run in-process.
    Unlike the entrypoint, this module's own file location already pins the
    one correct root (`lib/orchestrator/base.py`'s grandparent), so there is
    no candidate list to search.
    """
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)
