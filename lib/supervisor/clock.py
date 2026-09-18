"""Time source for the supervisor package.

Kept in its own module so cross-module references in the package resolve
through the owning module object (``clock.utcnow()``) rather than a name
import, matching the orchestrator package discipline. A test can rebind
``clock.utcnow`` and observe the replacement from the calling module.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string (second precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
