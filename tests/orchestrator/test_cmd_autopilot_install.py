"""Tests for ``lib.orchestrator.cmd_autopilot_install``.

Hermetic: systemctl is faked through the injectable *runner*, template lookup
is pinned to the repo ``systemd/`` templates through *template_root*, the
systemd user directory is a throwaway temp dir, and the toolchain lookup is a
fake ``which`` returning real temp directories.  No test touches a user
manager.
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lib.models import resolver
from lib.orchestrator import cmd_autopilot_install

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT_TEMPLATE = REPO_ROOT / "systemd" / "opsx-autopilot.service.in"
DEFAULT_UNIT = "opsx-autopilot"

_MODEL_HOME: tempfile.TemporaryDirectory | None = None
_MODEL_PATCH = None
_ENV_PATCH = None


def setUpModule() -> None:
    """Pin model resolution so plan loading never reads ambient config."""
    global _MODEL_HOME, _MODEL_PATCH, _ENV_PATCH
    _MODEL_HOME = tempfile.TemporaryDirectory()
    _MODEL_PATCH = mock.patch.object(
        resolver, "USER_CONFIG_PATH", Path(_MODEL_HOME.name) / "models.toml"
    )
    _MODEL_PATCH.start()
    _ENV_PATCH = mock.patch.dict(
        os.environ,
        {
            "OPSX_CONTROLLER_MODEL": "test-provider/test-controller",
            "OPSX_IMPLEMENTER_MODEL": "test-provider/test-implementer",
            "OPSX_REVIEWER_MODEL": "test-provider/test-reviewer",
            "OPSX_ARCHIVER_MODEL": "test-provider/test-archiver",
        },
    )
    _ENV_PATCH.start()


def tearDownModule() -> None:
    assert _ENV_PATCH is not None and _MODEL_PATCH is not None and _MODEL_HOME is not None
    _ENV_PATCH.stop()
    _MODEL_PATCH.stop()
    _MODEL_HOME.cleanup()


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _unquote_systemd(raw: str) -> str:
    """Test-local inverse of the module's double-quoted systemd encoding.

    Deliberately independent of the implementation: it strips the quotes and
    then reverses backslash escapes, ``$$``, and ``%%`` the way systemd does,
    so a corrupted value fails the round-trip.
    """
    if not (raw.startswith('"') and raw.endswith('"') and len(raw) >= 2):
        # Bare path directive: systemd still applies specifier expansion, so
        # ``%%`` is the only escape to reverse.
        return raw.replace("%%", "%")
    inner = raw[1:-1]
    out: list[str] = []
    i = 0
    while i < len(inner):
        ch = inner[i]
        nxt = inner[i + 1] if i + 1 < len(inner) else ""
        if ch == "\\" and nxt:
            out.append(nxt)
            i += 2
        elif (ch == "$" and nxt == "$") or (ch == "%" and nxt == "%"):
            out.append(ch)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _dropin_value(dropin: str, key: str) -> str:
    """The decoded value of the first ``key=`` line in *dropin*."""
    prefix = f"{key}="
    line = next(line for line in dropin.splitlines() if line.startswith(prefix))
    return _unquote_systemd(line[len(prefix):])


class FakeRunner:
    """Records systemctl argv; optionally fails matching invocations."""

    def __init__(self, fail_on: str | None = None, stderr: str = "boom") -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on
        self.stderr = stderr

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.fail_on is not None and any(self.fail_on in arg for arg in argv):
            return SimpleNamespace(returncode=1, stdout="", stderr=self.stderr)
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class InstallHarness(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
        self.plan = self.repo / "plan.toml"
        self.plan.write_text(
            '[plan]\nname = "install-test"\nadapter = "opencode"\n\n'
            '[[changes]]\nid = "c1"\n',
            encoding="utf-8",
        )

        self.unit_dir = self.root / "systemd-user"

        # Real temp directories for the fake toolchain so existence filtering
        # keeps them.
        self.openspec_dir = self.root / "toolchain" / "openspec-bin"
        self.client_dir = self.root / "toolchain" / "client-bin"
        self.node_dir = self.root / "toolchain" / "node-bin"
        self.which_map: dict[str, str] = {}
        for name, directory in (
            ("openspec", self.openspec_dir),
            ("opencode", self.client_dir),
            ("node", self.node_dir),
        ):
            directory.mkdir(parents=True, exist_ok=True)
            exe = directory / name
            exe.write_text("#!/bin/sh\n", encoding="utf-8")
            exe.chmod(0o755)
            self.which_map[name] = str(exe)

        self._plan_env = mock.patch.dict(os.environ, {"OPSX_PLAN": ""})
        self._plan_env.start()
        self.addCleanup(self._plan_env.stop)

    # -- helpers ----------------------------------------------------------
    def args(self, *, plan="plan.toml", unit_name=None, print_only=False,
             no_enable=False) -> argparse.Namespace:
        return argparse.Namespace(
            repo=str(self.repo),
            plan=plan,
            unit_name=unit_name,
            print_only=print_only,
            no_enable=no_enable,
            action="install",
        )

    def which(self, name: str) -> str | None:
        return self.which_map.get(name)

    def install(self, runner=None, **kwargs):
        runner = runner or FakeRunner()
        out = io.StringIO()
        err = io.StringIO()
        call_kwargs: dict = {}
        for key in ("plan", "unit_name", "print_only", "no_enable"):
            if key in kwargs:
                call_kwargs[key] = kwargs.pop(key)
        rc = None
        with redirect_stdout(out), redirect_stderr(err):
            rc = cmd_autopilot_install.cmd_autopilot_install(
                self.args(**call_kwargs),
                runner=runner,
                which_func=self.which,
                user_unit_dir=self.unit_dir,
                template_root=REPO_ROOT,
                **kwargs,
            )
        return rc, out.getvalue(), err.getvalue(), runner

    def unit_path(self, name: str = DEFAULT_UNIT) -> Path:
        return self.unit_dir / f"{name}.service"

    def dropin_path(self, name: str = DEFAULT_UNIT) -> Path:
        return self.unit_dir / f"{name}.service.d" / "plan.conf"

    @property
    def resolved_plan(self) -> str:
        return str(self.plan.resolve())


class RenderTests(InstallHarness):
    def test_render_unit_verbatim_and_dropin_fields(self) -> None:
        rc, out, err, runner = self.install()
        self.assertEqual(rc, 0, err)

        unit = self.unit_path().read_text(encoding="utf-8")
        self.assertEqual(unit, UNIT_TEMPLATE.read_text(encoding="utf-8"))

        dropin = self.dropin_path().read_text(encoding="utf-8")
        self.assertIn(cmd_autopilot_install.GENERATED_HEADER.splitlines()[0], dropin)
        self.assertEqual(_dropin_value(dropin, "WorkingDirectory"), str(self.repo))
        self.assertEqual(
            _dropin_value(dropin, "Environment=OPSX_PLAN"), self.resolved_plan
        )
        # WorkingDirectory is bare: systemd keeps quotes literal in path
        # directives.  Environment values are quoted so systemd's parser
        # strips the quotes rather than splitting on embedded whitespace or
        # expanding `$`/`%`.
        self.assertIn(f"WorkingDirectory={self.repo}", dropin)
        self.assertIn(
            f'Environment=OPSX_PLAN="{self.resolved_plan}"', dropin
        )

        dirs = _dropin_value(dropin, "Environment=PATH").split(":")
        for directory in (self.openspec_dir, self.client_dir, self.node_dir):
            self.assertIn(str(directory.resolve()), dirs)
        self.assertIn("/usr/local/bin", dirs)
        self.assertIn("/usr/bin", dirs)
        # Toolchain dirs precede the systemd defaults, in resolver order.
        self.assertLess(
            dirs.index(str(self.openspec_dir.resolve())),
            dirs.index(str(self.client_dir.resolve())),
        )
        self.assertLess(
            dirs.index(str(self.client_dir.resolve())),
            dirs.index(str(self.node_dir.resolve())),
        )
        self.assertLess(
            dirs.index(str(self.node_dir.resolve())),
            dirs.index("/usr/local/sbin"),
        )

    def test_symlinked_toolchain_launcher_keeps_launcher_directory(self) -> None:
        # A symlinked launcher (npm global style) must contribute the symlink's
        # own directory to PATH, not the resolved target's directory.
        real = self.root / "toolchain" / "real-openspec"
        real.mkdir(parents=True)
        target = real / "openspec.js"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        link_dir = self.root / "toolchain" / "bin"
        link_dir.mkdir()
        link = link_dir / "openspec"
        link.symlink_to(target)
        self.which_map["openspec"] = str(link)

        rc, _, err, _ = self.install()
        self.assertEqual(rc, 0, err)
        dropin = self.dropin_path().read_text(encoding="utf-8")
        dirs = _dropin_value(dropin, "Environment=PATH").split(":")
        self.assertIn(str(link_dir), dirs)
        self.assertNotIn(str(real), dirs)

    def test_alternate_adapter_client_resolved(self) -> None:
        claude_dir = self.root / "toolchain" / "claude-bin"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "claude").write_text("#!/bin/sh\n", encoding="utf-8")
        self.which_map["claude"] = str(claude_dir / "claude")
        self.plan.write_text(
            '[plan]\nname = "install-test"\nadapter = "claude-code"\n\n'
            '[[changes]]\nid = "c1"\n',
            encoding="utf-8",
        )
        rc, _, err, _ = self.install()
        self.assertEqual(rc, 0, err)
        dropin = self.dropin_path().read_text(encoding="utf-8")
        self.assertIn(str(claude_dir.resolve()), dropin)


class IdempotencyTests(InstallHarness):
    def test_two_installs_are_byte_identical(self) -> None:
        rc1, _, err1, _ = self.install()
        self.assertEqual(rc1, 0, err1)
        unit_first = self.unit_path().read_bytes()
        dropin_first = self.dropin_path().read_bytes()

        rc2, _, err2, _ = self.install()
        self.assertEqual(rc2, 0, err2)
        self.assertEqual(self.unit_path().read_bytes(), unit_first)
        self.assertEqual(self.dropin_path().read_bytes(), dropin_first)


class DryRunTests(InstallHarness):
    def test_print_writes_nothing_and_skips_systemctl(self) -> None:
        rc, out, err, runner = self.install(print_only=True)
        self.assertEqual(rc, 0, err)
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.unit_path().exists())
        self.assertFalse(self.dropin_path().exists())
        self.assertFalse(self.unit_dir.exists())
        self.assertIn("opsx-autopilot.service", out)
        self.assertIn("plan.conf", out)
        self.assertIn(cmd_autopilot_install.GENERATED_HEADER.splitlines()[0], out)
        self.assertIn(UNIT_TEMPLATE.read_text(encoding="utf-8").strip(), out)


class UnitNameTests(InstallHarness):
    def test_alternate_unit_name_leaves_default_untouched(self) -> None:
        rc, _, err, _ = self.install()
        self.assertEqual(rc, 0, err)
        default_unit = self.unit_path().read_bytes()
        default_dropin = self.dropin_path().read_bytes()

        rc2, _, err2, _ = self.install(unit_name="opsx-autopilot-kf")
        self.assertEqual(rc2, 0, err2)
        self.assertEqual(self.unit_path().read_bytes(), default_unit)
        self.assertEqual(self.dropin_path().read_bytes(), default_dropin)

        alt_unit = self.unit_path("opsx-autopilot-kf")
        alt_dropin = self.dropin_path("opsx-autopilot-kf")
        self.assertTrue(alt_unit.is_file())
        self.assertTrue(alt_dropin.is_file())
        self.assertEqual(
            _dropin_value(alt_dropin.read_text(encoding="utf-8"), "WorkingDirectory"),
            str(self.repo),
        )

    def test_service_suffix_is_accepted(self) -> None:
        rc, _, err, _ = self.install(unit_name="opsx-autopilot-kf.service")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.unit_path("opsx-autopilot-kf").is_file())
        self.assertFalse((self.unit_dir / "opsx-autopilot-kf.service.service").exists())


class UnsafeUnitNameTests(InstallHarness):
    """`--unit-name` must never escape the user unit directory or systemctl."""

    UNSAFE_NAMES = [
        "",
        "   ",
        "/etc/systemd/system/evil",
        "../escape",
        "..",
        "foo/bar",
        "foo\\bar",
        "foo/../bar",
        ".hidden",
        "foo bar",
        "-leading-dash",
        "--user",
        "opsx\u0000autopilot",
        "opsx\nautopilot",
    ]

    def test_unsafe_names_rejected_without_writes_or_systemctl(self) -> None:
        for name in self.UNSAFE_NAMES:
            with self.subTest(unit_name=name):
                rc, out, err, runner = self.install(unit_name=name)
                self.assertEqual(rc, 2, (name, err))
                self.assertIn("invalid unit name", err)
                self.assertEqual(runner.calls, [])
                self.assertFalse(self.unit_dir.exists())

    def test_unsafe_name_rejected_in_print_mode(self) -> None:
        rc, out, err, runner = self.install(
            unit_name="../escape", print_only=True
        )
        self.assertEqual(rc, 2)
        self.assertIn("invalid unit name", err)
        self.assertEqual(out, "")
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.unit_dir.exists())

    def test_rejection_happens_before_plan_resolution(self) -> None:
        # Even a plan that cannot resolve must not turn an unsafe name into a
        # different (plan-specific) failure: the name is rejected first.
        rc, _, err, runner = self.install(unit_name="../escape", plan=None)
        self.assertEqual(rc, 2)
        self.assertIn("invalid unit name", err)
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.unit_dir.exists())


class EscapeTests(InstallHarness):
    def test_quote_encoder_escapes_systemd_metacharacters(self) -> None:
        raw = '$HOME 100% "quoted" \\back\\'
        encoded = cmd_autopilot_install._quote_systemd_value(raw)
        self.assertEqual(encoded, '"$$HOME 100%% \\"quoted\\" \\\\back\\\\"')
        self.assertEqual(_unquote_systemd(encoded), raw)

    def test_working_directory_is_rendered_bare(self) -> None:
        # systemd keeps quotes literal in WorkingDirectory and drops the rest
        # of the fragment when the directive fails, so the value must be bare.
        # `$` survives un-doubled (paths get no variable expansion) and `%` is
        # doubled against specifier expansion.
        encoded = cmd_autopilot_install._encode_working_directory(
            "/repo/$cash 100%"
        )
        self.assertEqual(encoded, "/repo/$cash 100%%")
        self.assertEqual(_unquote_systemd(encoded), "/repo/$cash 100%")

    def test_whitespace_and_escapes_survive_render(self) -> None:
        repo = self.root / "re po$%"
        repo.mkdir()
        plan = repo / "my plan.toml"
        plan.write_text(
            '[plan]\nname = "install-test"\nadapter = "opencode"\n\n'
            '[[changes]]\nid = "c1"\n',
            encoding="utf-8",
        )
        self.repo = repo
        self.plan = plan

        spaced_bin = self.root / "tool chain" / "bin"
        spaced_bin.mkdir(parents=True)
        exe = spaced_bin / "openspec"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        self.which_map["openspec"] = str(exe)

        rc, _, err, _ = self.install(plan="my plan.toml")
        self.assertEqual(rc, 0, err)

        dropin = self.dropin_path().read_text(encoding="utf-8")
        self.assertEqual(_dropin_value(dropin, "WorkingDirectory"), str(repo))
        self.assertEqual(
            _dropin_value(dropin, "Environment=OPSX_PLAN"), str(plan.resolve())
        )
        self.assertIn(
            str(spaced_bin.resolve()),
            _dropin_value(dropin, "Environment=PATH").split(":"),
        )
        # systemd expands `$`/`%` in Environment values and `%` in paths; the
        # rendered lines must carry the escaped forms so the parsed values are
        # the literal paths.
        self.assertIn("$$", dropin)
        self.assertIn("%%", dropin)


class FailureTests(InstallHarness):
    def test_unresolvable_plan_writes_nothing(self) -> None:
        rc, out, err, runner = self.install(plan=None)
        self.assertNotEqual(rc, 0)
        self.assertTrue(err.strip())
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.unit_dir.exists())

    def test_failing_daemon_reload_exits_nonzero(self) -> None:
        runner = FakeRunner(fail_on="daemon-reload", stderr="no user manager")
        rc, out, err, _ = self.install(runner=runner)
        self.assertNotEqual(rc, 0)
        self.assertIn("daemon-reload", err)
        self.assertIn("no user manager", err)
        # The files are still written before systemctl runs.
        self.assertTrue(self.unit_path().is_file())


class SystemctlInvocationTests(InstallHarness):
    def test_enable_by_default_and_never_start(self) -> None:
        rc, _, err, runner = self.install()
        self.assertEqual(rc, 0, err)
        self.assertIn(["systemctl", "--user", "daemon-reload"], runner.calls)
        self.assertIn(
            ["systemctl", "--user", "enable", DEFAULT_UNIT], runner.calls
        )
        flat = [arg for call in runner.calls for arg in call]
        self.assertNotIn("start", flat)

    def test_no_enable_skips_enable(self) -> None:
        rc, _, err, runner = self.install(no_enable=True)
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            runner.calls, [["systemctl", "--user", "daemon-reload"]]
        )


if __name__ == "__main__":
    unittest.main()
