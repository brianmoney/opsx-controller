"""Read-only supervision service probe and fail-closed activation gate.

This concern-named orchestrator runtime module owns everything the command
surface needs to report on the packaged Linux supervision service:

- the installed paths of the versioned systemd user unit template and the
  provisioning document deployed by ``scripts/install-orchestrator.sh``,
- any rendered service unit the operator installed into the systemd user unit
  directory,
- the supervisor ledger schema version when a ledger is present, and
- the isolation-backend capability status.

The probe is read-only and side-effect free. It imports without running the
CLI, spawns no process, reads or writes nothing under ``.opsx-plan/`` at import
time, and never enables, starts, writes, or provisions the service. It uses the
standard library and the existing runtime packages only. An absent or
unsupported service state is *reported*, not failed, so legacy unsupervised
installations stay green in ``opsx-plan doctor``.

Activation itself is a separate, documented operator action. This module exposes
:func:`activation_gate`, which composes the read-only
:func:`service_host_capability` service-host check (systemd user manager and
OpenCode session bridge) with the existing
:func:`lib.supervisor.authority.require_authority_backend` authority gate, which
composes detection with the mandatory activation probe. Either unsupported
prerequisite raises the named
:class:`lib.supervisor.authority.UnsupportedHostError`; a failing probe raises
the named ``ActivationProbeError``. There is deliberately no weaker activation
path and no silent fallback.

The service host is a systemd **user** unit, so its effective identity is the
user whose manager loads it. The packaged template pins that identity with
``AssertUser=``; this module's host check only validates that a supported
manager/bridge exists, never which user is invoking the probe.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

#: Installed-runtime-relative path of the versioned systemd user unit template.
INSTALLED_TEMPLATE_RELPATH = "systemd/opsx-supervise.service.in"
#: Installed-runtime-relative path of the provisioning document.
INSTALLED_DOCUMENT_RELPATH = "docs/opsx-supervision-service.md"
#: Name the operator renders the template to inside the systemd user unit dir.
RENDERED_UNIT_NAME = "opsx-supervise.service"
#: Doctor label; ``run_doctor_checks`` special-cases it for the detail dump.
CHECK_LABEL = "Supervision service packaging is reported"

_INSTALLED_RUNTIME_PARTS = (".local", "lib", "opsx-controller")
_SYSTEMD_USER_UNIT_PARTS = (".config", "systemd", "user")

#: Service-host capability status values. ``unsupported`` is fail-closed: the
#: activation gate refuses it and substitutes no weaker posture.
HOST_AVAILABLE = "available"
HOST_UNSUPPORTED = "unsupported"

#: Environment key an operator may set to pin the session-server argv instead
#: of resolving the ``opencode`` CLI. Mirrors
#: ``lib.orchestrator.supervision.ENV_SERVER_COMMAND``; kept as a local literal
#: so this module's only non-stdlib dependency stays ``lib.supervisor``.
_SESSION_SERVER_COMMAND_ENV = "OPSX_SESSION_SERVER_COMMAND"

#: Trusted launch directories for host prerequisites. Ambient ``PATH`` is not
#: consulted, mirroring the authority boundary's trusted-directory discipline.
_TRUSTED_HOST_BIN_DIRS = (
    "/usr/bin",
    "/usr/sbin",
    "/bin",
    "/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
)


def _default_which(name: str) -> str | None:
    """Resolve *name* using only the fixed trusted host directories."""
    return shutil.which(name, path=os.pathsep.join(_TRUSTED_HOST_BIN_DIRS))


def _is_opencode_invocation(command: str) -> bool:
    """Return True only when *command* pins an allowed OpenCode bridge argv.

    A configured ``OPSX_SESSION_SERVER_COMMAND`` replaces the documented
    ``opencode serve`` invocation wholesale, so it is accepted only when its
    executable is ``opencode`` and its first argument is exactly the
    ``serve`` subcommand. A later argument merely *named* ``serve`` (as in
    ``opencode run serve``) is not a server invocation and fails closed, as
    is any other string — including a nonempty non-OpenCode command or an
    unparseable one. The check is read-only: it parses the configured text
    and resolves nothing.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if len(argv) < 2:
        return False
    return os.path.basename(argv[0]) == "opencode" and argv[1] == "serve"


def _default_runtime_dir(env: Mapping[str, str]) -> Path | None:
    """Return the invoking user's systemd runtime directory, or ``None``."""
    configured = (env.get("XDG_RUNTIME_DIR") or "").strip()
    if configured:
        return Path(configured)
    try:
        uid = os.getuid()
    except AttributeError:  # pragma: no cover - non-POSIX hosts
        return None
    return Path(f"/run/user/{uid}")


