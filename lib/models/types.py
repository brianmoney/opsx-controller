"""Type definitions for per-adapter model configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# The four roles every run needs — unresolved required role → fatal.
ROLES: tuple[str, ...] = ("controller", "implementer", "reviewer", "archiver")

# Roles that individual configurations may use but are not mandatory.
OPTIONAL_ROLES: tuple[str, ...] = ("implementer_escalation",)

# Every role the resolver inspects (required + optional).
ALL_ROLES: tuple[str, ...] = ROLES + OPTIONAL_ROLES

# Role -> the ambient/exported environment variable name for that role.
ROLE_ENV: dict[str, str] = {role: f"OPSX_{role.upper()}_MODEL" for role in ALL_ROLES}

# Canonical reasoning-effort labels accepted in model configuration. Adapters
# translate these labels to their client's vocabulary.
CANONICAL_VARIANTS: tuple[str, ...] = ("low", "medium", "high", "max")

# Role -> the ambient/exported environment variable for that role's
# reasoning variant. Variants are resolved from ``<role>_variant`` keys
# alongside the model and are never required.
ROLE_VARIANT_ENV: dict[str, str] = {
    role: f"OPSX_{role.upper()}_VARIANT" for role in ALL_ROLES
}


@dataclass(frozen=True)
class ResolvedModel:
    """The outcome of resolving one (adapter, role) pair.

    ``model`` is ``None`` when no source provided a value for this role;
    ``source`` describes where the value came from (or ``"unresolved"``).
    ``variant`` is the optional canonical reasoning-effort label resolved from
    ``<role>_variant`` keys (``None`` when unset — the adapter then keeps its
    built-in default).
    """

    role: str
    model: Optional[str]
    source: str
    variant: Optional[str] = None
    variant_source: str = "unresolved"
