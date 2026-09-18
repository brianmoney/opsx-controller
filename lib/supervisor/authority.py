"""Operator authority boundary: backend detection, enforcement, and probe.

This module owns the single fail-closed gate that every supervision
enablement path must pass, plus the pure pieces that gate is built from:

- the three-principal model (operator, trusted service, worker) as data,
  including the distinctness rule that rejects a collapsed configuration;
- pure, unprivileged, side-effect-free backend capability detection that
  reports ``available`` / ``unprovisioned`` / ``unsupported`` with reasons,
  including validation of the explicit service-owned store-file contract and
  the worker-safe ownership of its mutable parent chain;
- the pure enforcement decision that answers whether a worker-domain write to
  a real target would be denied by filesystem ownership, mode, *and* any
  POSIX ACL, so a named ACL grant to the worker cannot masquerade as a safe
  mode;
- the mandatory activation probe: spawn a real subprocess under the worker
  principal and require it to prove, with execution evidence bound to the real
  store identity, that its effective identity is the worker's and that its
  attempt to open the authority store for writing failed with ``EACCES``.

Design rules enforced here:

- Standard library only, and no import of another runtime package. The
  trusted-location rule and canonicalization are reused from
  :mod:`lib.supervisor.ledger` through the module object (never a name import)
  so a deployment missing that module fails loudly rather than silently
  weakening the boundary.
- The store target is canonicalized before it is validated: a symlink,
  relative spelling, or ``..`` cannot conceal a worktree or a worker-owned
  location from the trusted-location rule or the ancestry checks.
- The default store target is *service-owned*: it is derived from the service
  principal's home (or a system service directory when that principal is not
  provisioned). It never follows the invoking user's home.
- The store's mutable parent chain is validated for service ownership (or
  root, the trusted system owner) and worker write denial — evaluated against
  the POSIX ACL as well as the mode bits — so a worker-writable directory
  cannot replace the protected store after the probe.
- Probe launchers are resolved only from trusted absolute locations with
  owner/mode/ACL/ancestry checks; ambient ``PATH``, bare names, and untrusted
  helper paths are refused. The launcher is canonicalized and only the
  canonical verified file is both validated and executed, so a symlink swapped
  after validation cannot redirect the identity switch.
- The probe child runs with a scrubbed environment and in Python isolated
  mode (``-I -S``), so ``PYTHONPATH``, ``sitecustomize``/``usercustomize``, and
  other startup hooks cannot forge the execution evidence.
- Detection creates nothing and writes nothing. It never provisions accounts,
  installs units, or opens the authority store for writing.
- ``require_authority_backend`` composes detection and the probe into one
  fail-closed gate. There is no weaker substitution and no no-probe route:
  unavailable hosts raise :class:`UnsupportedHostError`, a failing or
  unproven probe raises :class:`ActivationProbeError`, and nothing is ever
  provisioned.
"""

from __future__ import annotations

import errno
import json
import os
import shlex
import shutil
import socket
import stat as stat_module
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from lib.supervisor import ledger

# The one selected backend. Containers/namespaces and self-restriction are
# explicitly not the baseline (see core/plan-supervision.md).
BACKEND_NAME = "linux-isolated-principals"

STATUS_AVAILABLE = "available"
STATUS_UNPROVISIONED = "unprovisioned"
STATUS_UNSUPPORTED = "unsupported"

# Environment keys an operator may set to describe a provisioned host. Their
# absence is not an error; the defaults below are the documented baseline names.
SERVICE_PRINCIPAL_ENV = "OPSX_SUPERVISOR_SERVICE_PRINCIPAL"
WORKER_PRINCIPAL_ENV = "OPSX_SUPERVISOR_WORKER_PRINCIPAL"
# The store contract is a *file*. OPSX_SUPERVISOR_STATE_FILE names the
# service-owned protected file the probe and detection inspect.
STORE_FILE_ENV = "OPSX_SUPERVISOR_STATE_FILE"
# Backwards-compatible alias for the same key, kept importable.
STATE_PATH_ENV = STORE_FILE_ENV
SWITCH_CMD_ENV = "OPSX_SUPERVISOR_SWITCH_CMD"

DEFAULT_SERVICE_PRINCIPAL = "opsx-supervisor"
DEFAULT_WORKER_PRINCIPAL = "opsx-worker"

# Trusted launch directories for the restricted-spawn tools. A bare name is
# never executed: it is resolved against exactly these absolute directories,
# each of which must itself be trusted (root- or service-owned, not
# worker-writable). This removes ambient ``PATH`` and shell-hook spoofing.
TRUSTED_SWITCH_DIRS = (
    "/usr/bin",
    "/usr/sbin",
    "/bin",
    "/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
)

# Only these provisioned restricted-spawn programs may carry the probe into the
# worker domain. A custom helper is allowed only by absolute path, in a
# trusted directory with trusted ownership/mode.
ALLOWED_SWITCH_EXECUTABLES = ("runuser", "setpriv")

# The store's mutable parent chain may be owned by the service principal or by
# the root trust root; anything else (including the worker) is rejected.
TRUSTED_PARENT_UIDS: tuple[int, ...] = (0,)

# Environment keys kept in the probe child's otherwise-scrubbed environment.
# Everything else is dropped so the caller cannot inject PYTHONPATH/startup
# hooks. ``os.environ`` is not inherited wholesale.
_PROBE_PASSTHROUGH_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ")