@dataclass(frozen=True)
class ServiceHostReport:
    """The read-only service-host prerequisite report.

    ``available`` only when both the systemd user manager and an OpenCode
    session bridge are supported. Every unsupported prerequisite contributes a
    reason rather than being skipped, so the activation gate can name it.
    """

    status: str
    systemd_user_manager: bool
    opencode_bridge: bool
    reasons: tuple[str, ...]
    runtime_dir: str | None = None

    @property
    def available(self) -> bool:
        return self.status == HOST_AVAILABLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "systemd_user_manager": self.systemd_user_manager,
            "opencode_bridge": self.opencode_bridge,
            "reasons": list(self.reasons),
            "runtime_dir": self.runtime_dir,
        }


def service_host_capability(
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    runtime_dir: os.PathLike[str] | str | None = None,
    path_exists: Callable[[Path], bool] | None = None,
) -> ServiceHostReport:
    """Return the read-only service-host prerequisite report.

    Pure, unprivileged, and side-effect free: it reads no service-manager
    state, runs no command, writes nothing, and never enables or starts
    anything. The two supported prerequisites are:

    - a **systemd user manager**: a trusted ``systemctl`` plus a live
      ``$XDG_RUNTIME_DIR/systemd/private`` user-manager socket, and
    - an **OpenCode session bridge**: a trusted ``opencode`` executable or an
      explicitly pinned ``OPSX_SESSION_SERVER_COMMAND`` that names an allowed
      ``opencode serve`` invocation.

    Anything else — including non-systemd service managers and non-OpenCode
    bridges, whether detected or explicitly configured — is ``unsupported``
    and fails closed. Every input is injectable so fixtures can simulate an
    unsupported host without touching the real one.
    """
    environment = os.environ if env is None else env
    lookup = which or _default_which
    exists = path_exists or (lambda path: os.path.exists(path))

    reasons: list[str] = []

    resolved_runtime = (
        Path(runtime_dir)
        if runtime_dir is not None
        else _default_runtime_dir(environment)
    )
    manager_socket = (
        resolved_runtime / "systemd" / "private"
        if resolved_runtime is not None
        else None
    )
    systemd_user_manager = bool(lookup("systemctl")) and manager_socket is not None and exists(
        manager_socket
    )
    if not systemd_user_manager:
        reasons.append(
            "no supported systemd user manager is available (a trusted systemctl "
            "and a live $XDG_RUNTIME_DIR/systemd/private user-manager socket are "
            "required); non-systemd service managers are unsupported"
        )

    configured_command = (environment.get(_SESSION_SERVER_COMMAND_ENV) or "").strip()
    if configured_command:
        # The pinned command replaces the default ``opencode serve`` argv, so
        # a configured non-OpenCode bridge is refused outright — the trusted
        # ``opencode`` lookup must not silently rescue it.
        opencode_bridge = _is_opencode_invocation(configured_command)
        if not opencode_bridge:
            reasons.append(
                "OPSX_SESSION_SERVER_COMMAND does not name an allowed OpenCode "
                "'opencode serve' invocation; non-OpenCode session bridges are "
                "unsupported"
            )
    else:
        opencode_bridge = bool(lookup("opencode"))
        if not opencode_bridge:
            reasons.append(
                "no supported OpenCode session bridge is available (the 'opencode' "
                "CLI is not resolvable and OPSX_SESSION_SERVER_COMMAND is unset); "
                "non-OpenCode session bridges are unsupported"
            )

    return ServiceHostReport(
        status=HOST_AVAILABLE if not reasons else HOST_UNSUPPORTED,
        systemd_user_manager=systemd_user_manager,
        opencode_bridge=opencode_bridge,
        reasons=tuple(reasons),
        runtime_dir=str(resolved_runtime) if resolved_runtime is not None else None,
    )


def _authority() -> Any:
    """Resolve the live ``lib.supervisor.authority`` module at call time.

    Resolving through ``sys.modules`` keeps cross-module references bound to
    the owning module object and stays correct through test module churn (a
    fresh import can rebind the package attribute while ``sys.modules`` holds
    the module the command surface imported).
    """
    import sys

    module = sys.modules.get("lib.supervisor.authority")
    if module is not None:
        return module
    from lib.supervisor import authority as authority_module

    return authority_module


def _home(home: Path | None = None) -> Path:
    return Path.home() if home is None else Path(home)


