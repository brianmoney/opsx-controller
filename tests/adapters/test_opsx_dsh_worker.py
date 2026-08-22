"""Unit tests for the dsh adapter worker shim.

The shim is exercised as a loaded module so its pure functions can be called
directly; ``main`` is tested with ``os.execvpe`` mocked so nothing ever execs.
No real ``dsh`` binary or Node is required.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_SHIM = (
    Path(__file__).resolve().parents[2]
    / "adapters" / "dsh" / "bin" / "opsx-dsh-worker"
)

_PINNED = "@deepseek-ai/dsh@0.1.0-rc.7"


def load_shim():
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("opsx_dsh_worker", str(_SHIM))
    spec = importlib.util.spec_from_loader("opsx_dsh_worker", loader)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_executable(directory: Path, name: str, content: str = "#!/bin/sh\n") -> Path:
    p = directory / name
    p.write_text(content, encoding="utf-8")
    os.chmod(p, 0o755)
    return p


class ShimTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.shim = load_shim()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _patch_home(self):
        return mock.patch.object(Path, "home", return_value=self.home)

    def _with_home(self):
        return mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "PATH": os.environ.get("PATH", "")},
            clear=False,
        )


class BinaryResolutionTests(ShimTestCase):
    def test_explicit_dsh_binary_path_wins_over_path_dsh(self) -> None:
        path_dir = Path(self.tmp.name) / "path"
        path_dir.mkdir()
        _make_executable(path_dir, "dsh")
        explicit = _make_executable(Path(self.tmp.name), "explicit-dsh")
        with mock.patch.dict(
            os.environ,
            {"DSH_BINARY": str(explicit), "PATH": str(path_dir)},
            clear=False,
        ):
            argv = self.shim.resolve_binary()
        self.assertEqual(argv, [str(explicit)])

    def test_dsh_binary_name_looked_up_on_path(self) -> None:
        path_dir = Path(self.tmp.name) / "path"
        path_dir.mkdir()
        fake = _make_executable(path_dir, "my-dsh")
        with mock.patch.dict(
            os.environ,
            {"DSH_BINARY": "my-dsh", "PATH": str(path_dir)},
            clear=False,
        ):
            argv = self.shim.resolve_binary()
        self.assertEqual(argv, [str(fake)])

    def test_dsh_on_path_used_when_no_dsh_binary(self) -> None:
        path_dir = Path(self.tmp.name) / "path"
        path_dir.mkdir()
        fake = _make_executable(path_dir, "dsh")
        with mock.patch.dict(
            os.environ,
            {"DSH_BINARY": "", "PATH": str(path_dir)},
            clear=False,
        ):
            argv = self.shim.resolve_binary()
        self.assertEqual(argv, [str(fake)])

    def test_pinned_npx_fallback(self) -> None:
        path_dir = Path(self.tmp.name) / "path"
        path_dir.mkdir()
        npx = _make_executable(path_dir, "npx")
        with mock.patch.dict(
            os.environ,
            {"DSH_BINARY": "", "PATH": str(path_dir)},
            clear=False,
        ):
            argv = self.shim.resolve_binary()
        self.assertEqual(argv, [str(npx), "--yes", _PINNED])

    def test_fails_closed_naming_all_three_sources(self) -> None:
        with mock.patch.dict(
            os.environ, {"DSH_BINARY": "", "PATH": "/nonexistent-dir"}, clear=False
        ), mock.patch("shutil.which", return_value=None):
            with self.assertRaises(self.shim.DshWorkerError) as ctx:
                self.shim.resolve_binary()
        message = str(ctx.exception)
        self.assertIn("DSH_BINARY", message)
        self.assertIn("PATH", message)
        self.assertIn(_PINNED, message)


class RoleInstructionTests(ShimTestCase):
    def test_missing_role_file_fails_closed_naming_it(self) -> None:
        missing_global = Path(self.tmp.name) / "missing-global"
        with mock.patch.object(Path, "cwd", return_value=Path(self.tmp.name)), \
             mock.patch.object(self.shim, "SUPPORT_DIR_GLOBAL", missing_global):
            with self.assertRaises(self.shim.DshWorkerError) as ctx:
                self.shim.resolve_role_instruction("reviewer")
        self.assertIn("reviewer", str(ctx.exception))

    def test_global_role_file_resolved_after_project_miss(self) -> None:
        global_dir = Path(self.tmp.name) / "global"
        global_dir.mkdir()
        (global_dir / "agents").mkdir()
        (global_dir / "agents" / "opsx-reviewer.md").write_text(
            "review instructions\n", encoding="utf-8"
        )
        with mock.patch.object(Path, "cwd", return_value=Path(self.tmp.name)), \
             mock.patch.object(self.shim, "SUPPORT_DIR_GLOBAL", global_dir):
            path = self.shim.resolve_role_instruction("reviewer")
        self.assertEqual(path, global_dir / "agents" / "opsx-reviewer.md")

    def test_project_role_file_wins_over_global(self) -> None:
        project_dir = Path(self.tmp.name) / "project"
        (project_dir / ".opsx-controller" / "dsh" / "agents").mkdir(parents=True)
        (project_dir / ".opsx-controller" / "dsh" / "agents" / "opsx-implementer.md").write_text(
            "project instructions\n", encoding="utf-8"
        )
        global_dir = Path(self.tmp.name) / "global"
        (global_dir / "agents").mkdir(parents=True)
        (global_dir / "agents" / "opsx-implementer.md").write_text(
            "global instructions\n", encoding="utf-8"
        )
        with mock.patch.object(Path, "cwd", return_value=project_dir), \
             mock.patch.object(self.shim, "SUPPORT_DIR_GLOBAL", global_dir):
            path = self.shim.resolve_role_instruction("implementer")
        self.assertEqual(
            path,
            project_dir / ".opsx-controller" / "dsh" / "agents" / "opsx-implementer.md",
        )

    def test_compose_prompt_joins_instructions_and_input(self) -> None:
        global_dir = Path(self.tmp.name) / "global"
        (global_dir / "agents").mkdir(parents=True)
        (global_dir / "agents" / "opsx-implementer.md").write_text(
            "implementer role instructions\n", encoding="utf-8"
        )
        with mock.patch.object(Path, "cwd", return_value=Path(self.tmp.name)), \
             mock.patch.object(self.shim, "SUPPORT_DIR_GLOBAL", global_dir):
            prompt = self.shim.compose_prompt(
                "implementer", "CHANGE: add-example\nROUND: 1\n"
            )
        self.assertIn("implementer role instructions", prompt)
        self.assertIn("CHANGE: add-example", prompt)
        self.assertIn("ROUND: 1", prompt)


class ModelPatchTests(ShimTestCase):
    def _patch_env(self, **overrides) -> mock._patch:
        env = {"DSH_HOME": str(self.tmp.name)}
        env.update(overrides)
        return mock.patch.dict(os.environ, env, clear=False)

    def test_patch_written_with_mapped_provider(self) -> None:
        with self._patch_env(OPSX_REVIEWER_MODEL="deepseek/deepseek-chat"):
            patch_path = self.shim.write_model_patch(Path(self.tmp.name), "reviewer")
        self.assertIsNotNone(patch_path)
        content = Path(patch_path).read_text(encoding="utf-8")
        self.assertIn("- id: agent-default-model", content)
        self.assertIn("config:", content)
        self.assertIn("provider: deepseek-official", content)
        self.assertIn("model: deepseek-chat", content)
        self.assertTrue(str(patch_path).startswith(str(Path(self.tmp.name) / "patches")))

    def test_provider_overlay_takes_precedence(self) -> None:
        overlay = json.dumps({"deepseek": "custom-provider"})
        with self._patch_env(
            OPSX_REVIEWER_MODEL="deepseek/deepseek-chat",
            OPSX_DSH_PROVIDER_MAP=overlay,
        ):
            patch_path = self.shim.write_model_patch(Path(self.tmp.name), "reviewer")
        content = Path(patch_path).read_text(encoding="utf-8")
        self.assertIn("provider: custom-provider", content)
        self.assertNotIn("deepseek-official", content)

    def test_unmapped_provider_passes_through(self) -> None:
        with self._patch_env(OPSX_IMPLEMENTER_MODEL="acme/acme-model"):
            patch_path = self.shim.write_model_patch(Path(self.tmp.name), "implementer")
        content = Path(patch_path).read_text(encoding="utf-8")
        self.assertIn("provider: acme", content)
        self.assertIn("model: acme-model", content)

    def test_no_model_env_returns_no_patch(self) -> None:
        with self._patch_env():  # OPSX_*_MODEL absent
            patch_path = self.shim.write_model_patch(Path(self.tmp.name), "archiver")
        self.assertIsNone(patch_path)

    def test_patch_contains_only_provider_model_no_extra_env_data(self) -> None:
        with self._patch_env(
            OPSX_IMPLEMENTER_MODEL="deepseek/deepseek-v4-pro",
            OPSX_DSH_PROVIDER_MAP=json.dumps({"deepseek": "deepseek-official"}),
            SOME_API_KEY="sk-keep-out-of-patch",
        ):
            patch_path = self.shim.write_model_patch(Path(self.tmp.name), "implementer")
        content = Path(patch_path).read_text(encoding="utf-8")
        self.assertIn("- id: agent-default-model", content)
        self.assertIn("provider: deepseek-official", content)
        self.assertIn("model: deepseek-v4-pro", content)
        self.assertNotIn("SOME_API_KEY", content)
        self.assertNotIn("sk-keep-out-of-patch", content)

    def test_provider_less_model_fails_closed(self) -> None:
        with self._patch_env(OPSX_REVIEWER_MODEL="deepseek-chat"):
            with self.assertRaises(self.shim.DshWorkerError) as ctx:
                self.shim.write_model_patch(Path(self.tmp.name), "reviewer")
        message = str(ctx.exception)
        self.assertIn("OPSX_REVIEWER_MODEL", message)
        self.assertIn("provider", message)


class VariantResolutionTests(ShimTestCase):
    def _variant_env(self, role: str, value: str) -> mock._patch:
        return mock.patch.dict(
            os.environ, {f"OPSX_{role.upper()}_VARIANT": value}, clear=False
        )

    def test_canonical_values_map_to_dsh_efforts(self) -> None:
        cases = {
            "low": "off",
            "medium": "low",
            "high": "high",
            "max": "max",
        }
        for value, expected in cases.items():
            with self._variant_env("implementer", value):
                self.assertEqual(self.shim.resolve_variant("implementer"), expected)

    def test_empty_variant_returns_empty(self) -> None:
        with self._variant_env("implementer", ""):
            self.assertEqual(self.shim.resolve_variant("implementer"), "")

    def test_missing_variant_env_returns_empty(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            self.assertEqual(self.shim.resolve_variant("archiver"), "")

    def test_unknown_label_warns_and_is_dropped(self) -> None:
        with self._variant_env("implementer", "turbo"), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            value = self.shim.resolve_variant("implementer")
        self.assertEqual(value, "")
        output = err.getvalue()
        self.assertIn("OPSX_IMPLEMENTER_VARIANT", output)
        self.assertIn("turbo", output)

    def test_unknown_label_omitted_from_settings(self) -> None:
        dsh_home = Path(self.tmp.name) / "state"
        with self._variant_env("implementer", "bogus"), \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            self.shim.apply_variant_settings(dsh_home, "implementer")
        settings_path = dsh_home / "settings.yaml"
        self.assertFalse(settings_path.is_file(), "unknown variant must not write settings")


class VariantSettingsTests(ShimTestCase):
    def _settings_path(self) -> Path:
        return Path(self.tmp.name) / "state" / "settings.yaml"

    def test_supported_variant_writes_reasoning_effort(self) -> None:
        with mock.patch.dict(
            os.environ, {"OPSX_IMPLEMENTER_VARIANT": "high"}, clear=False
        ):
            self.shim.apply_variant_settings(Path(self.tmp.name) / "state", "implementer")
        content = self._settings_path().read_text(encoding="utf-8")
        self.assertIn("agent-default-model:\n", content)
        self.assertIn("  reasoningEffort: high\n", content)

    def test_existing_operator_section_preserved(self) -> None:
        settings = self._settings_path()
        settings.parent.mkdir(parents=True)
        settings.write_text(
            "ui-onboarding:\n  welcomeNoticeVersion: 2026-08-13.1\n"
            "agent-default-model:\n  provider: openai-codex\n  model: gpt-5.6-luna\n",
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ, {"OPSX_IMPLEMENTER_VARIANT": "max"}, clear=False
        ):
            self.shim.apply_variant_settings(Path(self.tmp.name) / "state", "implementer")
        content = settings.read_text(encoding="utf-8")
        self.assertIn("ui-onboarding:\n  welcomeNoticeVersion: 2026-08-13.1\n", content)
        self.assertIn("agent-default-model:\n", content)
        self.assertIn("  provider: openai-codex\n", content)
        self.assertIn("  model: gpt-5.6-luna\n", content)
        self.assertIn("  reasoningEffort: max\n", content)

    def test_no_variant_removes_key_and_preserves_rest(self) -> None:
        settings = self._settings_path()
        settings.parent.mkdir(parents=True)
        settings.write_text(
            "agent-default-model:\n  provider: deepseek-official\n"
            "  model: deepseek-chat\n  reasoningEffort: low\n"
            "llm-deepseek:\n  apiKeyEnv: DEEPSEEK_API_KEY\n",
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ, {"OPSX_IMPLEMENTER_VARIANT": ""}, clear=False
        ):
            self.shim.apply_variant_settings(Path(self.tmp.name) / "state", "implementer")
        content = settings.read_text(encoding="utf-8")
        self.assertNotIn("reasoningEffort", content)
        self.assertIn("provider: deepseek-official", content)
        self.assertIn("model: deepseek-chat", content)
        self.assertIn("llm-deepseek:\n  apiKeyEnv: DEEPSEEK_API_KEY\n", content)

    def test_no_variant_removes_empty_section_header(self) -> None:
        settings = self._settings_path()
        settings.parent.mkdir(parents=True)
        settings.write_text(
            "agent-default-model:\n  reasoningEffort: low\n",
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ, {"OPSX_IMPLEMENTER_VARIANT": ""}, clear=False
        ):
            self.shim.apply_variant_settings(Path(self.tmp.name) / "state", "implementer")
        content = settings.read_text(encoding="utf-8")
        self.assertEqual(content, "")

    def test_update_in_place_when_key_present(self) -> None:
        settings = self._settings_path()
        settings.parent.mkdir(parents=True)
        settings.write_text(
            "agent-default-model:\n  reasoningEffort: low\n",
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ, {"OPSX_IMPLEMENTER_VARIANT": "low"}, clear=False
        ):
            self.shim.apply_variant_settings(Path(self.tmp.name) / "state", "implementer")
        content = settings.read_text(encoding="utf-8")
        self.assertEqual(content, "agent-default-model:\n  reasoningEffort: off\n")

    def test_inline_section_left_untouched(self) -> None:
        settings = self._settings_path()
        settings.parent.mkdir(parents=True)
        settings.write_text(
            "agent-default-model: {provider: deepseek-official, model: deepseek-chat}\n",
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ, {"OPSX_IMPLEMENTER_VARIANT": "low"}, clear=False
        ), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.shim.apply_variant_settings(Path(self.tmp.name) / "state", "implementer")
        self.assertEqual(
            settings.read_text(encoding="utf-8"),
            "agent-default-model: {provider: deepseek-official, model: deepseek-chat}\n",
        )
        self.assertIn("inline", err.getvalue())

    def test_unwritable_settings_does_not_raise(self) -> None:
        dsh_home = Path(self.tmp.name) / "state"
        with mock.patch.dict(
            os.environ, {"OPSX_IMPLEMENTER_VARIANT": "high"}, clear=False
        ), mock.patch.object(Path, "write_text", side_effect=OSError("permission denied")), \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            # Must not raise; the shim reports and continues to dispatch.
            self.shim.apply_variant_settings(dsh_home, "implementer")


class StalePatchSweepTests(ShimTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.patches = self.home / "patches"
        self.patches.mkdir(parents=True)

    def _touch(self, name: str, age_seconds: int) -> Path:
        p = self.patches / name
        p.write_text("stale\n", encoding="utf-8")
        ts = time.time() - age_seconds
        os.utime(p, (ts, ts))
        return p

    def test_stale_shim_patch_removed(self) -> None:
        self._touch("opsx-implementer-model-123.yml", age_seconds=7200)
        self.shim.sweep_stale_patches(self.home)
        self.assertEqual(list(self.patches.iterdir()), [])

    def test_fresh_shim_patch_preserved(self) -> None:
        self._touch("opsx-implementer-model-123.yml", age_seconds=60)
        self.shim.sweep_stale_patches(self.home)
        self.assertEqual([p.name for p in self.patches.iterdir()],
                         ["opsx-implementer-model-123.yml"])

    def test_foreign_and_operator_files_preserved(self) -> None:
        stale_shim = self._touch("opsx-reviewer-model-9.yml", age_seconds=7200)
        self._touch("operator-patch.yml", age_seconds=7200)
        self._touch("agent-default-model.yml", age_seconds=7200)
        self._touch("opsx-notes.txt", age_seconds=7200)
        self.shim.sweep_stale_patches(self.home)
        self.assertEqual(
            sorted(p.name for p in self.patches.iterdir()),
            ["agent-default-model.yml", "operator-patch.yml", "opsx-notes.txt"],
        )
        self.assertFalse(stale_shim.exists())

    def test_sweep_tolerates_filesystem_errors(self) -> None:
        self._touch("opsx-implementer-model-123.yml", age_seconds=7200)
        with mock.patch.object(Path, "stat", side_effect=OSError("stale handle")), \
             mock.patch.object(Path, "unlink", side_effect=OSError("locked")):
            self.shim.sweep_stale_patches(self.home)  # must not raise
        self.assertEqual([p.name for p in self.patches.iterdir()],
                         ["opsx-implementer-model-123.yml"])

    def test_sweep_missing_directory_is_noop(self) -> None:
        self.shim.sweep_stale_patches(self.home / "absent")  # must not raise

    def test_sweep_runs_before_new_patch_is_written(self) -> None:
        stale = self._touch("opsx-archiver-model-7.yml", age_seconds=7200)
        with mock.patch.dict(
            os.environ, {"OPSX_ARCHIVER_MODEL": "deepseek/deepseek-chat"}, clear=False
        ):
            new_patch = self.shim.write_model_patch(self.home, "archiver")
        self.assertFalse(stale.exists())
        self.assertIsNotNone(new_patch)
        self.assertEqual(
            sorted(p.name for p in self.patches.iterdir()), [new_patch.name]
        )


class EnvironmentTests(ShimTestCase):
    def test_dsh_home_precedence_ambient_wins(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"DSH_HOME": "/ambient", "OPSX_DSH_HOME": "/opsx"},
            clear=False,
        ):
            self.assertEqual(self.shim.resolve_dsh_home(), Path("/ambient"))

    def test_dsh_home_opsx_fallback(self) -> None:
        with mock.patch.dict(
            os.environ, {"DSH_HOME": "", "OPSX_DSH_HOME": "/opsx"}, clear=False
        ):
            self.assertEqual(self.shim.resolve_dsh_home(), Path("/opsx"))

    def test_dsh_home_default_under_state_dir(self) -> None:
        with mock.patch.dict(
            os.environ, {"DSH_HOME": "", "OPSX_DSH_HOME": ""}, clear=False
        ), self._patch_home():
            self.assertEqual(
                self.shim.resolve_dsh_home(),
                self.home / ".local" / "state" / "opsx-controller" / "dsh",
            )

    def test_controlled_env_applies_defaults_on_clean_environment(self) -> None:
        dsh_home = Path(self.tmp.name) / "state"
        saved = {
            key: os.environ.pop(key, None)
            for key in ("DSH_PERMISSION_MODE", "DSH_TOOLS_MODE", "DSH_TELEMETRY_DISABLED")
        }
        try:
            env = self.shim.controlled_env(dsh_home)
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value
        self.assertEqual(env["DSH_HOME"], str(dsh_home))
        self.assertEqual(env["DSH_PERMISSION_MODE"], "workspace-write")
        self.assertEqual(env["DSH_TOOLS_MODE"], "code")
        self.assertEqual(env["DSH_TELEMETRY_DISABLED"], "1")

    def test_controlled_env_preserves_operator_values(self) -> None:
        dsh_home = Path(self.tmp.name) / "state"
        with mock.patch.dict(
            os.environ,
            {
                "DSH_PERMISSION_MODE": "read-only",
                "DSH_TOOLS_MODE": "full",
                "DSH_TELEMETRY_DISABLED": "0",
            },
            clear=False,
        ):
            env = self.shim.controlled_env(dsh_home)
        self.assertEqual(env["DSH_PERMISSION_MODE"], "read-only")
        self.assertEqual(env["DSH_TOOLS_MODE"], "full")
        self.assertEqual(env["DSH_TELEMETRY_DISABLED"], "0")

    def test_startup_agents_written_only_when_absent(self) -> None:
        dsh_home = Path(self.tmp.name) / "state"
        self.shim.ensure_startup_agents(dsh_home)
        self.assertTrue((dsh_home / "AGENTS.md").is_file())
        original = (dsh_home / "AGENTS.md").read_text(encoding="utf-8")
        (dsh_home / "AGENTS.md").write_text("operator content\n", encoding="utf-8")
        self.shim.ensure_startup_agents(dsh_home)
        self.assertEqual((dsh_home / "AGENTS.md").read_text(encoding="utf-8"), "operator content\n")
        self.assertNotEqual(original, "operator content\n")


class ArgvConstructionTests(ShimTestCase):
    def test_build_argv_includes_patch_when_given(self) -> None:
        with mock.patch.dict(
            os.environ, {"DSH_BINARY": ""}, clear=False
        ), mock.patch("shutil.which", return_value="/fake/dsh"):
            argv = self.shim.build_argv("prompt text", Path("/patch.yml"))
        self.assertEqual(argv, ["/fake/dsh", "--profile", "headless", "--patch", "/patch.yml", "prompt text"])

    def test_build_argv_omits_patch_when_none(self) -> None:
        with mock.patch("shutil.which", return_value="/fake/dsh"):
            argv = self.shim.build_argv("prompt text", None)
        self.assertEqual(argv, ["/fake/dsh", "--profile", "headless", "prompt text"])


class MainExecTests(ShimTestCase):
    def _global_role(self) -> Path:
        global_dir = Path(self.tmp.name) / "global"
        (global_dir / "agents").mkdir(parents=True)
        (global_dir / "agents" / "opsx-implementer.md").write_text(
            "implementer role instructions\n", encoding="utf-8"
        )
        return global_dir

    def test_main_execs_dsh_with_composed_prompt(self) -> None:
        global_dir = self._global_role()
        exec_calls: list[tuple] = []

        def fake_execvpe(*args, **kwargs):
            exec_calls.append((args, kwargs))
            # A successful exec never returns; raising SystemExit keeps the
            # test from continuing past the shim's exec line.
            raise SystemExit(0)

        with mock.patch.object(self.shim, "SUPPORT_DIR_GLOBAL", global_dir), \
             mock.patch.object(Path, "cwd", return_value=Path(self.tmp.name)), \
             mock.patch("shutil.which", return_value="/fake/dsh"), \
             mock.patch.dict(
                 os.environ,
                 {"DSH_HOME": str(Path(self.tmp.name) / "state"),
                  "DSH_BINARY": ""},
                 clear=False,
             ), \
             mock.patch("os.execvpe", side_effect=fake_execvpe):
            with self.assertRaises(SystemExit):
                self.shim.main(["--role", "implementer", "CHANGE: add-example\nROUND: 1\n"])
        self.assertEqual(len(exec_calls), 1)
        (exec_file, exec_argv, exec_env), _ = exec_calls[0]
        self.assertEqual(exec_file, "/fake/dsh")
        self.assertEqual(exec_argv[0], "/fake/dsh")
        self.assertIn("--profile", exec_argv)
        self.assertIn("headless", exec_argv)
        prompt = exec_argv[-1]
        self.assertIn("implementer role instructions", prompt)
        self.assertIn("CHANGE: add-example", prompt)
        self.assertEqual(exec_env["DSH_HOME"], str(Path(self.tmp.name) / "state"))
        self.assertEqual(exec_env["DSH_PERMISSION_MODE"], "workspace-write")

    def test_main_returns_nonzero_and_diagnostic_on_missing_role_file(self) -> None:
        missing_global = Path(self.tmp.name) / "missing-global"
        with mock.patch.object(Path, "cwd", return_value=Path(self.tmp.name)), \
             mock.patch.object(self.shim, "SUPPORT_DIR_GLOBAL", missing_global), \
             mock.patch("os.execvpe") as m_exec, \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = self.shim.main(["--role", "archiver", "CHANGE: x\n"])
        self.assertEqual(rc, 1)
        m_exec.assert_not_called()
        self.assertIn("archiver", err.getvalue())

    def test_main_returns_nonzero_and_diagnostic_when_no_binary_resolves(self) -> None:
        global_dir = self._global_role()
        with mock.patch.object(self.shim, "SUPPORT_DIR_GLOBAL", global_dir), \
             mock.patch.object(Path, "cwd", return_value=Path(self.tmp.name)), \
             mock.patch("shutil.which", return_value=None), \
             mock.patch.dict(
                 os.environ, {"DSH_BINARY": "", "PATH": "/nonexistent"}, clear=False
             ), \
             mock.patch("os.execvpe") as m_exec, \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = self.shim.main(["--role", "implementer", "CHANGE: x\n"])
        self.assertEqual(rc, 1)
        m_exec.assert_not_called()
        self.assertIn("DSH_BINARY", err.getvalue())


if __name__ == "__main__":
    unittest.main()