# POSIX ACL access xattr. Ownership and mode bits alone do not express a
# *named* grant, so a directory that looks worker-unwritable can still be
# worker-writable through an ACL. The boundary refuses any extended access ACL
# on a mutable store/launcher parent (fail closed) and additionally inspects
# the ACL for an explicit worker grant.
_POSIX_ACL_ACCESS_XATTR = "system.posix_acl_access"
_NO_ACL_ERRNOS = tuple(
    code
    for code in (
        getattr(errno, "ENODATA", None),
        getattr(errno, "ENOATTR", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOENT", None),
        getattr(errno, "ENOTDIR", None),
    )
    if code is not None
)
_POSIX_ACL_XATTR_VERSION = 0x0002
# Linux wire layout (``linux/posix_acl_xattr.h``): a single little-endian
# ``u32`` version header followed by fixed-size entries
# ``struct posix_acl_xattr_entry { __le16 e_tag; __le16 e_perm; __le32 e_id; }``.
# There is no entry-count field; the count is derived from the payload length.
_POSIX_ACL_XATTR_HEADER_SIZE = 4
_POSIX_ACL_XATTR_ENTRY_SIZE = 8
_ACL_UNDEFINED_ID = 0xFFFFFFFF
_ACL_USER_OBJ = 0x01
_ACL_USER = 0x02
_ACL_GROUP_OBJ = 0x04
_ACL_GROUP = 0x08
_ACL_MASK = 0x10
_ACL_OTHER = 0x20
_VALID_ACL_TAGS = frozenset(
    {_ACL_USER_OBJ, _ACL_USER, _ACL_GROUP_OBJ, _ACL_GROUP, _ACL_MASK, _ACL_OTHER}
)

PROVISIONING_POINTER = (
    "provision a dedicated service principal and a dedicated unprivileged "
    "worker principal manually; see core/plan-supervision.md "
    "('Operator authority boundary') for the host-specific steps"
)

# Probe exit codes. The probe script also emits a JSON evidence line; the exit
# status alone is never accepted as proof.
PROBE_EXIT_DENIED = 3
PROBE_EXIT_WROTE = 0
PROBE_EXIT_WRONG_UID = 5
_PROBE_EXIT_OTHER_ERROR = 4

# The probe runs under the worker principal and attempts the forbidden open.
# It proves its effective identity first, then reports the real kernel result
# as one JSON line. ``EACCES`` exits 3; any other OSError exits 4; a success
# exits 0; a wrong effective uid exits 5 without touching the store. The file
# is opened O_WRONLY only (no O_CREAT), so a missing or denied target never
# creates anything as a side effect of probing.
#
# ``sys.argv[3]`` is a per-invocation nonce that the parent also knows and
# checks, so a recorded or pre-baked evidence line cannot be replayed. The
# child is launched with ``-I -S`` (isolated, no site) in a scrubbed
# environment, so PYTHONPATH/sitecustomize hooks cannot intercept this script.
PROBE_SCRIPT = (
    "import errno, json, os, sys\n"
    "path = sys.argv[1]\n"
    "expected_uid = int(sys.argv[2])\n"
    "nonce = sys.argv[3]\n"
    "result = {'uid': os.geteuid(), 'expected_uid': expected_uid, 'nonce': nonce}\n"
    "if os.geteuid() != expected_uid:\n"
    "    result['outcome'] = 'wrong_uid'\n"
    "    print(json.dumps(result))\n"
    "    sys.exit(5)\n"
    "try:\n"
    "    fd = os.open(path, os.O_WRONLY)\n"
    "except OSError as exc:\n"
    "    result['errno'] = exc.errno\n"
    "    result['outcome'] = 'denied' if exc.errno == errno.EACCES else 'error'\n"
    "    print(json.dumps(result))\n"
    "    sys.exit(3 if exc.errno == errno.EACCES else 4)\n"
    "os.close(fd)\n"
    "result['outcome'] = 'wrote'\n"
    "print(json.dumps(result))\n"
    "sys.exit(0)\n"
)


class AuthorityError(Exception):
    """Base class for operator-authority-boundary failures."""


class UnsupportedHostError(AuthorityError):
    """The host cannot provide the isolation backend; enabling is refused."""


class ActivationProbeError(AuthorityError):
    """The activation probe failed, could not run, or was indeterminate."""


# ---------------------------------------------------------------------------
# Three-principal model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """One OS identity in the three-principal model.

    ``uid`` is ``None`` when the named principal does not exist on this host;
    existence is a provisioning fact, never inferred.
    """

    role: str
    name: str
    uid: int | None

    @property
    def exists(self) -> bool:
        return self.uid is not None


@dataclass(frozen=True)
class PrincipalSet:
    """The operator, service, and worker principals with the distinctness rule."""

    operator: Principal
    service: Principal
    worker: Principal

    def as_list(self) -> list[Principal]:
        return [self.operator, self.service, self.worker]

    def missing(self) -> list[Principal]:
        """Principals that do not exist on this host."""
        return [principal for principal in self.as_list() if not principal.exists]

    @property
    def distinct(self) -> bool:
        """True only when all three exist as three pairwise-distinct uid values."""
        uids = [principal.uid for principal in self.as_list()]
        if any(uid is None for uid in uids):
            return False
        return len(set(uids)) == len(uids)


def _lookup_uid(name: str) -> int | None:
    """Resolve a principal name to its numeric uid, or ``None`` when absent."""
    try:
        import pwd  # Unix-only; absent on non-POSIX hosts.
    except ImportError:  # pragma: no cover - non-POSIX hosts
        return None
    try:
        return int(pwd.getpwnam(name).pw_uid)
    except (KeyError, OSError):  # pragma: no cover - depends on host state
        return None


def _current_user_name() -> str:
    try:
        import pwd
    except ImportError:  # pragma: no cover - non-POSIX hosts
        return os.environ.get("USER") or str(os.getuid())
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):  # pragma: no cover - depends on host state
        return os.environ.get("USER") or str(os.getuid())


def _default_gid_resolver(name: str) -> list[int]:
    """Return the gids *name* belongs to, or ``[]`` when unresolvable."""
    try:
        import grp
        import pwd
    except ImportError:  # pragma: no cover - non-POSIX hosts
        return []
    try:
        entry = pwd.getpwnam(name)
    except (KeyError, OSError):  # pragma: no cover - depends on host state
        return []
    gids = {int(entry.pw_gid)}
    try:
        for group in grp.getgrall():
            if name in group.gr_mem:
                gids.add(int(group.gr_gid))
    except OSError:  # pragma: no cover - depends on host state
        pass
    return sorted(gids)


def resolve_principal(
    role: str,
    name: str,
    *,
    resolver: Callable[[str], int | None] | None = None,
) -> Principal:
    """Resolve *name* to a :class:`Principal`, using *resolver* when supplied."""
    lookup = resolver or _lookup_uid
    return Principal(role=role, name=name, uid=lookup(name))


def principal_set_from_environment(
    *,
    env: Mapping[str, str] | None = None,
    resolver: Callable[[str], int | None] | None = None,
    operator_name: str | None = None,
) -> PrincipalSet:
    """Build the three-principal set from the environment and OS account facts."""
    environment = os.environ if env is None else env
    operator = operator_name or _current_user_name()
    service = environment.get(SERVICE_PRINCIPAL_ENV) or DEFAULT_SERVICE_PRINCIPAL
    worker = environment.get(WORKER_PRINCIPAL_ENV) or DEFAULT_WORKER_PRINCIPAL
    return PrincipalSet(
        operator=resolve_principal("operator", operator, resolver=resolver),
        service=resolve_principal("service", service, resolver=resolver),
        worker=resolve_principal("worker", worker, resolver=resolver),
    )


# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityReport:
    """The pure capability report detection produces."""

    status: str
    reasons: tuple[str, ...]
    backend: str = BACKEND_NAME
    principals: PrincipalSet | None = None
    state_path: str | None = None
    provisioning_pointer: str = PROVISIONING_POINTER

    @property
    def available(self) -> bool:
        return self.status == STATUS_AVAILABLE

    def as_dict(self) -> dict[str, Any]:
        principals = None
        if self.principals is not None:
            principals = {
                principal.role: {"name": principal.name, "uid": principal.uid}
                for principal in self.principals.as_list()
            }
        return {
            "backend": self.backend,
            "status": self.status,
            "reasons": list(self.reasons),
            "principals": principals,
            "state_path": self.state_path,
            "provisioning_pointer": self.provisioning_pointer,
        }


def _service_home(service_uid: int | None) -> Path | None:
    """Return the service principal's home directory, or ``None`` when unknown."""
    if service_uid is None:
        return None
    try:
        import pwd
    except ImportError:  # pragma: no cover - non-POSIX hosts
        return None
    try:
        entry = pwd.getpwuid(int(service_uid))
    except (KeyError, OSError):  # pragma: no cover - depends on host state
        return None
    home = entry.pw_dir
    if not home:
        return None
    return Path(home)


def default_state_path(
    *,
    env: Mapping[str, str] | None = None,
    service_uid: int | None = None,
    service_home: os.PathLike[str] | str | None = None,
) -> Path:
    """Return the canonical service-owned authority-store *file* for this host.

    An explicit ``OPSX_SUPERVISOR_STATE_FILE`` wins (canonicalized before use).
    Otherwise the default is derived from the *service* principal's home
    directory — never the invoking user's home, which a caller could make
    writable or point at a worktree. When the service principal is not
    provisioned, a system service directory (``/var/lib``) is used, which is
    root-owned and therefore already worker-safe. The result is canonicalized:
    symlinks and ``..`` are resolved before any validation or probe.

    ``service_home`` is an explicit provisioning/test seam and takes precedence
    over the ``pwd`` lookup; the invoking user's home is never consulted (the
    ledger's ``default_ledger_path`` helper is intentionally not used).
    """
    environment = os.environ if env is None else env
    configured = environment.get(STORE_FILE_ENV)
    if configured:
        return ledger._canonical(configured)
    service_home = (
        Path(service_home)
        if service_home is not None
        else _service_home(service_uid)
    )
    if service_home is not None:
        target = (
            service_home
            / ".local"
            / "share"
            / "opsx-controller"
            / "supervisor"
            / "supervisor.sqlite3"
        )
    else:
        # No service principal: fall back to a root-owned system directory,
        # which is worker-safe even before provisioning.
        target = (
            Path("/var/lib")
            / "opsx-controller"
            / "supervisor"
            / "supervisor.sqlite3"
        )
    return ledger._canonical(target)


def _default_trusted_location_check(path: Path) -> None:
    """Reuse the ledger's canonical trusted-location rule (path semantics only)."""
    ledger._assert_trusted_location(path)


def _store_contract_reasons(
    path: Path,
    *,
    service_uid: int | None,
    worker_uid: int,
    worker_gids: Iterable[int],
    stat_fn: Callable[[Path], os.stat_result] | None = None,
    ancestor_stat: Callable[[Path], os.stat_result] | None = None,
    worker_denied_check: Callable[[os.stat_result], bool] | None = None,
    acl_reader: Callable[[Path], bytes | None] | None = None,
) -> list[str]:
    """Validate the explicit service-owned store-file contract and its ancestry.

    The target must be a canonical existing regular file whose full mutable
    parent chain is owned by the service principal (or root) and denies the
    worker principal a write, and whose own mode denies the worker a write.
    Validating the ancestors blocks the replacement attack where the protected
    leaf is swapped after the probe because a worker-writable directory holds
    it. Every failing condition produces a reason rather than being skipped.
    """
    read_stat = stat_fn or os.stat

    reasons = _ancestry_reasons(
        path,
        service_uid=service_uid,
        worker_uid=worker_uid,
        worker_gids=worker_gids,
        stat_fn=ancestor_stat or os.stat,
        worker_denied_check=worker_denied_check,
        acl_reader=acl_reader,
    )
    if reasons:
        return reasons

    try:
        result = read_stat(path)
    except FileNotFoundError:
        return [
            f"authority-store file {path} does not exist; the service principal "
            "must provision the protected store file"
        ]
    except OSError as exc:
        return [f"authority-store file {path} could not be inspected: {exc}"]

    mode = int(result.st_mode)
    if stat_module.S_ISDIR(mode):
        return [
            f"authority-store target {path} is a directory; the contract requires "
            "an explicit service-owned store file, not a directory"
        ]
    if not stat_module.S_ISREG(mode):
        return [f"authority-store target {path} is not a regular file"]

    reasons = []
    if service_uid is not None and int(result.st_uid) != service_uid:
        reasons.append(
            f"authority-store file {path} is owned by uid {result.st_uid}, not the "
            f"service principal uid {service_uid}"
        )
    if worker_denied_check is not None:
        denied = worker_denied_check(result)
    else:
        try:
            denied = worker_write_denied(
                result,
                worker_uid=worker_uid,
                worker_gids=worker_gids,
                acl_reader=acl_reader,
                path=path,
            ).denied
        except OSError as exc:
            return [
                f"authority-store file {path} has an uninspectable extended ACL: "
                f"{exc}"
            ]
    if not denied:
        reasons.append(
            f"authority-store file {path} does not deny the worker principal write access"
        )
    return reasons


def _trusted_parent_owner(uid: int, *, service_uid: int | None) -> bool:
    """True when *uid* is an allowed owner for a store parent directory."""
    if uid == 0 or uid in TRUSTED_PARENT_UIDS:
        return True
    return service_uid is not None and uid == service_uid


def _ancestry_reasons(
    path: Path,
    *,
    service_uid: int | None,
    worker_uid: int,
    worker_gids: Iterable[int],
    stat_fn: Callable[[Path], os.stat_result],
    worker_denied_check: Callable[[os.stat_result], bool] | None,
    acl_reader: Callable[[Path], bytes | None] | None = None,
) -> list[str]:
    """Validate the mutable directory chain above the store target.

    Every directory from the target's parent up to the filesystem root must be
    owned by the root trust root or the service principal, and must deny the
    worker principal a write. Otherwise the worker could create or replace the
    protected leaf (or a parent symlink) between the probe and use. Denial is
    evaluated against the POSIX ACL as well as the mode bits, so a *named* ACL
    grant to the worker cannot hide behind safe-looking permissions.
    """
    gids = list(worker_gids)
    reasons: list[str] = []
    for ancestor in path.parents:
        try:
            result = stat_fn(ancestor)
        except FileNotFoundError:
            # A non-existent ancestor is created by provisioning; it cannot be
            # worker-replaceable if it does not exist yet, but the leaf check
            # will independently fail when it is missing.
            break
        except OSError as exc:
            reasons.append(
                f"authority-store parent {ancestor} could not be inspected: {exc}"
            )
            continue
        if not stat_module.S_ISDIR(int(result.st_mode)):
            reasons.append(f"authority-store parent {ancestor} is not a directory")
            continue
        owner = int(result.st_uid)
        if not _trusted_parent_owner(owner, service_uid=service_uid):
            reasons.append(
                f"authority-store parent {ancestor} is owned by uid {owner}, which is "
                "neither the root trust root nor the service principal"
            )
            continue
        if worker_denied_check is not None:
            denied = worker_denied_check(result)
        else:
            try:
                denied = worker_write_denied(
                    result,
                    worker_uid=worker_uid,
                    worker_gids=gids,
                    acl_reader=acl_reader,
                    path=ancestor,
                ).denied
            except OSError as exc:
                # An ACL we cannot read is an unproven denial: fail closed.
                reasons.append(
                    f"authority-store parent {ancestor} has an uninspectable "
                    f"extended ACL: {exc}"
                )
                continue
        if not denied:
            reasons.append(
                f"authority-store parent {ancestor} is writable by the worker "
                "principal, so the protected store could be replaced"
            )
    return reasons


def detect_backend(
    *,
    principals: PrincipalSet | None = None,
    system: str | None = None,
    peer_credential_supported: bool | None = None,
    state_path: os.PathLike[str] | str | None = None,
    trusted_location_check: Callable[[Path], None] | None = None,
    store_stat: Callable[[Path], os.stat_result] | None = None,
    ancestor_stat: Callable[[Path], os.stat_result] | None = None,
    resolver: Callable[[str], int | None] | None = None,
    gid_resolver: Callable[[str], Sequence[int]] | None = None,
    acl_reader: Callable[[Path], bytes | None] | None = None,
    env: Mapping[str, str] | None = None,
) -> CapabilityReport:
    """Compute the backend capability report from observable host facts.

    Pure, unprivileged, and side-effect free: it creates no accounts, installs
    no units, writes nothing, and never requires the service identity. Every
    input is injectable so fixtures can simulate a supported, unprovisioned, or
    unsupported host without touching the real one.

    The state target is canonicalized before any check. The default target is
    derived from the *service* principal, never the invoking user, so a caller
    cannot point the boundary at a home it controls.
    """
    platform = sys.platform if system is None else system
    peer_ok = (
        hasattr(socket, "SO_PEERCRED")
        if peer_credential_supported is None
        else bool(peer_credential_supported)
    )
    platform_ok = str(platform).startswith("linux")

    reasons: list[str] = []
    if not platform_ok:
        reasons.append(
            f"platform '{platform}' is not Linux; the isolated-principal backend "
            "requires Linux peer credentials"
        )
    if not peer_ok:
        reasons.append(
            "the platform does not expose SO_PEERCRED, so peer-credential "
            "authentication is unavailable"
        )

    resolved_principals = principals or principal_set_from_environment(
        env=env, resolver=resolver
    )
    missing = resolved_principals.missing()
    if missing:
        reasons.append(
            "principal(s) not provisioned: "
            + ", ".join(f"{principal.role} '{principal.name}'" for principal in missing)
        )
    elif not resolved_principals.distinct:
        reasons.append(
            "operator, service, and worker are not three distinct OS identities; "
            "a collapsed configuration cannot enforce the boundary"
        )

    # Canonicalize before validation: a symlink, relative spelling, or ``..``
    # must not conceal a worktree or worker-owned location from the path rule
    # or the ancestry checks.
    if state_path is not None:
        resolved_state = ledger._canonical(state_path)
    else:
        resolved_state = default_state_path(
            env=env, service_uid=resolved_principals.service.uid
        )
    check = trusted_location_check or _default_trusted_location_check
    try:
        check(resolved_state)
    except ledger.TrustedLocationError as exc:
        reasons.append(f"authority-store location is not trusted: {exc}")

    # Validate the explicit store-file contract only once the worker principal
    # is known; without a worker uid the denial property cannot be evaluated.
    if resolved_principals.worker.uid is not None:
        gids = list(
            (gid_resolver or _default_gid_resolver)(resolved_principals.worker.name)
        )
        reasons.extend(
            _store_contract_reasons(
                resolved_state,
                service_uid=resolved_principals.service.uid,
                worker_uid=resolved_principals.worker.uid,
                worker_gids=gids,
                stat_fn=store_stat,
                ancestor_stat=ancestor_stat,
                acl_reader=acl_reader,
            )
        )

    if reasons:
        status = (
            STATUS_UNSUPPORTED
            if (not platform_ok or not peer_ok)
            else STATUS_UNPROVISIONED
        )
    else:
        status = STATUS_AVAILABLE

    return CapabilityReport(
        status=status,
        reasons=tuple(reasons),
        principals=resolved_principals,
        state_path=str(resolved_state),
    )


# ---------------------------------------------------------------------------
# Enforcement decision (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteDecision:
    """The pure result of the worker-domain write enforcement decision."""

    denied: bool
    reason: str

    @property
    def allowed(self) -> bool:
        return not self.denied


def decide_worker_write(
    *,
    st_mode: int,
    st_uid: int,
    st_gid: int,
    worker_uid: int,
    worker_gids: Iterable[int],
) -> WriteDecision:
    """Decide whether a worker-domain write to a target would be denied.

    Applies the standard POSIX owner/group/other write-bit rule to the target's
    real ``stat`` data and the worker principal's uid/gids. Root bypasses the
    permission bits, so a root worker is never reported as denied.
    """
    gids = set(worker_gids)
    if worker_uid == 0:
        return WriteDecision(
            denied=False,
            reason="worker principal is root; filesystem permission bits do not deny it",
        )
    if worker_uid == st_uid:
        bits = (st_mode >> 6) & 0o7
        who = "owner"
    elif st_gid in gids:
        bits = (st_mode >> 3) & 0o7
        who = "group"
    else:
        bits = st_mode & 0o7
        who = "other"
    if bits & 0o2:
        return WriteDecision(
            denied=False,
            reason=(
                f"worker principal (uid {worker_uid}) has {who} write permission "
                f"on the target"
            ),
        )
    return WriteDecision(
        denied=True,
        reason=(
            f"target mode {st_mode & 0o777:04o} denies write to the worker "
            f"principal (uid {worker_uid}, {who} class)"
        ),
    )


def decide_worker_write_for_stat(
    stat_result: os.stat_result,
    *,
    worker_uid: int,
    worker_gids: Iterable[int],
) -> WriteDecision:
    """Convenience wrapper over :func:`decide_worker_write` for real stat data."""
    return decide_worker_write(
        st_mode=stat_result.st_mode,
        st_uid=stat_result.st_uid,
        st_gid=stat_result.st_gid,
        worker_uid=worker_uid,
        worker_gids=worker_gids,
    )


def _acl_structure_is_valid(entries: Sequence[tuple[int, int, int]]) -> bool:
    """Validate the kernel ordering/structure of parsed access-ACL entries.

    Byte alignment, a correct version, and known tags are not enough: the
    entries must form the ordered access-ACL layout the kernel enforces in
    ``posix_acl_valid()``. This walks the entries with a small state/index
    machine and requires:

    - permission nibbles use only the ``rwx`` bits;
    - ``USER_OBJ`` first, then named users, then ``GROUP_OBJ``, then named
      groups, then an optional ``MASK``, then ``OTHER`` last;
    - the ``USER_OBJ``/``GROUP_OBJ``/``MASK``/``OTHER`` singleton identifiers
      are ``ACL_UNDEFINED_ID``;
    - named users/groups carry concrete, non-duplicate identifiers;
    - a named entry implies a ``MASK``; and
    - no duplicate, out-of-order, or trailing entry.

    Valid minimal ACLs (``USER_OBJ, GROUP_OBJ, OTHER``) and mask-only extended
    ACLs (``USER_OBJ, GROUP_OBJ, MASK, OTHER``) are preserved because Linux
    accepts them. Anything else is structurally malformed and must not be
    trusted, so callers fail closed.
    """
    state = _ACL_USER_OBJ
    needs_mask = False
    mask_seen = False
    seen_user_ids: set[int] = set()
    seen_group_ids: set[int] = set()
    for tag, ident, perm in entries:
        if tag not in _VALID_ACL_TAGS or perm & ~0o7:
            return False
        if tag == _ACL_USER_OBJ:
            if state != _ACL_USER_OBJ or ident != _ACL_UNDEFINED_ID:
                return False
            state = _ACL_USER
        elif tag == _ACL_USER:
            if state != _ACL_USER or ident == _ACL_UNDEFINED_ID:
                return False
            if ident in seen_user_ids:
                return False
            seen_user_ids.add(ident)
            needs_mask = True
        elif tag == _ACL_GROUP_OBJ:
            if state != _ACL_USER or ident != _ACL_UNDEFINED_ID:
                return False
            state = _ACL_GROUP
        elif tag == _ACL_GROUP:
            if state != _ACL_GROUP or ident == _ACL_UNDEFINED_ID:
                return False
            if ident in seen_group_ids:
                return False
            seen_group_ids.add(ident)
            needs_mask = True
        elif tag == _ACL_MASK:
            if state != _ACL_GROUP or ident != _ACL_UNDEFINED_ID:
                return False
            mask_seen = True
            state = _ACL_OTHER
        elif tag == _ACL_OTHER:
            if ident != _ACL_UNDEFINED_ID:
                return False
            if state != _ACL_OTHER and not (
                state == _ACL_GROUP and not needs_mask
            ):
                return False
            state = 0
    if state != 0:
        return False
    if needs_mask and not mask_seen:
        return False
    return True


def parse_posix_acl_access(raw: bytes) -> list[tuple[int, int, int]]:
    """Parse a Linux ``system.posix_acl_access`` value into ``(tag, id, perm)``.

    The on-disk/xattr format (``linux/posix_acl_xattr.h``) is a single
    little-endian ``u32`` version header followed by zero or more fixed-size
    8-byte entries. There is *no* entry-count field: the entry count is derived
    from the payload length, and a payload whose length is not
    ``4 + 8 * n`` is malformed.

    A value that is merely well-aligned, correctly versioned, and uses known
    tags is still malformed when its entries do not form a complete, ordered
    access ACL (see :func:`_acl_structure_is_valid`). Any malformed value —
    bad length, wrong version, out-of-range tag, or invalid layout — yields an
    empty list, and callers that care fail closed rather than falling back to
    the mode bits.
    """
    entries: list[tuple[int, int, int]] = []
    if len(raw) < _POSIX_ACL_XATTR_HEADER_SIZE:
        return []
    if (len(raw) - _POSIX_ACL_XATTR_HEADER_SIZE) % _POSIX_ACL_XATTR_ENTRY_SIZE:
        return []
    try:
        (version,) = struct.unpack_from("<I", raw, 0)
    except struct.error:
        return []
    if version != _POSIX_ACL_XATTR_VERSION:
        return []
    offset = _POSIX_ACL_XATTR_HEADER_SIZE
    while offset < len(raw):
        try:
            tag, perm, ident = struct.unpack_from("<HHI", raw, offset)
        except struct.error:
            return []
        entries.append((int(tag), int(ident), int(perm)))
        offset += _POSIX_ACL_XATTR_ENTRY_SIZE
    if not _acl_structure_is_valid(entries):
        return []
    return entries


def acl_worker_write_decision(
    raw: bytes | None,
    *,
    worker_uid: int,
    worker_gids: Iterable[int],
    st_uid: int | None = None,
    st_gid: int | None = None,
) -> WriteDecision | None:
    """Return the ACL-based worker-write decision, or ``None`` when inconclusive.

    Returns ``None`` only when *raw* is ``None`` (the path has no extended
    access ACL, so there is nothing to add to the ownership/mode decision).
    Every present payload — including an empty ``b''`` or a short/malformed
    one — is an ACL that could not be evaluated, and is reported as an
    indeterminate grant so the caller fails closed rather than falling back to
    the mode bits.

    It walks the ACL entries in kernel evaluation order — owner, named users,
    the group class (owning group plus matching named groups, unioned and then
    masked), then other — and decides whether the worker principal is granted
    write. Any group-class match pre-empts the other entry, exactly as the
    kernel does, so a present but unparseable ACL, or one with no evaluable
    entry, fails closed rather than trusting a mode-bit-only answer.
    """
    if raw is None:
        return None
    entries = parse_posix_acl_access(raw)
    if not entries:
        return WriteDecision(
            denied=False,
            reason="extended POSIX ACL is present but could not be parsed; failing closed",
        )
    gids = set(worker_gids)
    mask: int | None = None
    owner_perm: int | None = None
    group_perm: int | None = None
    other_perm: int | None = None
    named_user_perm: int | None = None
    named_group_perms: list[int] = []
    for tag, ident, perm in entries:
        if tag == _ACL_USER_OBJ:
            owner_perm = perm
        elif tag == _ACL_USER and ident == worker_uid:
            named_user_perm = perm
        elif tag == _ACL_GROUP_OBJ:
            group_perm = perm
        elif tag == _ACL_GROUP and ident in gids:
            named_group_perms.append(perm)
        elif tag == _ACL_MASK:
            mask = perm
        elif tag == _ACL_OTHER:
            other_perm = perm

    def effective(perm: int) -> int:
        # The mask filters named-user, owning-group, and named-group entries;
        # it does not apply to the owner or other entries (kernel semantics).
        return perm if mask is None else perm & mask

    # Root bypasses ACL permission entries entirely.
    if worker_uid == 0:
        return WriteDecision(
            denied=False,
            reason="worker principal is root; ACL permission entries do not deny it",
        )
    # Kernel precedence: owner entry, then a named user entry for the worker,
    # then the group class (owning group and matching named groups, unioned),
    # then other.
    if st_uid is not None and worker_uid == st_uid and owner_perm is not None:
        granted = bool(owner_perm & 0o2)
        return WriteDecision(
            denied=not granted,
            reason=(
                "ACL owner entry grants the worker principal write access"
                if granted
                else "ACL owner entry denies the worker principal write"
            ),
        )
    if named_user_perm is not None:
        granted = bool(effective(named_user_perm) & 0o2)
        return WriteDecision(
            denied=not granted,
            reason=(
                "named ACL user entry grants the worker principal write access"
                if granted
                else "named ACL user entry does not grant the worker principal write"
            ),
        )
    # Group class: when the worker is in the owning group, that entry and every
    # matching named-group entry form one union, and the mask is applied to the
    # union (not entry-by-entry as an early return). Any group-class match
    # pre-empts the other entry, so the union may broaden a grant but never
    # falls back to other.
    group_class_perms: list[int] = []
    if st_gid is not None and st_gid in gids and group_perm is not None:
        group_class_perms.append(group_perm)
    group_class_perms.extend(named_group_perms)
    if group_class_perms:
        granted = any(effective(perm) & 0o2 for perm in group_class_perms)
        return WriteDecision(
            denied=not granted,
            reason=(
                "ACL group class grants the worker principal write access"
                if granted
                else "ACL group class denies the worker principal write"
            ),
        )
    if other_perm is not None:
        granted = bool(other_perm & 0o2)
        return WriteDecision(
            denied=not granted,
            reason=(
                "extended ACL other entry grants write access"
                if granted
                else "extended ACL does not grant the worker principal write"
            ),
        )
    # An extended ACL was present but contained no entry this evaluator
    # recognises: refuse to fall back to the mode bits, which the ACL overrides.
    return WriteDecision(
        denied=False,
        reason="extended POSIX ACL present but not evaluable; failing closed",
    )


def worker_write_denied(
    stat_result: os.stat_result,
    *,
    worker_uid: int,
    worker_gids: Iterable[int],
    acl_reader: Callable[[Path], bytes | None] | None = None,
    path: os.PathLike[str] | str | None = None,
) -> WriteDecision:
    """Decide worker write denial from mode bits *and* a POSIX ACL.

    This is the enforcement decision used for every mutable store and launcher
    parent. A named ACL grant to the worker overrides an apparently-safe mode,
    so the ACL is consulted first (defaulting to the real ``system.posix_acl_
    access`` xattr) and the mode decision is the fallback. An ACL that is
    present but unreadable/unparseable fails closed.
    """
    gids = list(worker_gids)
    reader = acl_reader if acl_reader is not None else _read_acl_xattr
    if path is not None:
        raw = reader(Path(path))
        acl_decision = acl_worker_write_decision(
            raw,
            worker_uid=worker_uid,
            worker_gids=gids,
            st_uid=int(stat_result.st_uid),
            st_gid=int(stat_result.st_gid),
        )
        if acl_decision is not None:
            return acl_decision
    return decide_worker_write_for_stat(
        stat_result, worker_uid=worker_uid, worker_gids=gids
    )


def _read_acl_xattr(path: Path) -> bytes | None:
    """Read the POSIX access ACL for *path*, or ``None`` when absent.

    ``ENODATA``/``ENOATTR`` mean no extended ACL (the mode bits are complete).
    Any other inspection failure is surfaced so the caller can fail closed.
    """
    getter = getattr(os, "getxattr", None)
    if getter is None:  # pragma: no cover - non-Linux / no xattr support
        return None
    try:
        return bytes(getter(path, _POSIX_ACL_ACCESS_XATTR))
    except OSError as exc:
        if exc.errno in _NO_ACL_ERRNOS:
            return None
        raise



# ---------------------------------------------------------------------------
# Activation probe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    """A passing activation-probe result. Failures raise instead."""

    outcome: str
    reason: str
    command: tuple[str, ...] = ()
    evidence: Mapping[str, Any] | None = None

    @property
    def passed(self) -> bool:
        return self.outcome == "denied"


def interpret_probe_exit(returncode: int | None) -> str:
    """Map a probe subprocess exit code to a coarse outcome.

    This is a pure helper only; the exit status alone is never accepted as
    proof. :func:`run_activation_probe` also requires the child's execution
    evidence (effective uid and real open result).
    """
    if returncode == PROBE_EXIT_DENIED:
        return "denied"
    if returncode == PROBE_EXIT_WROTE:
        return "wrote"
    return "indeterminate"


def _resolve_trusted_tool(name: str) -> str | None:
    """Resolve *name* to an absolute path using only trusted launch directories.

    Ambient ``PATH`` is deliberately not consulted: it is attacker-influenced in
    a worker domain. The lookup is restricted to the fixed trusted directories.
    """
    return shutil.which(name, path=os.pathsep.join(TRUSTED_SWITCH_DIRS))


def discover_switch_mechanism(
    worker_name: str,
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> list[str] | None:
    """Find the provisioned restricted-spawn mechanism, or ``None``.

    Order: an explicit ``OPSX_SUPERVISOR_SWITCH_CMD`` (``{user}`` substituted),
    then ``setpriv``, then ``runuser``. The lookup resolves to an absolute path
    using only trusted launch directories; a bare name is never returned, so an
    attacker-controlled ``PATH`` cannot shadow the launcher. The result is
    validated (trusted ownership and ancestry) before it is executed.
    """
    environment = os.environ if env is None else env
    lookup = which or _resolve_trusted_tool
    configured = (environment.get(SWITCH_CMD_ENV) or "").strip()
    if configured:
        parts = shlex.split(configured)
        # The environment may only *pin the path* of an allowlisted tool; it
        # may never inject an arbitrary program or its own arguments. The
        # final argv is always built here, so a worker-supplied wrapper or
        # option string cannot smuggle in a different interpreter.
        if not parts:
            return None
        candidate = parts[0].format(user=worker_name)
        base = os.path.basename(candidate)
        if base not in ALLOWED_SWITCH_EXECUTABLES:
            return None
        if os.path.isabs(candidate):
            path = candidate if os.path.isfile(candidate) else None
        else:
            path = lookup(base)
        if not path:
            return None
        if base == "setpriv":
            return [
                str(path),
                "--reuid", worker_name,
                "--regid", worker_name,
                "--clear-groups",
            ]
        return [str(path), "-u", worker_name, "--"]
    setpriv = lookup("setpriv")
    if setpriv:
        return [
            str(setpriv),
            "--reuid", worker_name,
            "--regid", worker_name,
            "--clear-groups",
        ]
    runuser = lookup("runuser")
    if runuser:
        return [str(runuser), "-u", worker_name, "--"]
    return None


def _canonical_launcher_path(executable: str) -> Path | None:
    """Canonicalize an absolute launcher path, or ``None`` for a non-absolute one.

    ``Path.resolve`` resolves the existing prefix, so a symlink in a trusted
    directory cannot conceal a worker-owned target. The canonical path is what
    gets stat-checked *and* executed; the pre-canonical spelling is never run.
    """
    path = Path(executable)
    if not path.is_absolute():
        return None
    return ledger._canonical(path)


def _trusted_launcher_reasons(
    path: Path,
    *,
    service_uid: int | None,
    worker_uid: int | None,
    worker_gids: Iterable[int],
    stat_fn: Callable[[Path], os.stat_result] | None = None,
    ancestor_stat: Callable[[Path], os.stat_result] | None = None,
    worker_denied_check: Callable[[os.stat_result], bool] | None = None,
    acl_reader: Callable[[Path], bytes | None] | None = None,
) -> list[str]:
    """Reasons the *canonical* switch executable is not part of the trusted base.

    The launcher performs the identity switch and then execs the probe
    interpreter, so it must be a root- or service-owned executable that the
    worker cannot write, sitting in a trusted (non-worker-writable) directory
    chain. The path passed here has already been canonicalized, so the checks
    apply to the real file that will be executed. A worker-owned helper in a
    worker-writable directory is refused.
    """
    read_stat = stat_fn or os.stat

    reasons: list[str] = []
    try:
        result = read_stat(path)
    except FileNotFoundError:
        return [f"restricted-spawn executable {path} does not exist"]
    except OSError as exc:
        return [f"restricted-spawn executable {path} could not be inspected: {exc}"]

    mode = int(result.st_mode)
    if not stat_module.S_ISREG(mode):
        reasons.append(f"restricted-spawn executable {path} is not a regular file")
    if not mode & 0o111:
        reasons.append(f"restricted-spawn executable {path} is not executable")

    owner = int(result.st_uid)
    if not _trusted_parent_owner(owner, service_uid=service_uid):
        reasons.append(
            f"restricted-spawn executable {path} is owned by uid {owner}, which is "
            "neither the root trust root nor the service principal"
        )
    if worker_uid is not None:
        if worker_denied_check is not None:
            denied = worker_denied_check(result)
        else:
            try:
                denied = worker_write_denied(
                    result,
                    worker_uid=worker_uid,
                    worker_gids=worker_gids,
                    acl_reader=acl_reader,
                    path=path,
                ).denied
            except OSError as exc:
                reasons.append(
                    f"restricted-spawn executable {path} has an uninspectable "
                    f"extended ACL: {exc}"
                )
                denied = False
        if not denied:
            reasons.append(
                f"restricted-spawn executable {path} is writable by the worker "
                "principal, so it could be replaced"
            )
        reasons.extend(
            _ancestry_reasons(
                path,
                service_uid=service_uid,
                worker_uid=worker_uid,
                worker_gids=worker_gids,
                stat_fn=ancestor_stat or os.stat,
                worker_denied_check=worker_denied_check,
                acl_reader=acl_reader,
            )
        )
    return reasons


def canonical_switch_mechanism(
    parts: Sequence[str],
    *,
    service_uid: int | None = None,
    worker_uid: int | None = None,
    worker_gids: Iterable[int] = (),
    stat_fn: Callable[[Path], os.stat_result] | None = None,
    ancestor_stat: Callable[[Path], os.stat_result] | None = None,
    worker_denied_check: Callable[[os.stat_result], bool] | None = None,
    acl_reader: Callable[[Path], bytes | None] | None = None,
) -> list[str]:
    """Return the trusted mechanism with its launcher replaced by its canonical path.

    Validates the launcher exactly like :func:`validate_switch_mechanism` and
    then rewrites ``argv[0]`` to the canonical, verified executable. Callers
    must execute *this* argv so a symlink swapped between validation and spawn
    cannot redirect execution at the pre-canonical spelling.
    """
    if not parts:
        raise ActivationProbeError(
            "the restricted-spawn mechanism is empty; refusing to run the probe"
        )
    executable = str(parts[0])
    canonical = _canonical_launcher_path(executable)
    if canonical is None:
        raise ActivationProbeError(
            f"restricted-spawn executable '{executable}' is not an absolute path"
        )
    reasons = _trusted_launcher_reasons(
        canonical,
        service_uid=service_uid,
        worker_uid=worker_uid,
        worker_gids=worker_gids,
        stat_fn=stat_fn,
        ancestor_stat=ancestor_stat,
        worker_denied_check=worker_denied_check,
        acl_reader=acl_reader,
    )
    if reasons:
        raise ActivationProbeError(
            "restricted-spawn mechanism is not trusted: " + "; ".join(reasons)
        )
    return [str(canonical), *[str(part) for part in parts[1:]]]


def validate_switch_mechanism(
    parts: Sequence[str],
    *,
    service_uid: int | None = None,
    worker_uid: int | None = None,
    worker_gids: Iterable[int] = (),
    stat_fn: Callable[[Path], os.stat_result] | None = None,
    ancestor_stat: Callable[[Path], os.stat_result] | None = None,
    worker_denied_check: Callable[[os.stat_result], bool] | None = None,
    acl_reader: Callable[[Path], bytes | None] | None = None,
) -> str:
    """Return the mechanism kind, or raise when it is not trusted.

    The probe carries the worker-domain write attempt, so the program that
    performs the identity switch is part of the trusted base. The executable
    must be an absolute path to a root- or service-owned, non-worker-writable
    file in a trusted directory chain. The allowlisted tools
    (``setpriv``/``runuser``) are recognised by basename; any other basename is
    accepted only when it passes the same trusted-ownership checks, which is
    how an explicitly provisioned absolute helper qualifies. A bare name, a
    worker-owned helper, or an untrusted directory is refused.

    This is the compatibility/kind-inspection entry point; execution paths use
    :func:`canonical_switch_mechanism` so they never run the pre-canonical
    spelling.
    """
    canonical_switch_mechanism(
        parts,
        service_uid=service_uid,
        worker_uid=worker_uid,
        worker_gids=worker_gids,
        stat_fn=stat_fn,
        ancestor_stat=ancestor_stat,
        worker_denied_check=worker_denied_check,
        acl_reader=acl_reader,
    )
    base = os.path.basename(str(parts[0]))
    if base in ALLOWED_SWITCH_EXECUTABLES:
        return base
    return "configured"


def _scrubbed_probe_env(source: Mapping[str, str] | None) -> dict[str, str]:
    """Build the minimal, non-inheriting environment for the probe child.

    Only a small allowlist of benign keys is carried over. ``PYTHONPATH``,
    ``PYTHONSTARTUP``, ``PYTHONHOME``, ``LD_PRELOAD``, and every other
    attacker-influenceable variable are dropped, so a worker cannot inject
    startup hooks that forge the probe's execution evidence.
    """
    origin = os.environ if source is None else source
    return {
        key: origin[key]
        for key in _PROBE_PASSTHROUGH_ENV_KEYS
        if key in origin
    }


def _parse_probe_evidence(stdout: str | None) -> dict[str, Any]:
    """Parse the probe child's single JSON evidence line, or raise."""
    text = (stdout or "").strip()
    if not text:
        raise ActivationProbeError(
            "activation probe produced no execution evidence; a bare exit status "
            "does not prove that a worker-domain write attempt ran"
        )
    candidate = text.splitlines()[-1].strip()
    try:
        evidence = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ActivationProbeError(
            f"activation probe evidence was unreadable: {exc}"
        ) from exc
    if not isinstance(evidence, dict):
        raise ActivationProbeError("activation probe evidence was not a JSON object")
    return evidence


def run_activation_probe(
    *,
    store_path: os.PathLike[str] | str | None,
    worker_name: str,
    worker_uid: int | None = None,
    switch: Sequence[str] | None = None,
    runner: Callable[..., Any] | None = None,
    python: str | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    service_uid: int | None = None,
    worker_gids: Iterable[int] | None = None,
    nonce: str | None = None,
    launcher_stat: Callable[[Path], os.stat_result] | None = None,
    launcher_ancestor_stat: Callable[[Path], os.stat_result] | None = None,
    launcher_denied_check: Callable[[os.stat_result], bool] | None = None,
    launcher_acl_reader: Callable[[Path], bytes | None] | None = None,
) -> ProbeResult:
    """Run the mandatory activation probe and require a proven denied write.

    Spawns a real subprocess under the worker principal (via a trusted
    absolute switch mechanism) that first proves its effective uid is the
    worker's and then attempts to open *store_path* for writing. A denial
    passes only when the exit status, the reported uid, the reported
    ``EACCES`` open result, and the per-invocation nonce all agree. The child
    runs in Python isolated mode (``-I -S``) with a scrubbed, non-inherited
    environment, so ``PYTHONPATH``/``sitecustomize`` hooks cannot forge the
    evidence. A successful write, a wrong or unproven identity, an inability
    to spawn the restricted process, or any indeterminate result raises
    :class:`ActivationProbeError`.
    """
    if store_path is None:
        raise ActivationProbeError(
            "activation probe requires the authority-store file path; refusing to run"
        )
    # Canonicalize so the probe targets the real file, not a symlink that could
    # be repointed between detection and the attempt.
    store_path = str(ledger._canonical(store_path))
    if worker_uid is None:
        raise ActivationProbeError(
            "activation probe requires the worker principal uid to verify the "
            "child identity; refusing to run"
        )
    mechanism = list(switch) if switch is not None else discover_switch_mechanism(
        worker_name, env=env, which=which
    )
    if not mechanism:
        raise ActivationProbeError(
            f"no restricted-spawn mechanism is available to run the probe under "
            f"the worker principal '{worker_name}'; refusing to enable supervision"
        )
    # Replace the launcher with its canonical, verified path *before* execution.
    # Validating the pre-canonical spelling and then executing it would let a
    # worker-controlled symlink be repointed after validation; only the
    # canonical path is both checked and run.
    mechanism = canonical_switch_mechanism(
        mechanism,
        service_uid=service_uid,
        worker_uid=worker_uid,
        worker_gids=worker_gids or (),
        stat_fn=launcher_stat,
        ancestor_stat=launcher_ancestor_stat,
        worker_denied_check=launcher_denied_check,
        acl_reader=launcher_acl_reader,
    )
    interpreter = python or sys.executable
    token = nonce or os.urandom(16).hex()
    argv = mechanism + [
        interpreter,
        "-I",
        "-S",
        "-c",
        PROBE_SCRIPT,
        str(store_path),
        str(int(worker_uid)),
        token,
    ]
    execute = runner or subprocess.run
    try:
        completed = execute(
            argv, capture_output=True, text=True, env=_scrubbed_probe_env(env)
        )
    except Exception as exc:  # noqa: BLE001 - every spawn failure is named
        raise ActivationProbeError(
            f"activation probe could not spawn the worker-principal process: {exc}"
        ) from exc

    returncode = getattr(completed, "returncode", None)
    evidence = _parse_probe_evidence(getattr(completed, "stdout", None))

    if evidence.get("nonce") != token:
        raise ActivationProbeError(
            "activation probe evidence nonce does not match this invocation; "
            "refusing to accept replayed or pre-baked evidence"
        )
    reported_uid = evidence.get("uid")
    if reported_uid != int(worker_uid):
        raise ActivationProbeError(
            f"activation probe identity mismatch: child reported uid {reported_uid!r} "
            f"but the worker principal is uid {int(worker_uid)}; refusing to accept "
            "the probe result"
        )
    if (
        returncode != PROBE_EXIT_DENIED
        or evidence.get("outcome") != "denied"
        or evidence.get("errno") != errno.EACCES
    ):
        outcome = evidence.get("outcome", "indeterminate")
        raise ActivationProbeError(
            f"activation probe failed: worker-domain write attempt was "
            f"'{outcome}' (exit {returncode}, errno {evidence.get('errno')!r}); "
            "supervision is not enabled"
        )
    return ProbeResult(
        outcome="denied",
        reason=(
            f"worker principal '{worker_name}' (uid {int(worker_uid)}) was denied "
            f"write access to {store_path}"
        ),
        command=tuple(argv),
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# The single fail-closed gate
# ---------------------------------------------------------------------------


def require_authority_backend(
    *,
    report: CapabilityReport | None = None,
    detect_kwargs: Mapping[str, Any] | None = None,
    switch: Sequence[str] | None = None,
    runner: Callable[..., Any] | None = None,
    probe_kwargs: Mapping[str, Any] | None = None,
    gid_resolver: Callable[[str], Sequence[int]] | None = None,
) -> CapabilityReport:
    """The one fail-closed gate every enablement path passes.

    Composes detection with the mandatory activation probe. An unavailable
    backend raises :class:`UnsupportedHostError`; a failing (or un-runnable)
    probe raises :class:`ActivationProbeError`. There is deliberately no
    no-probe route: every available path runs the real probe, and nothing is
    provisioned and no weaker posture is substituted.

    The worker's primary and supplementary gids are resolved *here*, in the
    gate, and passed to the probe as ``worker_gids`` so the probe's canonical
    launcher and ancestry checks can see a group-class ACL grant to the worker.
    Without them an owning-group or named-group write grant on the launcher
    (or its parent chain) would be invisible. Resolution failing or yielding no
    gids fails closed, because the group half of the boundary could not be
    proven. ``gid_resolver`` is the injection seam (default:
    :func:`_default_gid_resolver`).

    ``switch``/``runner``/``probe_kwargs`` are test seams threaded to
    :func:`run_activation_probe`; production callers omit them and the gate
    discovers and canonicalizes the launcher itself.
    """
    capability = report if report is not None else detect_backend(**(dict(detect_kwargs or {})))
    if capability.status != STATUS_AVAILABLE:
        raise UnsupportedHostError(
            f"no supported authority backend ({capability.status}): "
            + "; ".join(capability.reasons)
        )
    if capability.principals is None or capability.principals.worker.uid is None:
        raise ActivationProbeError(
            "available capability report is missing the worker principal; "
            "refusing to enable supervision"
        )
    if capability.state_path is None:
        raise ActivationProbeError(
            "available capability report is missing the authority-store file path; "
            "refusing to enable supervision"
        )

    worker_name = capability.principals.worker.name
    resolve_gids = gid_resolver or _default_gid_resolver
    try:
        worker_gids = list(resolve_gids(worker_name))
    except Exception as exc:  # noqa: BLE001 - fail closed on any resolution failure
        raise ActivationProbeError(
            f"could not resolve group memberships for the worker principal "
            f"'{worker_name}'; refusing to enable supervision: {exc}"
        ) from exc
    if not worker_gids:
        raise ActivationProbeError(
            f"no group memberships could be resolved for the worker principal "
            f"'{worker_name}'; refusing to enable supervision"
        )

    extra = dict(probe_kwargs or {})
    # The gate's own resolution is authoritative: a caller-supplied worker_gids
    # in probe_kwargs must not override (or weaken) it.
    extra.pop("worker_gids", None)
    run_activation_probe(
        store_path=capability.state_path,
        worker_name=worker_name,
        worker_uid=capability.principals.worker.uid,
        service_uid=capability.principals.service.uid,
        worker_gids=worker_gids,
        switch=switch,
        runner=runner,
        **extra,
    )
    return capability


__all__ = [
    "ActivationProbeError",
    "ALLOWED_SWITCH_EXECUTABLES",
    "AuthorityError",
    "BACKEND_NAME",
    "CapabilityReport",
    "DEFAULT_SERVICE_PRINCIPAL",
    "DEFAULT_WORKER_PRINCIPAL",
    "Principal",
    "PrincipalSet",
    "PROBE_EXIT_DENIED",
    "PROBE_EXIT_WROTE",
    "PROBE_EXIT_WRONG_UID",
    "PROBE_SCRIPT",
    "PROVISIONING_POINTER",
    "ProbeResult",
    "SERVICE_PRINCIPAL_ENV",
    "STATE_PATH_ENV",
    "STATUS_AVAILABLE",
    "STATUS_UNPROVISIONED",
    "STATUS_UNSUPPORTED",
    "STORE_FILE_ENV",
    "SWITCH_CMD_ENV",
    "TRUSTED_PARENT_UIDS",
    "TRUSTED_SWITCH_DIRS",
    "UnsupportedHostError",
    "WORKER_PRINCIPAL_ENV",
    "WriteDecision",
    "acl_worker_write_decision",
    "canonical_switch_mechanism",
    "decide_worker_write",
    "decide_worker_write_for_stat",
    "default_state_path",
    "detect_backend",
    "discover_switch_mechanism",
    "interpret_probe_exit",
    "parse_posix_acl_access",
    "principal_set_from_environment",
    "require_authority_backend",
    "resolve_principal",
    "run_activation_probe",
    "validate_switch_mechanism",
    "worker_write_denied",
]