def installed_runtime_root(home: Path | None = None) -> Path:
    """Return the global installed runtime root (``~/.local/lib/opsx-controller``)."""
    return _home(home).joinpath(*_INSTALLED_RUNTIME_PARTS)


def template_path(home: Path | None = None, runtime_root: Path | None = None) -> Path:
    root = Path(runtime_root) if runtime_root is not None else installed_runtime_root(home)
    return root / INSTALLED_TEMPLATE_RELPATH


def document_path(home: Path | None = None, runtime_root: Path | None = None) -> Path:
    root = Path(runtime_root) if runtime_root is not None else installed_runtime_root(home)
    return root / INSTALLED_DOCUMENT_RELPATH


def installed_unit_path(home: Path | None = None) -> Path:
    """Return the systemd user unit path an operator would render into."""
    return _home(home).joinpath(*_SYSTEMD_USER_UNIT_PARTS) / RENDERED_UNIT_NAME


def default_ledger_path(env: Mapping[str, str] | None = None) -> Path | None:
    """Return the configured/default authority-store file path, or ``None``.

    The default is derived from the fixed service principal, never the invoking
    user's home. Resolution is best-effort and side-effect free: an unresolved
    service principal or an untrusted location yields ``None`` rather than an
    error, because the probe only reports on an already-provisioned store.
    """
    environment = os.environ if env is None else env
    service_uid: int | None = None
    try:
        service = _authority().resolve_principal(
            "service", _authority().DEFAULT_SERVICE_PRINCIPAL
        )
        service_uid = service.uid
    except Exception:  # noqa: BLE001 - a missing principal is reported, not raised
        service_uid = None
    try:
        return _authority().default_state_path(env=environment, service_uid=service_uid)
    except Exception:  # noqa: BLE001 - an untrusted location is reported, not raised
        return None


def read_ledger_schema_version(ledger_path: Path | str | None) -> int | None:
    """Read ``PRAGMA user_version`` from *ledger_path* without migrating it.

    The connection is opened read-only and immutable, so the probe cannot
    create or migrate a ledger. A missing file, a non-ledger file, or any
    unreadable store returns ``None``.
    """
    if ledger_path is None:
        return None
    path = Path(ledger_path)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ServiceProbe:
    """The read-only service/schema/backend report."""

    template_path: str
    template_installed: bool
    document_path: str
    document_installed: bool
    unit_path: str
    unit_installed: bool
    ledger_path: str | None
    ledger_schema_version: int | None
    backend_status: str
    backend_reasons: tuple[str, ...]
    service_host_status: str
    service_host_reasons: tuple[str, ...]

    @property
    def service_enabled(self) -> bool:
        """A rendered unit is present; the probe never queries the manager."""
        return self.unit_installed

    def as_dict(self) -> dict[str, Any]:
        return {
            "template_path": self.template_path,
            "template_installed": self.template_installed,
            "document_path": self.document_path,
            "document_installed": self.document_installed,
            "unit_path": self.unit_path,
            "unit_installed": self.unit_installed,
            "service_enabled": self.service_enabled,
            "ledger_path": self.ledger_path,
            "ledger_schema_version": self.ledger_schema_version,
            "backend_status": self.backend_status,
            "backend_reasons": list(self.backend_reasons),
            "service_host_status": self.service_host_status,
            "service_host_reasons": list(self.service_host_reasons),
        }


def probe(
    *,
    home: Path | None = None,
    runtime_root: Path | None = None,
    ledger_path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    backend_report: Any | None = None,
    host_report: ServiceHostReport | None = None,
) -> ServiceProbe:
    """Return the read-only service/schema/backend probe.

    Nothing is written, enabled, started, or provisioned. ``backend_report``
    and ``host_report`` are injection seams for tests; production callers omit
    them and the pure :func:`lib.supervisor.authority.detect_backend` and
    :func:`service_host_capability` run.
    """
    resolution_home = _home(home)
    resolved_runtime = (
        Path(runtime_root) if runtime_root is not None else installed_runtime_root(resolution_home)
    )
    template = resolved_runtime / INSTALLED_TEMPLATE_RELPATH
    document = resolved_runtime / INSTALLED_DOCUMENT_RELPATH
    unit = resolution_home.joinpath(*_SYSTEMD_USER_UNIT_PARTS) / RENDERED_UNIT_NAME

    if ledger_path is None:
        ledger_path = default_ledger_path(env)
    ledger = Path(ledger_path) if ledger_path is not None else None

    report = backend_report if backend_report is not None else _authority().detect_backend()
    status = getattr(report, "status", "unknown")
    reasons = tuple(getattr(report, "reasons", ()) or ())

    host = host_report if host_report is not None else service_host_capability(env=env)

    return ServiceProbe(
        template_path=str(template),
        template_installed=template.is_file(),
        document_path=str(document),
        document_installed=document.is_file(),
        unit_path=str(unit),
        unit_installed=unit.is_file(),
        ledger_path=str(ledger) if ledger is not None else None,
        ledger_schema_version=read_ledger_schema_version(ledger),
        backend_status=str(status),
        backend_reasons=reasons,
        service_host_status=str(host.status),
        service_host_reasons=tuple(host.reasons),
    )


