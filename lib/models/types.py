"""Type definitions for per-adapter model configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# The four phases every adapter dispatches a worker for.
ROLES: tuple[str, ...] = ("controller", "implementer", "reviewer", "archiver")

# Role -> the ambient/exported environment variable name for that role.
ROLE_ENV: dict[str, str] = {role: f"OPSX_{role.upper()}_MODEL" for role in ROLES}


@dataclass(frozen=True)
class ResolvedModel:
    """The outcome of resolving one (adapter, role) pair.

    ``model`` is ``None`` when no source provided a value for this role;
    ``source`` describes where the value came from (or ``"unresolved"``).
    """

    role: str
    model: Optional[str]
    source: str
