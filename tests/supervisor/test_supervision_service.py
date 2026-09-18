"""Hermetic tests for the packaged supervision service.

This suite proves the service-packaging contract end to end without touching
the real host:

* the versioned systemd user unit template and provisioning document are
  deployed by the real shared installer into a temporary ``HOME`` sandbox,
  byte-for-byte, disabled, and with no account or store created;
* adapter and universal installs produce an identical service layout;
* the activation gate is mandatory and fails closed on an unsupported host;
* ``opsx-plan doctor`` reports the service/schema/backend state read-only and
  stays green without supervision enablement;
* the installed commands survive start/kill/restart against a durable ledger,
  using a fake service-manager seam and the loopback fake OpenCode API.

The suite installs its own fail-closed guard (below) that permits only loopback
network, local subprocesses, and the temporary installer sandbox, and refuses
external network, paid-model credentials, and real service-manager provisioning.
It deliberately does not import the fault suite's guard, which forbids
installer commands that this suite must run inside its sandbox.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO = Path(__file__).resolve().parents[2]
_ORCH_INSTALLER = _REPO / "scripts" / "install-orchestrator.sh"
_OPENCODE_INSTALLER = _REPO / "adapters" / "opencode" / "install.sh"
_UNIVERSAL_INSTALLER = _REPO / "install.sh"
_TEMPLATE = _REPO / "systemd" / "opsx-supervise.service.in"
_DOCUMENT = _REPO / "docs" / "opsx-supervision-service.md"

_RUNTIME_REL = Path(".local") / "lib" / "opsx-controller"
_TEMPLATE_REL = Path("systemd") / "opsx-supervise.service.in"
_DOCUMENT_REL = Path("docs") / "opsx-supervision-service.md"
_RENDERED_UNIT_REL = Path(".config") / "systemd" / "user" / "opsx-supervise.service"

# ---------------------------------------------------------------------------
# Fail-closed service guard
# ---------------------------------------------------------------------------

PAID_CREDENTIAL_ENV = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "MISTRAL_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "COHERE_API_KEY",
    "DEEPSEEK_API_KEY",
    "XAI_API_KEY",
    "GROQ_API_KEY",
)

FAKE_MODEL_PREFIXES = ("fake/", "test-provider/", "fake-")

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")

# Commands that would provision a real host or reach the external network.
FORBIDDEN_COMMAND_MARKERS = (
    "systemctl",
    "launchctl",
    "launchd",
    "loginctl",
    "daemon-reload",
    "rc-service",
    "/etc/init.d",
    "schtasks",
    "curl",
    "wget",
    "ssh",
    "scp",
    "rsync",
)


class ServiceGuardError(RuntimeError):
    """Raised when a service-suite check attempts a prohibited resource."""


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


def scrub_paid_credentials(env: Mapping[str, Any] | None = None) -> list[str]:
    target = os.environ if env is None else env
    removed: list[str] = []
    for key in PAID_CREDENTIAL_ENV:
        if _text(target.get(key)).strip():
            removed.append(key)
        target.pop(key, None)  # type: ignore[attr-defined]
    return removed


def assert_no_paid_credentials(env: Mapping[str, Any] | None = None) -> None:
    target = os.environ if env is None else env
    present = sorted(k for k in PAID_CREDENTIAL_ENV if _text(target.get(k)).strip())
    if present:
        raise ServiceGuardError(
            f"paid-model credentials are prohibited: {', '.join(present)}"
        )


def assert_fake_model_identifier(model: Any) -> str:
    text = _text(model).strip()
    if not text:
        raise ServiceGuardError("an empty model identifier is prohibited")
    if any(text.startswith(prefix) for prefix in FAKE_MODEL_PREFIXES):
        return text
    raise ServiceGuardError(
        f"non-fake model identifier {text!r} is prohibited; use a fake/test-provider pin"
    )


def assert_loopback_address(address: Any) -> None:
    if isinstance(address, (str, bytes)) or address is None:
        return
    try:
        host = _text(address[0])
    except (TypeError, IndexError):
        raise ServiceGuardError(
            f"unrecognized connect address {address!r}; refusing non-loopback"
        ) from None
    if host not in LOOPBACK_HOSTS:
        raise ServiceGuardError(
            f"non-loopback connect to {host!r} is prohibited; only AF_UNIX and "
            "loopback are permitted"
        )


def assert_command_allowed(argv: Any) -> None:
    if isinstance(argv, (str, bytes)):
        tokens: Sequence[Any] = [argv]
    else:
        try:
            tokens = list(argv or ())
        except TypeError:
            tokens = [argv]
    for token in tokens:
        text = _text(token)
        for marker in FORBIDDEN_COMMAND_MARKERS:
            if marker in text:
                raise ServiceGuardError(
                    f"real service-manager / external-network command is "
                    f"prohibited (matched {marker!r} in {text!r})"
                )


def _guarded_connect(original: Any) -> Any:
    def connect(self: Any, address: Any) -> Any:
        if getattr(self, "family", None) == getattr(socket, "AF_UNIX", None):
            return original(self, address)
        if isinstance(address, (str, bytes)) or address is None:
            return original(self, address)
        assert_loopback_address(address)
        return original(self, address)

    return connect


def _guarded_connect_ex(original: Any) -> Any:
    def connect_ex(self: Any, address: Any) -> Any:
        if getattr(self, "family", None) == getattr(socket, "AF_UNIX", None):
            return original(self, address)
        if isinstance(address, (str, bytes)) or address is None:
            return original(self, address)
        assert_loopback_address(address)
        return original(self, address)

    return connect_ex


def _guarded_popen_init(original: Any) -> Any:
    def init(self: Any, args: Any, *rest: Any, **kwargs: Any) -> Any:
        assert_command_allowed(args)
        return original(self, args, *rest, **kwargs)

    return init


class service_guard(contextlib.AbstractContextManager):
    """Fail-closed guard for the service-packaging suite.

    Entering the context scrubs paid credentials, patches the socket connect
    paths to permit only loopback / AF_UNIX, and patches ``subprocess.Popen`` to
    refuse real service-manager and external-network commands while still
    permitting the local installer sandbox.
    """

    def __init__(self, *, environ: Mapping[str, Any] | None = None) -> None:
        self._env = os.environ if environ is None else environ
        self._saved_connect: Any = None
        self._saved_connect_ex: Any = None
        self._saved_popen_init: Any = None

    def __enter__(self) -> "service_guard":
        scrub_paid_credentials(self._env)
        self._saved_connect = socket.socket.connect
        self._saved_connect_ex = socket.socket.connect_ex
        self._saved_popen_init = subprocess.Popen.__init__
        socket.socket.connect = _guarded_connect(self._saved_connect)
        socket.socket.connect_ex = _guarded_connect_ex(self._saved_connect_ex)
        subprocess.Popen.__init__ = _guarded_popen_init(self._saved_popen_init)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._saved_connect is not None:
            socket.socket.connect = self._saved_connect
        if self._saved_connect_ex is not None:
            socket.socket.connect_ex = self._saved_connect_ex
        if self._saved_popen_init is not None:
            subprocess.Popen.__init__ = self._saved_popen_init
        return None


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _model_env() -> dict[str, str]:
    return {
        "OPSX_CONTROLLER_MODEL": "test-provider/test-controller",
        "OPSX_IMPLEMENTER_MODEL": "test-provider/test-implementer",
        "OPSX_REVIEWER_MODEL": "test-provider/test-reviewer",
        "OPSX_ARCHIVER_MODEL": "test-provider/test-archiver",
    }


def _run_installer(
    installer: Path, home: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(installer), *args],
        cwd=_REPO,
        env={**os.environ, "HOME": str(home), **env},
        capture_output=True,
        text=True,
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class FakeServiceManager:
    """A test seam that models process lifetime and records lifecycle calls.

    It never touches a real service manager: it only records enable/start/stop/
    kill/restart requests and runs the supplied local commands as subprocesses.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._live: dict[str, subprocess.Popen] = {}

    def enable(self, unit: str) -> None:
        self.calls.append(("enable", unit))

    def disable(self, unit: str) -> None:
        self.calls.append(("disable", unit))

    def start(
        self, unit: str, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path
    ) -> subprocess.Popen:
        self.calls.append(("start", unit))
        proc = subprocess.Popen(
            [str(token) for token in argv],
            env=dict(env),
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._live[unit] = proc
        return proc

    def kill(self, unit: str) -> None:
        self.calls.append(("kill", unit))
        proc = self._live.pop(unit)
        proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 - best-effort reap in a test seam
            pass
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    def restart(
        self, unit: str, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path
    ) -> subprocess.Popen:
        self.calls.append(("restart", unit))
        return self.start(unit, argv, env=env, cwd=cwd)

    def wait(self, unit: str, timeout: float = 60.0) -> tuple[str, str]:
        proc = self._live.pop(unit)
        out, err = proc.communicate(timeout=timeout)
        return out or "", err or ""


def _policy() -> dict[str, Any]:
    from lib.supervisor import budgets as budget_mod
    from lib.supervisor import model_policy

    return {
        "authority_config": {"mode": "policy-bound"},
        "model_selection": {
            "version": model_policy.MODEL_POLICY_VERSION,
            "roles": {role: "fake/x" for role in model_policy.POLICY_ROLES},
            "stages": dict(model_policy.STANDARD_STAGE_MAPPING),
        },
        "inexpensive_allowlist": {
            "version": model_policy.MODEL_POLICY_VERSION,
            "models": ["fake/x"],
            "source": "test fixture",
        },
        "manifest_snapshot_hash": "deadbeef",
        "budgets": {
            "version": budget_mod.BUDGET_SCHEMA_VERSION,
            "total_cost_usd": 100.0,
            "per_action_cost_usd": None,
            "total_elapsed_minutes": None,
            "per_action_elapsed_minutes": None,
            "max_incident_attempts": None,
        },
        "deadlines": {
            "version": budget_mod.BUDGET_SCHEMA_VERSION,
            "execution_deadline_minutes": None,
        },
    }


class ServiceSuiteCase(unittest.TestCase):
    """Shared sandbox: a guard, a temporary HOME, a repo, and durable storage."""

    def setUp(self) -> None:
        guard = service_guard()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.storage = self.root / "service-storage"
        self.storage.mkdir()
        self.db = self.storage / "supervisor.sqlite3"
        self.env = {**_model_env(), "HOME": str(self.home)}

    # -- helpers ---------------------------------------------------------

    def _install(
        self, *, home: Path | None = None, installer: Path | None = None
    ) -> None:
        target = home if home is not None else self.home
        if installer is None or installer == _ORCH_INSTALLER:
            argv = [
                "bash",
                str(_ORCH_INSTALLER),
                str(_REPO),
                "--global",
            ]
        else:
            argv = ["bash", str(installer), "--global"]
        proc = subprocess.run(
            argv,
            cwd=_REPO,
            env={**os.environ, "HOME": str(target), **_model_env()},
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def _runtime(self, home: Path | None = None) -> Path:
        return (home if home is not None else self.home) / _RUNTIME_REL

    def _installed_binary(self, home: Path | None = None) -> Path:
        return (home if home is not None else self.home) / ".local" / "bin" / "opsx-plan"

    def _register_job(self) -> int:
        from lib.supervisor import ledger as ledger_mod

        handle = ledger_mod.open_ledger(self.db, repository_root=self.repo)
        try:
            return handle.register_job(
                run_id="run-service",
                worktree=self.repo,
                owner="service",
                policy=_policy(),
                operator="operator",
                manifest_content="# supervised plan\ntitle = 'x'\n",
            )
        finally:
            handle.close()

    def _subprocess_env(self) -> dict[str, str]:
        env = {
            **os.environ,
            "HOME": str(self.home),
            "OPSX_SUPERVISOR_STATE_FILE": str(self.db),
        }
        env.pop("PYTHONPATH", None)
        return env


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


class ServiceGuardSelfTests(unittest.TestCase):
    def test_guard_refuses_service_manager_and_external_commands(self) -> None:
        for forbidden in (
            ["systemctl", "--user", "enable", "opsx-supervise.service"],
            ["launchctl", "load", "x.plist"],
            ["wget", "https://example.com"],
            ["rsync", "host:/x", "/tmp/x"],
        ):
            with self.assertRaises(ServiceGuardError):
                assert_command_allowed(forbidden)

    def test_guard_permits_the_local_installer_sandbox(self) -> None:
        assert_command_allowed(
            ["bash", str(_ORCH_INSTALLER), str(_REPO), "--global"]
        )
        assert_command_allowed(["git", "init"])
        assert_command_allowed([sys.executable, "-c", "print(1)"])

    def test_guard_refuses_non_loopback_and_paid_credentials(self) -> None:
        with self.assertRaises(ServiceGuardError):
            assert_loopback_address(("8.8.8.8", 53))
        assert_loopback_address(("127.0.0.1", 1234))
        with self.assertRaises(ServiceGuardError):
            assert_fake_model_identifier("openai/gpt-5")
        self.assertEqual(assert_fake_model_identifier("fake/x"), "fake/x")

    def test_guard_scrubs_paid_credentials(self) -> None:
        env = {"OPENAI_API_KEY": "secret", "KEEP": "1"}
        removed = scrub_paid_credentials(env)
        self.assertEqual(removed, ["OPENAI_API_KEY"])
        self.assertNotIn("OPENAI_API_KEY", env)
        assert_no_paid_credentials(env)


# ---------------------------------------------------------------------------
# 1.x / 4.2: versioned data and sandboxed install
# ---------------------------------------------------------------------------


class ServicePackagingInstallTests(ServiceSuiteCase):
    def test_installer_deploys_template_and_document_byte_for_byte(self) -> None:
        self._install()
        runtime = self._runtime()
        for rel, source in ((_TEMPLATE_REL, _TEMPLATE), (_DOCUMENT_REL, _DOCUMENT)):
            installed = runtime / rel
            self.assertTrue(installed.is_file(), f"missing installed artifact: {rel}")
            self.assertEqual(
                _sha(source), _sha(installed), f"installed artifact differs: {rel}"
            )
        self.assertFalse(
            (self.home / _RENDERED_UNIT_REL).exists(),
            "installer must not write a rendered unit into the service-manager dir",
        )

    def test_repeated_install_refreshes_packaged_artifacts(self) -> None:
        self._install()
        installed = self._runtime() / _TEMPLATE_REL
        installed.write_text("stale\n", encoding="utf-8")
        self._install()
        self.assertEqual(_sha(_TEMPLATE), _sha(installed))

    def test_install_creates_no_account_and_no_store(self) -> None:
        self._install()
        share = self.home / ".local" / "share" / "opsx-controller" / "supervisor"
        self.assertFalse(share.exists(), "installer must not create the authority store")
        self.assertFalse(
            (self.home / ".config" / "systemd" / "user").exists(),
            "installer must not touch the service-manager unit directory",
        )
        proc = subprocess.run(
            ["bash", str(_ORCH_INSTALLER), str(_REPO), "--global"],
            cwd=_REPO,
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("systemctl", proc.stdout + proc.stderr)
        self.assertNotIn("useradd", proc.stdout + proc.stderr)

    def test_template_and_document_are_inert_linux_only_data(self) -> None:
        template = _TEMPLATE.read_text(encoding="utf-8")
        document = _DOCUMENT.read_text(encoding="utf-8")
        # The template is Linux/systemd-user only and names no other manager.
        for forbidden in ("launchctl", "launchd", "schtasks", "sc.exe"):
            self.assertNotIn(forbidden, template)
        # Data only: no shell executed at install/render time beyond the
        # operator's documented envsubst.
        self.assertIn("[Install]", template)
        self.assertIn("[Service]", template)
        self.assertIn("supervise serve", template)
        # The provisioning document declares non-Linux managers unsupported
        # (fail-closed) and names the mandatory probe and manual enable step.
        self.assertIn("launchd", document)
        self.assertIn("unsupported", document)
        self.assertIn("opsx-plan supervise probe", document)
        self.assertIn("systemctl --user enable", document)


# ---------------------------------------------------------------------------
# 2.2 / 2.4: one shared path, no adapter-specific service logic
# ---------------------------------------------------------------------------


class ServiceInstallationPathTests(unittest.TestCase):
    def test_adapter_installers_have_no_service_logic(self) -> None:
        for installer in sorted((_REPO / "adapters").glob("*/install.sh")):
            text = installer.read_text(encoding="utf-8")
            for forbidden in ("systemctl", "useradd", "launchctl", "supervise.service.in"):
                self.assertNotIn(
                    forbidden,
                    text,
                    f"{installer} must inherit service packaging by delegation",
                )
            self.assertIn("install-orchestrator.sh", text)

    def test_universal_installer_delegates_to_adapters(self) -> None:
        text = _UNIVERSAL_INSTALLER.read_text(encoding="utf-8")
        self.assertIn("install.sh", text)
        self.assertNotIn("systemctl", text)
        self.assertNotIn("useradd", text)


# ---------------------------------------------------------------------------
# 4.4: read-only service-host capability check
# ---------------------------------------------------------------------------


def _supported_host_kwargs() -> dict[str, Any]:
    """Injectable, hermetic inputs for a fully supported service host."""
    return {
        "env": {"XDG_RUNTIME_DIR": "/run/user/1000"},
        "which": lambda name: f"/usr/bin/{name}",
        "path_exists": lambda _path: True,
    }


def _host_without_systemd() -> "Any":
    from lib.orchestrator import supervision_service

    return supervision_service.ServiceHostReport(
        status=supervision_service.HOST_UNSUPPORTED,
        systemd_user_manager=False,
        opencode_bridge=True,
        reasons=("no supported systemd user manager is available",),
        runtime_dir="/run/user/1000",
    )


def _host_without_bridge() -> "Any":
    from lib.orchestrator import supervision_service

    return supervision_service.ServiceHostReport(
        status=supervision_service.HOST_UNSUPPORTED,
        systemd_user_manager=True,
        opencode_bridge=False,
        reasons=("no supported OpenCode session bridge is available",),
        runtime_dir="/run/user/1000",
    )


def _host_available() -> "Any":
    from lib.orchestrator import supervision_service

    return supervision_service.ServiceHostReport(
        status=supervision_service.HOST_AVAILABLE,
        systemd_user_manager=True,
        opencode_bridge=True,
        reasons=(),
        runtime_dir="/run/user/1000",
    )


class ServiceHostCapabilityTests(unittest.TestCase):
    def test_available_when_both_prerequisites_are_present(self) -> None:
        from lib.orchestrator import supervision_service as svc

        report = svc.service_host_capability(**_supported_host_kwargs())
        self.assertTrue(report.available)
        self.assertEqual(report.status, svc.HOST_AVAILABLE)
        self.assertTrue(report.systemd_user_manager)
        self.assertTrue(report.opencode_bridge)
        self.assertEqual(report.reasons, ())

    def test_refuses_an_absent_systemd_user_manager(self) -> None:
        from lib.orchestrator import supervision_service as svc

        report = svc.service_host_capability(
            env={"OPSX_SESSION_SERVER_COMMAND": "opencode serve"},
            which=lambda _name: None,
            path_exists=lambda _path: False,
        )
        self.assertFalse(report.available)
        self.assertFalse(report.systemd_user_manager)
        self.assertTrue(report.opencode_bridge)
        self.assertTrue(any("systemd" in reason for reason in report.reasons))

    def test_refuses_an_absent_opencode_bridge(self) -> None:
        from lib.orchestrator import supervision_service as svc

        report = svc.service_host_capability(
            env={"XDG_RUNTIME_DIR": "/run/user/1000"},
            which=lambda name: "/usr/bin/systemctl" if name == "systemctl" else None,
            path_exists=lambda _path: True,
        )
        self.assertFalse(report.available)
        self.assertTrue(report.systemd_user_manager)
        self.assertFalse(report.opencode_bridge)
        self.assertTrue(any("OpenCode" in reason for reason in report.reasons))

    def test_refuses_a_non_opencode_configured_command(self) -> None:
        from lib.orchestrator import supervision_service as svc

        # Even with a resolvable trusted 'opencode', an explicitly pinned
        # non-OpenCode bridge is refused rather than silently rescued.
        report = svc.service_host_capability(
            env={
                "XDG_RUNTIME_DIR": "/run/user/1000",
                "OPSX_SESSION_SERVER_COMMAND": "claude serve --port 41000",
            },
            which=lambda name: f"/usr/bin/{name}",
            path_exists=lambda _path: True,
        )
        self.assertFalse(report.available)
        self.assertTrue(report.systemd_user_manager)
        self.assertFalse(report.opencode_bridge)
        self.assertTrue(
            any("non-OpenCode" in reason for reason in report.reasons),
            report.reasons,
        )

    def test_refuses_an_unparseable_configured_command(self) -> None:
        from lib.orchestrator import supervision_service as svc

        report = svc.service_host_capability(
            env={
                "XDG_RUNTIME_DIR": "/run/user/1000",
                "OPSX_SESSION_SERVER_COMMAND": "opencode serve 'unterminated",
            },
            which=lambda name: f"/usr/bin/{name}",
            path_exists=lambda _path: True,
        )
        self.assertFalse(report.available)
        self.assertFalse(report.opencode_bridge)

    def test_accepts_an_approved_opencode_configured_command(self) -> None:
        from lib.orchestrator import supervision_service as svc

        # The pinned OpenCode invocation alone suffices: no trusted 'opencode'
        # resolution is required when the override is an allowed argv.
        report = svc.service_host_capability(
            env={
                "XDG_RUNTIME_DIR": "/run/user/1000",
                "OPSX_SESSION_SERVER_COMMAND": (
                    "/usr/local/bin/opencode serve --hostname 127.0.0.1 --port 41000"
                ),
            },
            which=lambda name: "/usr/bin/systemctl" if name == "systemctl" else None,
            path_exists=lambda _path: True,
        )
        self.assertTrue(report.available)
        self.assertTrue(report.systemd_user_manager)
        self.assertTrue(report.opencode_bridge)
        self.assertEqual(report.reasons, ())

    def test_refuses_an_opencode_command_without_the_serve_subcommand(self) -> None:
        from lib.orchestrator import supervision_service as svc

        report = svc.service_host_capability(
            env={
                "XDG_RUNTIME_DIR": "/run/user/1000",
                "OPSX_SESSION_SERVER_COMMAND": "opencode --version",
            },
            which=lambda name: f"/usr/bin/{name}",
            path_exists=lambda _path: True,
        )
        self.assertFalse(report.available)
        self.assertFalse(report.opencode_bridge)

    def test_refuses_an_opencode_command_with_a_later_serve_argument(self) -> None:
        from lib.orchestrator import supervision_service as svc

        # A later argv token merely named 'serve' is not the subcommand:
        # 'opencode run serve' never invokes the OpenCode server, so the
        # bridge check must fail closed on it.
        for malformed in (
            "opencode run serve",
            "opencode --verbose run serve --port 41000",
            "opencode exec 'serve'",
        ):
            report = svc.service_host_capability(
                env={
                    "XDG_RUNTIME_DIR": "/run/user/1000",
                    "OPSX_SESSION_SERVER_COMMAND": malformed,
                },
                which=lambda name: f"/usr/bin/{name}",
                path_exists=lambda _path: True,
            )
            self.assertFalse(report.available, malformed)
            self.assertFalse(report.opencode_bridge, malformed)
            self.assertTrue(
                any("'opencode serve'" in reason for reason in report.reasons),
                (malformed, report.reasons),
            )

    def test_is_read_only_and_never_enables_anything(self) -> None:
        from lib.orchestrator import supervision_service as svc

        # No subprocess, socket, or write is attempted: the check is pure and
        # only consults the injected read-only seams.
        calls: list[str] = []
        report = svc.service_host_capability(
            env={},
            which=lambda name: calls.append(name) or None,
            path_exists=lambda _path: True,
        )
        self.assertFalse(report.available)
        self.assertIn("systemctl", calls)
        self.assertIn("opencode", calls)


# ---------------------------------------------------------------------------
# 4.4: activation gate is mandatory and fails closed on either host check
# ---------------------------------------------------------------------------


class ServiceActivationGateTests(unittest.TestCase):
    def test_unsupported_authority_host_raises_named_error(self) -> None:
        from lib.orchestrator import supervision_service
        from lib.supervisor import authority

        report = authority.detect_backend(
            system="darwin", peer_credential_supported=False
        )
        self.assertEqual(report.status, authority.STATUS_UNSUPPORTED)
        with self.assertRaises(authority.UnsupportedHostError):
            supervision_service.activation_gate(
                host_report=_host_available(), report=report
            )

    def test_unprovisioned_authority_host_fails_closed_without_probe(self) -> None:
        from lib.orchestrator import supervision_service
        from lib.supervisor import authority

        report = authority.detect_backend(
            principals=authority.principal_set_from_environment(
                env={}, resolver=lambda _name: None
            )
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        with self.assertRaises(authority.UnsupportedHostError):
            supervision_service.activation_gate(
                host_report=_host_available(), report=report
            )

    def test_missing_systemd_user_manager_raises_named_error(self) -> None:
        from lib.orchestrator import supervision_service
        from lib.supervisor import authority

        with self.assertRaises(authority.UnsupportedHostError) as ctx:
            supervision_service.activation_gate(
                host_report=_host_without_systemd(), report="unused"
            )
        self.assertIn("systemd", str(ctx.exception))

    def test_missing_opencode_bridge_raises_named_error(self) -> None:
        from lib.orchestrator import supervision_service
        from lib.supervisor import authority

        with self.assertRaises(authority.UnsupportedHostError) as ctx:
            supervision_service.activation_gate(
                host_report=_host_without_bridge(), report="unused"
            )
        self.assertIn("OpenCode", str(ctx.exception))

    def test_host_kwargs_injection_refuses_an_unprovisioned_host_hermetically(self) -> None:
        from lib.orchestrator import supervision_service
        from lib.supervisor import authority

        with self.assertRaises(authority.UnsupportedHostError):
            supervision_service.activation_gate(
                host_kwargs={
                    "env": {},
                    "which": lambda _name: None,
                    "path_exists": lambda _path: False,
                },
                report="unused",
            )

    def test_non_opencode_configured_bridge_raises_named_error(self) -> None:
        from lib.orchestrator import supervision_service
        from lib.supervisor import authority

        with self.assertRaises(authority.UnsupportedHostError) as ctx:
            supervision_service.activation_gate(
                host_kwargs={
                    "env": {
                        "XDG_RUNTIME_DIR": "/run/user/1000",
                        "OPSX_SESSION_SERVER_COMMAND": "claude serve",
                    },
                    "which": lambda name: f"/usr/bin/{name}",
                    "path_exists": lambda _path: True,
                },
                report="unused",
            )
        self.assertIn("non-OpenCode", str(ctx.exception))

    def test_opencode_command_with_a_later_serve_argument_raises_named_error(self) -> None:
        from lib.orchestrator import supervision_service
        from lib.supervisor import authority

        # The activation gate must fail closed on a malformed OpenCode argv
        # whose only 'serve' token is an argument, not the subcommand.
        with self.assertRaises(authority.UnsupportedHostError) as ctx:
            supervision_service.activation_gate(
                host_kwargs={
                    "env": {
                        "XDG_RUNTIME_DIR": "/run/user/1000",
                        "OPSX_SESSION_SERVER_COMMAND": "opencode run serve",
                    },
                    "which": lambda name: f"/usr/bin/{name}",
                    "path_exists": lambda _path: True,
                },
                report="unused",
            )
        self.assertIn("'opencode serve'", str(ctx.exception))

    def test_gate_composes_the_host_check_with_the_authority_gate(self) -> None:
        from unittest import mock

        from lib.orchestrator import supervision_service
        from lib.supervisor import authority

        sentinel = object()
        with mock.patch.object(
            authority,
            "require_authority_backend",
            return_value=sentinel,
        ) as gate:
            self.assertIs(
                supervision_service.activation_gate(
                    host_report=_host_available(), report="r"
                ),
                sentinel,
            )
        gate.assert_called_once_with(report="r")
        # The gate resolves the live owning module at call time, so a patch on
        # lib.supervisor.authority is observed.
        with mock.patch.object(
            authority,
            "require_authority_backend",
            side_effect=authority.UnsupportedHostError("x"),
        ):
            with self.assertRaises(authority.UnsupportedHostError):
                supervision_service.activation_gate(
                    host_report=_host_available(), report="r"
                )

    def test_probe_host_check_runs_before_the_authority_gate(self) -> None:
        import io
        from unittest import mock

        from lib.orchestrator import cmd_supervise, supervision_service
        from lib.supervisor import authority

        # A missing host prerequisite must refuse even when the authority gate
        # would otherwise be available: no fallback to the weaker path.
        with mock.patch.object(
            authority, "require_authority_backend"
        ) as authority_gate:
            with mock.patch.object(
                supervision_service,
                "service_host_capability",
                return_value=_host_without_bridge(),
            ):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(
                        cmd_supervise.cmd_supervise_probe(mock.Mock()), 1
                    )
            authority_gate.assert_not_called()


# ---------------------------------------------------------------------------
# 4.4: the rendered unit runs under the configured service identity
# ---------------------------------------------------------------------------


class ServiceUnitIdentityTests(unittest.TestCase):
    def _render(self, **overrides: str) -> str:
        rendered = _TEMPLATE.read_text(encoding="utf-8")
        values = {
            "OPSX_SUPERVISE_EXECUTABLE": "/home/opsx-supervisor/.local/bin/opsx-plan",
            "OPSX_SUPERVISE_REPO": "/srv/supervised",
            "OPSX_SUPERVISE_SERVICE_PRINCIPAL": "opsx-supervisor",
            "OPSX_SUPERVISE_WORKER_PRINCIPAL": "opsx-worker",
            "OPSX_SUPERVISE_STATE_FILE": (
                "/home/opsx-supervisor/.local/share/opsx-controller/supervisor/"
                "supervisor.sqlite3"
            ),
        }
        values.update(overrides)
        for key, value in values.items():
            rendered = rendered.replace("${" + key + "}", value)
        return rendered

    def test_rendered_unit_asserts_the_configured_service_identity(self) -> None:
        rendered = self._render(OPSX_SUPERVISE_SERVICE_PRINCIPAL="svc-account")
        self.assertIn("AssertUser=svc-account", rendered)
        self.assertIn(
            "Environment=OPSX_SUPERVISOR_SERVICE_PRINCIPAL=svc-account", rendered
        )
        self.assertNotIn("${OPSX_SUPERVISE_SERVICE_PRINCIPAL}", rendered)

    def test_non_service_user_manager_is_refused_by_the_unit(self) -> None:
        rendered = self._render(OPSX_SUPERVISE_SERVICE_PRINCIPAL="opsx-supervisor")
        # The assertion user is the configured service principal, never `%u`
        # (the loading manager's user) and never an environment-only identity.
        self.assertIn("AssertUser=opsx-supervisor", rendered)
        self.assertNotIn("AssertUser=%u", rendered)

    def test_provisioning_document_requires_the_service_principal_manager(self) -> None:
        document = _DOCUMENT.read_text(encoding="utf-8")
        self.assertIn("loginctl enable-linger", document)
        self.assertIn("AssertUser=", document)
        self.assertIn("service principal's own user manager", document)
        self.assertIn("XDG_RUNTIME_DIR", document)
        self.assertIn("systemctl --user enable", document)


# ---------------------------------------------------------------------------
# 4.4: identical layout across installers
# ---------------------------------------------------------------------------


class ServiceLayoutParityTests(ServiceSuiteCase):
    def test_adapter_and_universal_installs_share_the_service_layout(self) -> None:
        adapter_home = self.root / "adapter-home"
        adapter_home.mkdir()
        universal_home = self.root / "universal-home"
        universal_home.mkdir()

        self._install(home=adapter_home, installer=_OPENCODE_INSTALLER)
        proc = _run_installer(_UNIVERSAL_INSTALLER, universal_home, _model_env(), "--global")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        for rel in (_TEMPLATE_REL, _DOCUMENT_REL):
            adapter_file = adapter_home / _RUNTIME_REL / rel
            universal_file = universal_home / _RUNTIME_REL / rel
            self.assertTrue(adapter_file.is_file(), f"adapter missing {rel}")
            self.assertTrue(universal_file.is_file(), f"universal missing {rel}")
            self.assertEqual(_tree(self._runtime(adapter_home)), _tree(self._runtime(universal_home)))
            self.assertEqual(_sha(adapter_file), _sha(universal_file))

        self.assertFalse((adapter_home / _RENDERED_UNIT_REL).exists())
        self.assertFalse((universal_home / _RENDERED_UNIT_REL).exists())


# ---------------------------------------------------------------------------
# 4.4: installer --verify checks the packaging
# ---------------------------------------------------------------------------


class ServiceVerifyTests(ServiceSuiteCase):
    def test_verify_helper_reports_missing_packaging(self) -> None:
        self._install(installer=_OPENCODE_INSTALLER)
        installed = self._runtime() / _TEMPLATE_REL
        installed.unlink()
        script = (
            f'source "{_REPO}/lib/install-common.sh"; '
            f'verify_supervision_service_packaging "{self._runtime()}" "{_REPO}"'
        )
        proc = subprocess.run(
            ["bash", "-c", script], cwd=_REPO, capture_output=True, text=True
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("MISSING", proc.stdout + proc.stderr)

    def test_verify_helper_accepts_a_matching_deployment(self) -> None:
        self._install(installer=_OPENCODE_INSTALLER)
        script = (
            f'source "{_REPO}/lib/install-common.sh"; '
            f'verify_supervision_service_packaging "{self._runtime()}" "{_REPO}"'
        )
        proc = subprocess.run(
            ["bash", "-c", script], cwd=_REPO, capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("supervision service artifact", proc.stdout + proc.stderr)


# ---------------------------------------------------------------------------
# 4.4: doctor reports read-only and stays green
# ---------------------------------------------------------------------------


class ServiceDoctorReportTests(ServiceSuiteCase):
    def test_doctor_reports_service_schema_and_backend_and_stays_green(self) -> None:
        from lib.supervisor import ledger as ledger_mod

        self._install()
        handle = ledger_mod.open_ledger(self.db, repository_root=self.repo)
        handle.close()

        proc = subprocess.run(
            [
                str(self._installed_binary()),
                "--repo",
                str(self.repo),
                "doctor",
            ],
            cwd=str(self.repo),
            env=self._subprocess_env(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = proc.stdout + proc.stderr
        self.assertIn("\u2713 Supervision service packaging is reported", output)
        self.assertNotIn("\u2717 Supervision service packaging is reported", output)
        self.assertIn("rendered service unit: not present", output)
        self.assertIn("supervisor ledger schema: version", output)
        self.assertIn("isolation backend:", output)
        # No service unit was enabled or created.
        self.assertFalse((self.home / _RENDERED_UNIT_REL).exists())


# ---------------------------------------------------------------------------
# 4.3: fake service manager + loopback fake API + durable restart
# ---------------------------------------------------------------------------


class ServiceRestartContinuationTests(ServiceSuiteCase):
    def test_start_kill_restart_continues_from_durable_state(self) -> None:
        self._install()
        job_id = self._register_job()
        unit = "opsx-supervise.service"
        binary = self._installed_binary()
        once_argv = [
            str(binary),
            "--repo",
            str(self.repo),
            "supervise",
            "watchdog",
            "--once",
            "--job-id",
            str(job_id),
            "--json",
        ]
        loop_argv = [
            str(binary),
            "--repo",
            str(self.repo),
            "supervise",
            "watchdog",
            "--interval",
            "0.2",
            "--job-id",
            str(job_id),
        ]
        manager = FakeServiceManager()

        first = manager.start(unit, once_argv, env=self._subprocess_env(), cwd=self.repo)
        out, err = manager.wait(unit)
        self.assertEqual(first.returncode, 0, err)
        first_report = json.loads(out)
        self.assertEqual(first_report["job_id"], job_id)

        # Start the long-lived installed command, confirm it is live, then kill
        # it as a crash and restart it. No daemon is provisioned: the fake
        # manager only runs local subprocesses.
        loop = manager.start(unit, loop_argv, env=self._subprocess_env(), cwd=self.repo)
        time.sleep(1.0)
        self.assertIsNone(loop.poll(), "installed watchdog did not stay live")
        manager.kill(unit)

        restarted = manager.restart(
            unit, once_argv, env=self._subprocess_env(), cwd=self.repo
        )
        out2, err2 = manager.wait(unit)
        self.assertEqual(restarted.returncode, 0, err2)
        second_report = json.loads(out2)

        # Real continuation: the restarted command reopens the same durable job
        # and classifies it identically, and the durable ledger still holds it.
        self.assertEqual(second_report["job_id"], job_id)
        self.assertEqual(second_report["classification"], first_report["classification"])
        self.assertTrue(self.db.is_file())
        handle_events = second_report.get("recent_events") or []
        self.assertTrue(handle_events, "durable classification event missing after restart")
        self.assertEqual(
            [call[:2] for call in manager.calls[:4]],
            [("start", unit), ("start", unit), ("kill", unit), ("restart", unit)][:4],
        )
        self.assertNotIn(("enable", unit), manager.calls)

    def test_loopback_fake_opencode_api_is_permitted(self) -> None:
        from lib.supervisor import session_bridge as bridge_mod
        from tests.supervisor.test_session_bridge import FakeOpencodeServer

        with FakeOpencodeServer() as server:
            transport = bridge_mod.LoopbackTransport.from_address(server.address)
            self.addCleanup(transport.close)
            bridge = bridge_mod.SessionBridge(transport)
            bridge.check_capability()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
