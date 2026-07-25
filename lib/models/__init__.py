"""Per-adapter model configuration package.

Provides a TOML-backed resolver that maps (adapter, role) pairs to a
concrete model identifier through a defined precedence ladder, plus
identifier-syntax validation for the adapters that require it.
"""

from lib.models.resolver import ModelConfigError, config_paths, resolve, validate
from lib.models.types import ROLE_ENV, ROLES, ResolvedModel

__all__ = [
    "ROLES",
    "ROLE_ENV",
    "ResolvedModel",
    "ModelConfigError",
    "config_paths",
    "resolve",
    "validate",
]
