"""Per-adapter model resolver.

Resolves a ``(adapter, role)`` pair to a concrete model identifier by
consulting, highest precedence first: a repository-local configuration
file, a user-global configuration file, and the ambient environment.
See ``resolve()`` for the exact precedence ladder.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Mapping, Optional

from lib.models.types import ROLE_ENV, ROLE_VARIANT_ENV, ROLES, ALL_ROLES, ResolvedModel

USER_CONFIG_PATH = Path.home() / ".config" / "opsx-controller" / "models.toml"
REPO_CONFIG_RELATIVE = Path(".opsx-plan") / "models.toml"


class ModelConfigError(Exception):
    """Raised when a present model configuration file cannot be parsed."""


def config_paths(repo: Optional[Path]) -> list[Path]:
    """Return candidate configuration file paths, highest precedence first.

    When *repo* is given, the repository-local override is listed before
    the user-global file. When *repo* is ``None`` (no repository context),
    only the user-global file is returned.
    """
    paths: list[Path] = []
    if repo is not None:
        paths.append(repo / REPO_CONFIG_RELATIVE)
    paths.append(USER_CONFIG_PATH)
    return paths


def _load_config(path: Path) -> Optional[dict]:
    """Load and parse *path* as TOML.

    Returns ``None`` when the file does not exist. Raises
    ``ModelConfigError`` naming the file when it exists but cannot be
    parsed as TOML.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ModelConfigError(f"cannot read model configuration file {path}: {exc}") from exc

    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ModelConfigError(f"invalid TOML in model configuration file {path}: {exc}") from exc


def _table_value(data: Optional[dict], *keys: str) -> Optional[str]:
    """Walk nested dict *keys* and return a stripped non-empty string, or None."""
    node = data
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if not isinstance(node, str):
        return None
    value = node.strip()
    return value or None


def resolve(
    adapter: str,
    repo: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, ResolvedModel]:
    """Resolve all four roles for *adapter*.

    Precedence per role, highest first:
      1. ``[adapters.<adapter>].<role>`` in the repository-local file
      2. ``[adapters.<adapter>].<role>`` in the user-global file
      3. ``[defaults].<role>`` in the repository-local file, then user-global
      4. the ambient ``OPSX_<ROLE>_MODEL`` environment variable
      5. unresolved

    Raises ``ModelConfigError`` when a present configuration file is not
    valid TOML. Never raises for an unresolved role; that role's
    ``ResolvedModel.model`` is ``None`` and its ``source`` is
    ``"unresolved"``.
    """
    if environ is None:
        environ = os.environ

    paths = config_paths(repo)
    configs = [(path, _load_config(path)) for path in paths]
    repo_local = configs[0] if repo is not None else None
    user_global = configs[-1]

    resolved: dict[str, ResolvedModel] = {}
    for role in ALL_ROLES:
        model, source = _resolve_key(
            role, adapter, repo_local, user_global, environ, ROLE_ENV[role]
        )
        variant, variant_source = _resolve_key(
            f"{role}_variant",
            adapter,
            repo_local,
            user_global,
            environ,
            ROLE_VARIANT_ENV[role],
        )
        resolved[role] = ResolvedModel(
            role=role,
            model=model,
            source=source,
            variant=variant,
            variant_source=variant_source,
        )

    return resolved


def _resolve_key(
    key: str,
    adapter: str,
    repo_local: Optional[tuple],
    user_global: tuple,
    environ: Mapping[str, str],
    env_name: str,
) -> tuple[Optional[str], str]:
    """Resolve one config *key* through the standard precedence ladder.

    Precedence, highest first:
      1. ``[adapters.<adapter>].<key>`` in the repository-local file
      2. ``[adapters.<adapter>].<key>`` in the user-global file
      3. ``[defaults].<key>`` in the repository-local file, then user-global
      4. the ambient *env_name* environment variable
      5. ``(None, "unresolved")``
    """
    if repo_local is not None:
        path, data = repo_local
        value = _table_value(data, "adapters", adapter, key)
        if value is not None:
            return value, f"repo-local config ({path})"

    path, data = user_global
    value = _table_value(data, "adapters", adapter, key)
    if value is not None:
        return value, f"user-global config ({path})"

    if repo_local is not None:
        path, data = repo_local
        value = _table_value(data, "defaults", key)
        if value is not None:
            return value, f"repo-local config ({path}, [defaults])"

    path, data = user_global
    value = _table_value(data, "defaults", key)
    if value is not None:
        return value, f"user-global config ({path}, [defaults])"

    env_value = (environ.get(env_name) or "").strip()
    if env_value:
        return env_value, "ambient environment"

    return None, "unresolved"


def validate(adapter: str, resolved: Mapping[str, ResolvedModel]) -> list[str]:
    """Return one warning per role whose identifier violates *adapter*'s syntax.

    ``claude-code`` rejects provider-prefixed identifiers (containing
    ``/``). ``opencode`` requires the ``provider/model`` form. Unresolved
    roles are skipped — there is no identifier to check. Every violating
    role is reported, not just the first.
    """
    warnings: list[str] = []
    for role in ALL_ROLES:
        entry = resolved.get(role)
        if entry is None or not entry.model:
            continue
        if adapter == "claude-code" and "/" in entry.model:
            warnings.append(
                f"{role}: '{entry.model}' is a provider-prefixed identifier, "
                f"which the claude-code adapter does not accept"
            )
        elif adapter == "opencode" and "/" not in entry.model:
            warnings.append(
                f"{role}: '{entry.model}' is missing the required 'provider/' "
                f"prefix that the opencode adapter requires"
            )
    return warnings