def doctor_check(
    repo: Path | None = None,
    *,
    home: Path | None = None,
    runtime_root: Path | None = None,
    ledger_path: Path | str | None = None,
    backend_report: Any | None = None,
) -> tuple[bool, str, str]:
    """Return the doctor ``(ok, label, remediation)`` tuple for the service.

    The check is informational: an uninstalled, unconfigured, or unsupported
    service state is reported by :func:`print_detail` and never fails the
    doctor run, so an operator who has not enabled supervision stays green.
    """
    probe(
        home=home,
        runtime_root=runtime_root,
        ledger_path=ledger_path,
        backend_report=backend_report,
    )
    return (True, CHECK_LABEL, "")


def print_detail(
    repo: Path | None = None,
    *,
    home: Path | None = None,
    runtime_root: Path | None = None,
    ledger_path: Path | str | None = None,
    backend_report: Any | None = None,
) -> None:
    """Print the extended read-only service/schema/backend detail dump."""
    state = probe(
        home=home,
        runtime_root=runtime_root,
        ledger_path=ledger_path,
        backend_report=backend_report,
    )
    print(f"    \u2022 service unit template: {'installed' if state.template_installed else 'not installed'} ({state.template_path})")
    print(f"    \u2022 provisioning document: {'installed' if state.document_installed else 'not installed'} ({state.document_path})")
    print(f"    \u2022 rendered service unit: {'present' if state.unit_installed else 'not present'} ({state.unit_path})")
    if state.ledger_schema_version is not None:
        print(f"    \u2022 supervisor ledger schema: version {state.ledger_schema_version} ({state.ledger_path})")
    else:
        print(f"    \u2022 supervisor ledger schema: no ledger present ({state.ledger_path or 'unresolved'})")
    print(f"    \u2022 isolation backend: {state.backend_status}")
    for reason in state.backend_reasons:
        print(f"      - {reason}")
    print(f"    \u2022 service host prerequisites: {state.service_host_status}")
    for reason in state.service_host_reasons:
        print(f"      - {reason}")


def activation_gate(
    *,
    report: Any | None = None,
    host_report: ServiceHostReport | None = None,
    host_kwargs: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """The documented fail-closed activation gate for the packaged service.

    Composes two independently-checked prerequisites before any enablement can
    proceed:

    1. :func:`service_host_capability` — a read-only check that this host has a
       supported systemd user manager and an OpenCode session bridge. Either
       missing prerequisite raises the named
       :class:`lib.supervisor.authority.UnsupportedHostError`; no other service
       manager or session bridge is substituted.
    2. :func:`lib.supervisor.authority.require_authority_backend` — backend
       detection plus the mandatory activation probe. An unavailable backend
       raises the same named unsupported-host error; a failing probe raises the
       named ``ActivationProbeError``.

    ``host_report``/``host_kwargs`` are injection seams for the read-only host
    check; ``report`` and the remaining keyword arguments are forwarded to the
    authority gate. There is no weaker activation path.
    """
    host = (
        host_report
        if host_report is not None
        else service_host_capability(**(dict(host_kwargs or {})))
    )
    if host.status != HOST_AVAILABLE:
        raise _authority().UnsupportedHostError(
            "no supported service host: " + "; ".join(host.reasons)
        )
    authority_kwargs = dict(kwargs)
    if report is not None:
        authority_kwargs["report"] = report
    return _authority().require_authority_backend(**authority_kwargs)


__all__ = [
    "CHECK_LABEL",
    "HOST_AVAILABLE",
    "HOST_UNSUPPORTED",
    "INSTALLED_DOCUMENT_RELPATH",
    "INSTALLED_TEMPLATE_RELPATH",
    "RENDERED_UNIT_NAME",
    "ServiceHostReport",
    "ServiceProbe",
    "activation_gate",
    "default_ledger_path",
    "document_path",
    "doctor_check",
    "installed_runtime_root",
    "installed_unit_path",
    "print_detail",
    "probe",
    "read_ledger_schema_version",
    "service_host_capability",
    "template_path",
]
