from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import re
import subprocess
import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path
from unittest import mock

from lib.models import resolver
from lib.models.types import ResolvedModel
from lib.orchestrator import compiler as compiler_mod
from lib.orchestrator import state as state_mod
from lib.orchestrator import base as base_mod
from lib.orchestrator import groundtruth as groundtruth_mod
from lib.orchestrator import planref as planref_mod

SCRIPT = Path(__file__).resolve().parents[2] / "orchestrator" / "opsx-plan.py"

# Pre-compiled regex for extracting the fenced TOML block emitted by
# build_schema_guidance.
_TOM_BLOCK = re.compile(r"```toml\s*\n(.*?)```", re.DOTALL)

_MODEL_HOME: tempfile.TemporaryDirectory | None = None
_MODEL_CONFIG_PATCH = None
_MODEL_ENV_PATCH = None


def setUpModule() -> None:
    global _MODEL_HOME, _MODEL_CONFIG_PATCH, _MODEL_ENV_PATCH
    _MODEL_HOME = tempfile.TemporaryDirectory()
    _MODEL_CONFIG_PATCH = mock.patch.object(
        resolver, "USER_CONFIG_PATH", Path(_MODEL_HOME.name) / "models.toml"
    )
    _MODEL_CONFIG_PATCH.start()
    _MODEL_ENV_PATCH = mock.patch.dict(
        os.environ,
        {
            "OPSX_CONTROLLER_MODEL": "test-provider/test-controller",
            "OPSX_IMPLEMENTER_MODEL": "test-provider/test-implementer",
            "OPSX_REVIEWER_MODEL": "test-provider/test-reviewer",
            "OPSX_ARCHIVER_MODEL": "test-provider/test-archiver",
        },
    )
    _MODEL_ENV_PATCH.start()


def tearDownModule() -> None:
    assert _MODEL_ENV_PATCH is not None
    assert _MODEL_CONFIG_PATCH is not None
    assert _MODEL_HOME is not None
    _MODEL_ENV_PATCH.stop()
    _MODEL_CONFIG_PATCH.stop()
    _MODEL_HOME.cleanup()


def load_opsx_plan():
    spec = importlib.util.spec_from_file_location("opsx_plan", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


class CompileTests(unittest.TestCase):
    """Tests for ``opsx-plan compile``: prompt construction, template
    injection, validation, error handling, and CLI routing."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init")
        git(
            self.repo,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test User",
            "commit",
            "-m",
            "init",
            "--allow-empty",
        )
        # Isolate model resolution from whatever the real machine's home
        # directory happens to contain, so these tests are hermetic.
        from lib.models import resolver as _resolver
        self._models_patch = mock.patch.object(
            _resolver, "USER_CONFIG_PATH", Path(self.tmp.name) / "unused-home" / "models.toml"
        )
        self._models_patch.start()
        self.addCleanup(self._models_patch.stop)
        # _set_model/_clear_model mutate OPSX_CONTROLLER_MODEL directly;
        # restore it so later test classes don't observe leftover state.
        self._original_controller_model = os.environ.get("OPSX_CONTROLLER_MODEL")
        self.addCleanup(self._restore_controller_model)

    def _restore_controller_model(self) -> None:
        if self._original_controller_model is not None:
            os.environ["OPSX_CONTROLLER_MODEL"] = self._original_controller_model
        else:
            os.environ.pop("OPSX_CONTROLLER_MODEL", None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_plan_md(self, rel_path: str, content: str) -> Path:
        p = self.repo / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def _set_model(self) -> None:
        import os as _os
        _os.environ["OPSX_CONTROLLER_MODEL"] = "test-provider/test-model"

    def _clear_model(self) -> None:
        import os as _os
        _os.environ.pop("OPSX_CONTROLLER_MODEL", None)

    # -- resolve / validation helpers (already covered by earlier tasks) --

    def test_resolve_compile_source_rejects_missing_file(self) -> None:
        with self.assertRaises(base_mod.PlanError) as ctx:
            compiler_mod.resolve_compile_source(self.repo, "nonexistent.md")
        self.assertIn("not found", str(ctx.exception))

    def test_resolve_compile_source_rejects_non_md_extension(self) -> None:
        p = self.repo / "plan.txt"
        p.write_text("text", encoding="utf-8")
        with self.assertRaises(base_mod.PlanError) as ctx:
            compiler_mod.resolve_compile_source(self.repo, "plan.txt")
        self.assertIn("must be a markdown file", str(ctx.exception))

    def test_resolve_compile_output_refuses_existing_without_force(self) -> None:
        p = self.repo / "out.toml"
        p.write_text("existing", encoding="utf-8")
        with self.assertRaises(base_mod.PlanError) as ctx:
            compiler_mod.resolve_compile_output(self.repo, "out.toml", force=False)
        self.assertIn("exists", str(ctx.exception))
        self.assertIn("--force", str(ctx.exception))

    def test_resolve_compile_output_allows_overwrite_with_force(self) -> None:
        p = self.repo / "out.toml"
        p.write_text("existing", encoding="utf-8")
        result = compiler_mod.resolve_compile_output(self.repo, "out.toml", force=True)
        self.assertEqual(result, p.resolve())

    def test_check_controller_model_fails_when_unset(self) -> None:
        self._clear_model()
        with self.assertRaises(base_mod.PlanError) as ctx:
            compiler_mod.check_controller_model()
        self.assertIn("controller model", str(ctx.exception))

    def test_check_controller_model_succeeds_when_set(self) -> None:
        self._set_model()
        model = compiler_mod.check_controller_model()
        self.assertEqual(model, "test-provider/test-model")

    # -- prompt construction --

    def test_build_compile_prompt_includes_source_content(self) -> None:
        content = "# My Plan\n\n## Phase 1\n\n### Change: `my-change`\n\n**Depends on:** None.\n"
        prompt = compiler_mod.build_compile_prompt(content, Path("/tmp/fake.md"), self.repo)
        self.assertIn("My Plan", prompt)
        self.assertIn("my-change", prompt)
        self.assertIn("Source plan markdown", prompt)

    def test_build_compile_prompt_includes_schema_guidance(self) -> None:
        prompt = compiler_mod.build_compile_prompt("content", Path("/tmp/fake.md"), self.repo)
        self.assertIn("[plan]", prompt)
        self.assertIn("[[changes]]", prompt)
        self.assertIn("depends_on", prompt)
        self.assertIn("pause_before", prompt)

    def test_build_compile_prompt_instructs_toml_only_output(self) -> None:
        prompt = compiler_mod.build_compile_prompt("content", Path("/tmp/fake.md"), self.repo)
        self.assertIn("Output only TOML", prompt)
        self.assertIn("fenced ```toml block", prompt)

    def test_build_compile_prompt_includes_dependency_semantics(self) -> None:
        prompt = compiler_mod.build_compile_prompt("content", Path("/tmp/fake.md"), self.repo)
        self.assertIn("become `depends_on`", prompt)
        self.assertIn("independence wording", prompt)
        self.assertIn("deferred", prompt.lower())

    def test_build_compile_prompt_instructs_plan_doc_reference(self) -> None:
        prompt = compiler_mod.build_compile_prompt("content", Path("/tmp/fake.md"), self.repo)
        self.assertIn("plan_doc", prompt)
        self.assertIn("/tmp/fake.md", prompt)

    def test_build_compile_prompt_includes_repo_relative_source_path(self) -> None:
        source = self._write_plan_md("openspec/plans/my-plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")
        prompt = compiler_mod.build_compile_prompt("# Plan\n", source, self.repo)
        self.assertIn('"openspec/plans/my-plan.md"', prompt)
        self.assertIn("plan_doc", prompt)

    def test_build_compile_prompt_includes_canonical_sample_when_no_repo_pairs(self) -> None:
        prompt = compiler_mod.build_compile_prompt("content", Path("/tmp/fake.md"), self.repo)
        self.assertIn("Sample plan (canonical)", prompt)
        self.assertIn("Sample manifest (canonical)", prompt)

    def test_build_compile_prompt_injects_template_pairs(self) -> None:
        plans_dir = self.repo / "openspec" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "example-plan.md").write_text("# Example plan\n", encoding="utf-8")
        (plans_dir / "example-plan.toml").write_text('[plan]\nname = "example"\n', encoding="utf-8")

        prompt = compiler_mod.build_compile_prompt("content", Path("/tmp/fake.md"), self.repo)
        self.assertIn("Example plan", prompt)
        self.assertIn("example-plan.toml", prompt)
        self.assertIn('name = "example"', prompt)

    def test_discover_template_pairs_returns_empty_when_no_plans_dir(self) -> None:
        pairs = compiler_mod.discover_template_pairs(self.repo)
        self.assertEqual(pairs, [])

    def test_discover_template_pairs_finds_md_and_toml(self) -> None:
        plans_dir = self.repo / "openspec" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "a.md").write_text("md", encoding="utf-8")
        (plans_dir / "a.toml").write_text("toml", encoding="utf-8")
        (plans_dir / "b.md").write_text("md2", encoding="utf-8")

        pairs = compiler_mod.discover_template_pairs(self.repo)
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0][0].name, "a.md")
        self.assertIsNotNone(pairs[0][1])
        self.assertEqual(pairs[1][0].name, "b.md")
        self.assertIsNone(pairs[1][1])

    # -- cmd_compile error handling --

    def test_cmd_compile_fails_without_model(self) -> None:
        self._clear_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")
        out = self.repo / "out.toml"
        args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                  output=str(out), force=False)
        with self.assertRaises(base_mod.PlanError) as ctx:
            self.opsx_plan.cmd_compile(args)
        self.assertIn("controller model", str(ctx.exception))

    def test_cmd_compile_fails_when_output_exists_without_force(self) -> None:
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")
        out = self.repo / "out.toml"
        out.write_text("existing", encoding="utf-8")
        args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                  output=str(out), force=False)
        with self.assertRaises(base_mod.PlanError) as ctx:
            self.opsx_plan.cmd_compile(args)
        self.assertIn("exists", str(ctx.exception))

    def test_cmd_compile_fails_when_source_not_found(self) -> None:
        self._set_model()
        out = self.repo / "out.toml"
        args = argparse.Namespace(repo=str(self.repo), source="missing.md",
                                  output=str(out), force=False)
        with self.assertRaises(base_mod.PlanError) as ctx:
            self.opsx_plan.cmd_compile(args)
        self.assertIn("not found", str(ctx.exception))

    # -- successful compile (mocked opencode) --

    def test_cmd_compile_success_with_valid_toml(self) -> None:
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")

        valid_toml = (
            '[plan]\nname = "test"\nadapter = "opencode"\n\n'
            "[[changes]]\nid = \"c1\"\nphase = 1\n"
        )

        def fake_run(repo, adapter, model, prompt):
            return valid_toml, ""

        original = compiler_mod.run_compile_client
        try:
            compiler_mod.run_compile_client = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=False, adapter="opencode")
            rc = self.opsx_plan.cmd_compile(args)
            self.assertEqual(rc, 0)
            self.assertTrue(out.is_file())
            content = out.read_text(encoding="utf-8")
            self.assertIn("c1", content)
        finally:
            compiler_mod.run_compile_client = original

    def test_cmd_compile_success_with_fenced_toml(self) -> None:
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")

        fenced_toml = (
            '```toml\n'
            '[plan]\nname = "test"\nadapter = "opencode"\n\n'
            "[[changes]]\nid = \"c1\"\nphase = 1\n"
            '```\n'
        )

        def fake_run(repo, adapter, model, prompt):
            return fenced_toml, ""

        original = compiler_mod.run_compile_client
        try:
            compiler_mod.run_compile_client = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=False)
            rc = self.opsx_plan.cmd_compile(args)
            self.assertEqual(rc, 0)
            self.assertTrue(out.is_file())
            content = out.read_text(encoding="utf-8")
            self.assertIn("c1", content)
        finally:
            compiler_mod.run_compile_client = original

    # -- invalid TOML rejection --

    def test_cmd_compile_rejects_invalid_toml(self) -> None:
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")

        def fake_run(repo, adapter, model, prompt):
            return "not valid toml {{{", ""

        original = compiler_mod.run_compile_client
        try:
            compiler_mod.run_compile_client = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=False)
            with self.assertRaises(base_mod.PlanError):
                self.opsx_plan.cmd_compile(args)
            self.assertFalse(out.is_file())
        finally:
            compiler_mod.run_compile_client = original

    def test_cmd_compile_rejects_empty_output(self) -> None:
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")

        def fake_run(repo, adapter, model, prompt):
            return "   ", ""

        original = compiler_mod.run_compile_client
        try:
            compiler_mod.run_compile_client = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=False)
            with self.assertRaises(base_mod.PlanError):
                self.opsx_plan.cmd_compile(args)
            self.assertFalse(out.is_file())
        finally:
            compiler_mod.run_compile_client = original

    def test_cmd_compile_rejects_toml_with_no_changes(self) -> None:
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")

        no_changes_toml = '[plan]\nname = "test"\n'

        def fake_run(repo, adapter, model, prompt):
            return no_changes_toml, ""

        original = compiler_mod.run_compile_client
        try:
            compiler_mod.run_compile_client = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=False)
            with self.assertRaises(base_mod.PlanError):
                self.opsx_plan.cmd_compile(args)
            self.assertFalse(out.is_file())
        finally:
            compiler_mod.run_compile_client = original

    def test_cmd_compile_rejects_unknown_dependency(self) -> None:
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")

        unknown_dep_toml = (
            '[plan]\nname = "test"\nadapter = "opencode"\n\n'
            "[[changes]]\nid = \"c1\"\nphase = 1\n"
            "depends_on = [\"nonexistent\"]\n"
        )

        def fake_run(repo, adapter, model, prompt):
            return unknown_dep_toml, ""

        original = compiler_mod.run_compile_client
        try:
            compiler_mod.run_compile_client = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=False)
            with self.assertRaises(base_mod.PlanError):
                self.opsx_plan.cmd_compile(args)
            self.assertFalse(out.is_file())
        finally:
            compiler_mod.run_compile_client = original

    def test_cmd_compile_rejects_duplicate_change_id(self) -> None:
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")

        dup_id_toml = (
            '[plan]\nname = "test"\nadapter = "opencode"\n\n'
            "[[changes]]\nid = \"c1\"\nphase = 1\n"
            "[[changes]]\nid = \"c1\"\nphase = 2\n"
        )

        def fake_run(repo, adapter, model, prompt):
            return dup_id_toml, ""

        original = compiler_mod.run_compile_client
        try:
            compiler_mod.run_compile_client = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=False)
            with self.assertRaises(base_mod.PlanError):
                self.opsx_plan.cmd_compile(args)
            self.assertFalse(out.is_file())
        finally:
            compiler_mod.run_compile_client = original

    def test_cmd_compile_does_not_overwrite_on_failure(self) -> None:
        """Even when --force is passed, an invalid model output must not
        overwrite an existing file."""
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")
        out = self.repo / "out.toml"
        out.write_text("original content", encoding="utf-8")

        def fake_run(repo, adapter, model, prompt):
            return "bad toml {{{", ""

        original = compiler_mod.run_compile_client
        try:
            compiler_mod.run_compile_client = fake_run
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=True)
            with self.assertRaises(base_mod.PlanError):
                self.opsx_plan.cmd_compile(args)
            self.assertEqual(out.read_text(encoding="utf-8"), "original content")
        finally:
            compiler_mod.run_compile_client = original

    def test_cmd_compile_rejects_scalar_plan_without_overwriting(self) -> None:
        """A scalar replacement for [plan] must be a validation error."""
        self._set_model()
        self._write_plan_md("plan.md", "# Plan\n")
        out = self.repo / "out.toml"
        out.write_text("original content", encoding="utf-8")

        malformed_toml = (
            'plan = "invalid"\n\n'
            '[[changes]]\nid = "c1"\n'
        )

        original = compiler_mod.run_compile_client
        try:
            compiler_mod.run_compile_client = lambda repo, adapter, model, prompt: (
                malformed_toml, ""
            )
            args = argparse.Namespace(
                repo=str(self.repo), source="plan.md",
                output=str(out), force=True, adapter="opencode",
            )
            with self.assertRaises(base_mod.PlanError) as ctx:
                self.opsx_plan.cmd_compile(args)
            self.assertIn("[plan]", str(ctx.exception))
            self.assertIn("table", str(ctx.exception))
            self.assertEqual(out.read_text(encoding="utf-8"), "original content")
        finally:
            compiler_mod.run_compile_client = original

    def test_cmd_compile_rejects_malformed_changes_without_overwriting(self) -> None:
        """Every [[changes]] entry must be a TOML table."""
        self._set_model()
        self._write_plan_md("plan.md", "# Plan\n")
        out = self.repo / "out.toml"
        out.write_text("original content", encoding="utf-8")

        malformed_toml = (
            'changes = ["invalid"]\n\n'
            '[plan]\nadapter = "opencode"\n'
        )

        original = compiler_mod.run_compile_client
        try:
            compiler_mod.run_compile_client = lambda repo, adapter, model, prompt: (
                malformed_toml, ""
            )
            args = argparse.Namespace(
                repo=str(self.repo), source="plan.md",
                output=str(out), force=True, adapter="opencode",
            )
            with self.assertRaises(base_mod.PlanError) as ctx:
                self.opsx_plan.cmd_compile(args)
            self.assertIn("[[changes]]", str(ctx.exception))
            self.assertIn("table", str(ctx.exception))
            self.assertEqual(out.read_text(encoding="utf-8"), "original content")
        finally:
            compiler_mod.run_compile_client = original

    # -- extract_toml --

    def test_extract_toml_from_fenced_block(self) -> None:
        output = '```toml\n[plan]\nname = "x"\n```\n'
        result = compiler_mod.extract_toml(output)
        self.assertIn('[plan]', result)
        self.assertNotIn('```', result)

    def test_extract_toml_from_bare_output(self) -> None:
        output = '[plan]\nname = "x"\n'
        result = compiler_mod.extract_toml(output)
        self.assertEqual(result, output.strip())

    def test_extract_toml_rejects_empty(self) -> None:
        with self.assertRaises(base_mod.PlanError):
            compiler_mod.extract_toml("   ")

    def test_extract_toml_rejects_no_toml(self) -> None:
        with self.assertRaises(base_mod.PlanError):
            compiler_mod.extract_toml("just some prose, no brackets")

    def test_extract_toml_rejects_multiple_fenced_blocks(self) -> None:
        output = (
            '```toml\n[plan]\nname = "x"\n```\n'
            '```toml\n[plan]\nname = "y"\n```\n'
        )
        with self.assertRaises(base_mod.PlanError) as ctx:
            compiler_mod.extract_toml(output)
        self.assertIn("multiple fenced", str(ctx.exception))

    def test_extract_toml_rejects_prose_before_fenced_block(self) -> None:
        output = "Here is the compiled plan:\n\n```toml\n[plan]\nname = \"x\"\n```\n"
        with self.assertRaises(base_mod.PlanError) as ctx:
            compiler_mod.extract_toml(output)
        self.assertIn("extra content found around", str(ctx.exception))

    def test_extract_toml_rejects_prose_after_fenced_block(self) -> None:
        output = "```toml\n[plan]\nname = \"x\"\n```\n\nLet me know if you need changes."
        with self.assertRaises(base_mod.PlanError) as ctx:
            compiler_mod.extract_toml(output)
        self.assertIn("extra content found around", str(ctx.exception))

    def test_extract_toml_accepts_clean_fenced_block_with_surrounding_whitespace(self) -> None:
        output = "\n\n```toml\n[plan]\nname = \"x\"\n```\n\n"
        result = compiler_mod.extract_toml(output)
        self.assertIn('[plan]', result)
        self.assertNotIn('```', result)

    # -- CLI parser coverage --

    def test_compile_subcommand_appears_in_help(self) -> None:
        """Prove ``compile`` appears in the subcommand list."""
        self.opsx_plan.sys = mock.Mock()
        stderr = io.StringIO()

        with mock.patch.object(self.opsx_plan.sys, "argv", ["opsx-plan", "--help"]), \
             mock.patch("sys.stdout", io.StringIO()) as stdout, \
             mock.patch("sys.stderr", stderr):
            # argparse calls sys.exit on --help; suppress it
            try:
                self.opsx_plan.main()
            except SystemExit:
                pass

        combined = stdout.getvalue() + stderr.getvalue()
        self.assertIn("compile", combined)

    def test_compile_subcommand_routes_to_cmd_compile(self) -> None:
        """Prove ``opsx-plan compile`` routes to ``cmd_compile``."""
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")
        out = self.repo / "out.toml"

        valid_toml = (
            '[plan]\nname = "test"\nadapter = "opencode"\n\n'
            "[[changes]]\nid = \"c1\"\nphase = 1\n"
        )

        def fake_run(repo, adapter, model, prompt):
            return valid_toml, ""

        original = compiler_mod.run_compile_client
        try:
            compiler_mod.run_compile_client = fake_run
            with mock.patch.object(
                self.opsx_plan.sys,
                "argv",
                ["opsx-plan", "--repo", str(self.repo),
                 "compile", "plan.md", "-o", str(out)],
            ):
                rc = self.opsx_plan.main()
            self.assertEqual(rc, 0)
            self.assertTrue(out.is_file())
        finally:
            compiler_mod.run_compile_client = original

    def test_run_compile_client_raises_on_spawn_failure(self) -> None:
        def fake_run(*args, **kwargs):
            raise FileNotFoundError("no opencode")

        with mock.patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(base_mod.PlanError) as ctx:
                compiler_mod.run_compile_client(self.repo, "opencode", "m", "prompt")
            self.assertIn("could not spawn", str(ctx.exception))

    def test_run_compile_client_passes_model_in_argv(self) -> None:
        """Verify ``run_compile_client`` spawns opencode with the
        configured model."""
        model = "configured-model-v1"
        prompt = "compile this plan"

        real_run = subprocess.run

        def fake_run(args, **kwargs):
            self.assertEqual(args[0], "opencode")
            self.assertEqual(args[1], "run")
            self.assertEqual(args[2], "--model")
            self.assertEqual(args[3], model)
            self.assertEqual(
                args[4],
                "Follow the complete compile instructions in the attached file. Output only TOML.",
            )
            self.assertEqual(args[5], "--file")
            self.assertEqual(Path(args[6]).read_text(encoding="utf-8"), prompt)
            result = mock.Mock()
            result.returncode = 0
            result.stdout = '[plan]\nname = "x"\n\n[[changes]]\nid = "c1"\n'
            result.stderr = ""
            return result

        with mock.patch("subprocess.run", side_effect=fake_run):
            stdout, stderr = compiler_mod.run_compile_client(
                self.repo, "opencode", model, prompt
            )

    def test_run_compile_client_raises_on_nonzero_exit(self) -> None:
        fake_result = mock.Mock()
        fake_result.returncode = 1
        fake_result.stdout = ""
        fake_result.stderr = "some error"

        with mock.patch("subprocess.run", return_value=fake_result):
            with self.assertRaises(base_mod.PlanError) as ctx:
                compiler_mod.run_compile_client(self.repo, "opencode", "m", "prompt")
            self.assertIn("exited with code 1", str(ctx.exception))

    def test_build_schema_guidance_includes_load_plan_fields(self) -> None:
        guidance = compiler_mod.build_schema_guidance()
        for field in ("name", "adapter", "implement_invoke",
                       "review_invoke", "archive_invoke", "timeout_minutes",
                       "max_rounds", "no_progress_limit", "fast_checks",
                       "plan_doc", "create_invoke", "pause_before", "depends_on",
                       "enabled", "phase", "id", "review_created"):
            self.assertIn(field, guidance,
                          f"schema guidance must mention field '{field}' consumed by load_plan()")

    def test_build_schema_guidance_toml_block_parses_for_each_adapter(self) -> None:
        """The TOML fenced block rendered by schema guidance must be valid TOML."""
        import tomllib

        for adapter in ("opencode", "claude-code"):
            guidance = compiler_mod.build_schema_guidance(adapter)
            m = _TOM_BLOCK.search(guidance)
            self.assertIsNotNone(m, f"{adapter}: no fenced toml block found")
            try:
                parsed = tomllib.loads(m.group(1))
            except Exception as exc:
                self.fail(f"{adapter}: generated TOML block must parse: {exc}")
            plan = parsed.get("plan", {})
            self.assertEqual(plan.get("adapter"), adapter,
                             f"{adapter}: adapter field must match")
            for key in ("state_file", "implement_invoke",
                         "review_invoke", "archive_invoke"):
                self.assertIn(key, plan,
                              f"{adapter}: [plan] must include '{key}'")

    # -- adapter-aware compile tests --

    def test_cmd_compile_codex_rejected_before_spawn(self) -> None:
        """Codex CLI compile exits non-zero before model resolution."""
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")
        out = self.repo / "out.toml"
        args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                  output=str(out), force=False, adapter="codex-cli")
        # Must not call subprocess.run at all — reject in cmd_compile itself.
        with mock.patch("subprocess.run") as m_run:
            rc = self.opsx_plan.cmd_compile(args)
        m_run.assert_not_called()
        self.assertNotEqual(rc, 0)

    def test_cmd_compile_claude_adapter_propagates_to_prompt(self) -> None:
        """Claude adapter appears in the compile prompt."""
        # _set_model() provides a provider-prefixed model valid for opencode
        # but rejected by claude-code. Override with a claude-valid model.
        os.environ["OPSX_CONTROLLER_MODEL"] = "claude-sonnet-5"
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")

        def fake_run(repo, adapter, model, prompt):
            self.assertEqual(adapter, "claude-code")
            self.assertIn("adapter defaults (claude-code)", prompt.lower())
            return (
                '[plan]\nname = "test"\nadapter = "claude-code"\n\n'
                "[[changes]]\nid = \"c1\"\nphase = 1\n",
                "",
            )

        original = compiler_mod.run_compile_client
        try:
            compiler_mod.run_compile_client = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=False,
                                      adapter="claude-code")
            rc = self.opsx_plan.cmd_compile(args)
            self.assertEqual(rc, 0)
        finally:
            compiler_mod.run_compile_client = original

    def test_cmd_compile_rejects_mismatched_adapter_in_manifest(self) -> None:
        """Generated manifest must have adapter matching the selected one."""
        os.environ["OPSX_CONTROLLER_MODEL"] = "claude-sonnet-5"
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")

        # Model returns opencode adapter despite claude-code selection.
        wrong_toml = (
            '[plan]\nname = "test"\nadapter = "opencode"\n\n'
            "[[changes]]\nid = \"c1\"\nphase = 1\n"
        )

        invoked = False

        def fake_run(repo, adapter, model, prompt):
            nonlocal invoked
            invoked = True
            return wrong_toml, ""

        original = compiler_mod.run_compile_client
        try:
            compiler_mod.run_compile_client = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=False,
                                      adapter="claude-code")
            with self.assertRaises(base_mod.PlanError) as ctx:
                self.opsx_plan.cmd_compile(args)
            self.assertIn("adapter", str(ctx.exception).lower())
            self.assertTrue(invoked)
            self.assertFalse(out.exists())
        finally:
            compiler_mod.run_compile_client = original

    def test_cmd_compile_unknown_adapter_exits_nonzero(self) -> None:
        """Unknown adapter name exits 2 before model resolution."""
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")
        out = self.repo / "out.toml"
        args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                  output=str(out), force=False,
                                  adapter="nonexistent")
        rc = self.opsx_plan.cmd_compile(args)
        self.assertEqual(rc, 2)

    # -- _build_compile_argv tests --

    def test_build_argv_for_opencode(self) -> None:
        prompt_file = self.repo / "compile-prompt.md"
        argv = compiler_mod._build_compile_argv(
            "opencode", "m1", "prompt text", prompt_file
        )
        self.assertIn("opencode", argv[0])
        self.assertIn("run", argv)
        self.assertIn("m1", argv)
        self.assertIn("--file", argv)
        self.assertIn(str(prompt_file), argv)
        self.assertLess(argv.index(
            "Follow the complete compile instructions in the attached file. Output only TOML."
        ), argv.index("--file"))
        self.assertNotIn("prompt text", argv)

    def test_build_argv_for_claude_code(self) -> None:
        argv = compiler_mod._build_compile_argv(
            "claude-code", "m2", "compile this"
        )
        self.assertIn("claude", argv[0])
        self.assertIn("-p", argv)
        self.assertIn("m2", argv)
        self.assertIn("compile this", argv)

    def test_build_argv_rejects_unsupported_codex(self) -> None:
        with self.assertRaises(base_mod.PlanError) as ctx:
            compiler_mod._build_compile_argv("codex-cli", "m", "prompt")
        self.assertIn("not supported", str(ctx.exception))

    # -- controller model syntax validation (reject before spawn) -----------

    def test_check_controller_model_rejects_provider_prefix_for_claude(self) -> None:
        """A provider-prefixed model (e.g. 'anthropic/claude-sonnet-5') is
        rejected for the claude-code adapter before any process spawn."""
        os.environ["OPSX_CONTROLLER_MODEL"] = "anthropic/claude-sonnet-5"
        try:
            with self.assertRaises(base_mod.PlanError) as ctx:
                compiler_mod.check_controller_model(adapter="claude-code")
            self.assertIn("not valid", str(ctx.exception))
            self.assertIn("claude-code", str(ctx.exception))
            self.assertIn("controller", str(ctx.exception))
        finally:
            os.environ.pop("OPSX_CONTROLLER_MODEL", None)

    def test_check_controller_model_rejects_missing_provider_prefix_for_opencode(self) -> None:
        """A model identifier without a 'provider/' prefix is rejected for
        the opencode adapter."""
        os.environ["OPSX_CONTROLLER_MODEL"] = "sonnet-direct"
        try:
            with self.assertRaises(base_mod.PlanError) as ctx:
                compiler_mod.check_controller_model(adapter="opencode")
            self.assertIn("not valid", str(ctx.exception))
            self.assertIn("opencode", str(ctx.exception))
            self.assertIn("controller", str(ctx.exception))
        finally:
            os.environ.pop("OPSX_CONTROLLER_MODEL", None)

    def test_check_controller_model_accepts_valid_opencode_model(self) -> None:
        """A valid provider/model identifier is accepted for opencode."""
        self._set_model()  # sets OPSX_CONTROLLER_MODEL = "test-provider/test-model"
        model = compiler_mod.check_controller_model(adapter="opencode")
        self.assertEqual(model, "test-provider/test-model")

    def test_check_controller_model_accepts_valid_claude_model(self) -> None:
        """A non-prefixed model identifier is accepted for claude-code."""
        os.environ["OPSX_CONTROLLER_MODEL"] = "claude-sonnet-5"
        try:
            model = compiler_mod.check_controller_model(adapter="claude-code")
            self.assertEqual(model, "claude-sonnet-5")
        finally:
            os.environ.pop("OPSX_CONTROLLER_MODEL", None)

    # -- compile client timeout ----------------------------------------------

    def test_run_compile_client_raises_on_timeout(self) -> None:
        """A compile client invocation that exceeds the timeout is reported
        as a PlanError."""
        def fake_run(args, **kwargs):
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 60))

        with mock.patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(base_mod.PlanError) as ctx:
                compiler_mod.run_compile_client(self.repo, "claude-code",
                                                   "m", "prompt")
            self.assertIn("timed out", str(ctx.exception).lower())

    def test_run_compile_client_opencode_attaches_and_removes_prompt_file(self) -> None:
        prompt = "large compile prompt"
        observed: dict[str, object] = {}
        result = mock.Mock(returncode=0, stdout="[plan]\n", stderr="")

        def fake_run(argv, **kwargs):
            prompt_file = Path(argv[argv.index("--file") + 1])
            observed["argv"] = argv
            observed["content"] = prompt_file.read_text(encoding="utf-8")
            observed["path"] = prompt_file
            return result

        with mock.patch("subprocess.run", side_effect=fake_run):
            stdout, stderr = compiler_mod.run_compile_client(
                self.repo, "opencode", "test-provider/test-model", prompt
            )

        self.assertEqual((stdout, stderr), ("[plan]\n", ""))
        self.assertEqual(observed["content"], prompt)
        self.assertLess(observed["argv"].index(
            "Follow the complete compile instructions in the attached file. Output only TOML."
        ), observed["argv"].index("--file"))
        self.assertNotIn(prompt, observed["argv"])
        self.assertFalse(observed["path"].exists())

    # -- _strip_claude_envelope / extract_toml tests --

    def test_strip_claude_json_result_envelope(self) -> None:
        output = '{"result": "[plan]\\nname = \\\"p\\\"\\nadapter = \\\"claude-code\\\"\\n\\n"}'
        stripped = compiler_mod._strip_claude_envelope(output)
        self.assertIn("[plan]", stripped)
        self.assertNotIn('{"result"', stripped)

    def test_strip_claude_envelope_preserves_non_json(self) -> None:
        output = '[plan]\nname = "p"\n'
        result = compiler_mod._strip_claude_envelope(output)
        self.assertEqual(result, output)

    def test_extract_toml_claude_jason_envelope(self) -> None:
        inner = '[plan]\nname = "p"\nadapter = "claude-code"\n\n[[changes]]\nid = "c1"\n'
        output = '{"result": "' + inner.replace('\n', '\\n').replace('"', '\\"') + '"}'
        result = compiler_mod.extract_toml(output, adapter="claude-code")
        self.assertIn("[plan]", result)

    def test_extract_toml_claude_plain_rejected_without_envelope(self) -> None:
        """Claude plain text without TOML content is rejected with named client."""
        with self.assertRaises(base_mod.PlanError) as ctx:
            compiler_mod.extract_toml("here is some prose", adapter="claude-code")
        self.assertIn("claude", str(ctx.exception).lower())

    def test_extract_toml_empty_opencode_mentions_opencode(self) -> None:
        with self.assertRaises(base_mod.PlanError) as ctx:
            compiler_mod.extract_toml("", adapter="opencode")
        self.assertIn("opencode", str(ctx.exception).lower())

    # -- Claude extraction edge cases ----------------------------------------

    def test_extract_toml_claude_strips_envelope_with_leading_whitespace(self) -> None:
        """Claude JSON envelope with leading/trailing whitespace is stripped."""
        inner = '[plan]\nname = "p"\nadapter = "claude-code"\n\n[[changes]]\nid = "c1"\n'
        escaped = inner.replace('\n', '\\n').replace('"', '\\"')
        output = ' \n {"result": "' + escaped + '"} \n'
        result = compiler_mod.extract_toml(output, adapter="claude-code")
        self.assertIn("[plan]", result)

    def test_extract_toml_claude_envelope_with_fenced_toml_inside_result(self) -> None:
        """When the envelope result string contains a fenced TOML block,
        the envelope is stripped before the fenced-block extraction."""
        inner = '```toml\n[plan]\nname = "p"\nadapter = "claude-code"\n\n[[changes]]\nid = "c1"\n```\n'
        escaped = json.dumps(inner)
        output = '{"result": ' + escaped + '}'
        result = compiler_mod.extract_toml(output, adapter="claude-code")
        self.assertIn("[plan]", result)
        self.assertNotIn("```", result)

    def test_extract_toml_claude_handles_result_not_a_string(self) -> None:
        """Claude envelope whose result is not a string falls through to
        standard extraction (e.g. result is a dict)."""
        output = '{"result": {"status": "ok"}}'
        with self.assertRaises(base_mod.PlanError) as ctx:
            compiler_mod.extract_toml(output, adapter="claude-code")
        self.assertIn("claude", str(ctx.exception).lower())

    def test_extract_toml_claude_handles_broken_json_gracefully(self) -> None:
        """Claude output that looks like JSON but is broken falls through
        without crashing the extractor."""
        # Malformed JSON that starts with `{` but is not parseable.
        output = '{"result": unfinished'
        with self.assertRaises(base_mod.PlanError) as ctx:
            compiler_mod.extract_toml(output, adapter="claude-code")
        self.assertIn("claude", str(ctx.exception).lower())

    def test_extract_toml_claude_envelope_result_empty_string(self) -> None:
        """Claude envelope with empty result string is treated as no TOML."""
        output = '{"result": ""}'
        with self.assertRaises(base_mod.PlanError) as ctx:
            compiler_mod.extract_toml(output, adapter="claude-code")
        self.assertIn("claude", str(ctx.exception).lower())

    # -- Claude compile client-level error and rejection tests ---------------

    def test_run_compile_client_claude_spawn_failure(self) -> None:
        """Claude compile client raises PlanError when the 'claude' binary
        is not on PATH (FileNotFoundError), reporting the missing executable
        by name."""
        def fake_run(*args, **kwargs):
            raise FileNotFoundError("no claude")

        with mock.patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(base_mod.PlanError) as ctx:
                compiler_mod.run_compile_client(
                    self.repo, "claude-code", "m", "prompt"
                )
            self.assertIn("could not spawn", str(ctx.exception))
            self.assertIn("claude", str(ctx.exception))

    def test_run_compile_client_claude_nonzero_exit(self) -> None:
        """Claude compile client raises PlanError with exit code and stderr
        when the Claude process exits non-zero."""
        fake_result = mock.Mock()
        fake_result.returncode = 1
        fake_result.stdout = ""
        fake_result.stderr = "Claude error: model not configured"

        with mock.patch("subprocess.run", return_value=fake_result):
            with self.assertRaises(base_mod.PlanError) as ctx:
                compiler_mod.run_compile_client(
                    self.repo, "claude-code", "m", "prompt"
                )
            self.assertIn("exited with code 1", str(ctx.exception))
            self.assertIn("Claude error", str(ctx.exception))

    def test_cmd_compile_claude_raw_toml_no_envelope(self) -> None:
        """Claude compile succeeds when output is plain TOML (no JSON
        envelope, no fenced block)."""
        self._set_model()
        os.environ["OPSX_CONTROLLER_MODEL"] = "claude-sonnet-5"
        source = self._write_plan_md(
            "plan.md",
            "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n",
        )

        raw_toml = (
            '[plan]\nname = "test"\nadapter = "claude-code"\n\n'
            "[[changes]]\nid = \"c1\"\nphase = 1\n"
        )

        def fake_run(repo, adapter, model, prompt):
            return raw_toml, ""

        original = compiler_mod.run_compile_client
        try:
            compiler_mod.run_compile_client = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(
                repo=str(self.repo), source="plan.md",
                output=str(out), force=False, adapter="claude-code",
            )
            rc = self.opsx_plan.cmd_compile(args)
            self.assertEqual(rc, 0)
            self.assertTrue(out.is_file())
            content = out.read_text(encoding="utf-8")
            self.assertIn("c1", content)
            self.assertIn("claude-code", content)
        finally:
            compiler_mod.run_compile_client = original
            os.environ.pop("OPSX_CONTROLLER_MODEL", None)

    def test_cmd_compile_claude_fenced_toml_no_envelope(self) -> None:
        """Claude compile succeeds when output is a fenced TOML block with
        no JSON envelope — the generic fenced-block extraction applies."""
        self._set_model()
        os.environ["OPSX_CONTROLLER_MODEL"] = "claude-sonnet-5"
        source = self._write_plan_md(
            "plan.md",
            "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n",
        )

        fenced_toml = (
            '```toml\n'
            '[plan]\nname = "test"\nadapter = "claude-code"\n\n'
            "[[changes]]\nid = \"c1\"\nphase = 1\n"
            '```\n'
        )

        def fake_run(repo, adapter, model, prompt):
            return fenced_toml, ""

        original = compiler_mod.run_compile_client
        try:
            compiler_mod.run_compile_client = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(
                repo=str(self.repo), source="plan.md",
                output=str(out), force=False, adapter="claude-code",
            )
            rc = self.opsx_plan.cmd_compile(args)
            self.assertEqual(rc, 0)
            self.assertTrue(out.is_file())
            content = out.read_text(encoding="utf-8")
            self.assertIn("c1", content)
            self.assertIn("claude-code", content)
        finally:
            compiler_mod.run_compile_client = original
            os.environ.pop("OPSX_CONTROLLER_MODEL", None)

    def test_cmd_compile_claude_rejects_prose_output(self) -> None:
        """Claude compile raises PlanError when output is plain prose with
        no TOML content — the extraction fails with a client-named message."""
        self._set_model()
        os.environ["OPSX_CONTROLLER_MODEL"] = "claude-sonnet-5"
        source = self._write_plan_md(
            "plan.md",
            "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n",
        )

        def fake_run(repo, adapter, model, prompt):
            return "I cannot compile this plan because it lacks change entries.", ""

        original = compiler_mod.run_compile_client
        try:
            compiler_mod.run_compile_client = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(
                repo=str(self.repo), source="plan.md",
                output=str(out), force=False, adapter="claude-code",
            )
            with self.assertRaises(base_mod.PlanError) as ctx:
                self.opsx_plan.cmd_compile(args)
            self.assertIn("claude", str(ctx.exception).lower())
            self.assertIn("TOML", str(ctx.exception))
            self.assertFalse(out.is_file())
        finally:
            compiler_mod.run_compile_client = original
            os.environ.pop("OPSX_CONTROLLER_MODEL", None)

    # -- default compile output and auto-activation (7.8) -------------------

    def test_compile_no_output_flag_defaults_and_activates(self) -> None:
        """7.8 — compile with no -o writes to openspec/plans/<stem>.toml
        and auto-activates the active-plan pointer."""
        self._set_model()
        source = self._write_plan_md(
            "openspec/plans/my-plan.md",
            "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n",
        )

        valid_toml = (
            '[plan]\nname = "test"\nadapter = "opencode"\n\n'
            "[[changes]]\nid = \"c1\"\nphase = 1\n"
        )

        def fake_run(repo, adapter, model, prompt):
            return valid_toml, ""

        original = compiler_mod.run_compile_client
        try:
            compiler_mod.run_compile_client = fake_run
            args = argparse.Namespace(
                repo=str(self.repo), source="openspec/plans/my-plan.md",
                output=None, force=False, adapter="opencode",
            )
            rc = self.opsx_plan.cmd_compile(args)
            self.assertEqual(rc, 0)
            default_out = self.repo / "openspec" / "plans" / "my-plan.toml"
            self.assertTrue(default_out.is_file(),
                            f"expected default output at {default_out}")
            content = default_out.read_text(encoding="utf-8")
            self.assertIn("c1", content)
            # Auto-activation: active-plan pointer must reference the compiled plan
            active = self.opsx_plan.planref.read_active_plan(self.repo)
            self.assertEqual(
                active, "openspec/plans/my-plan.toml",
                "compile without -o must auto-activate the output plan",
            )
        finally:
            compiler_mod.run_compile_client = original



class CompileOptionalOutputTests(unittest.TestCase):
    """7.8–7.9: compile default output and discover_template_pairs ordering."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        git(
            self.repo,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test User",
            "commit",
            "-m",
            "init",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_discover_template_pairs_includes_archived(self):
        """7.9"""
        plans_dir = self.repo / "openspec" / "plans"
        plans_dir.mkdir(parents=True)
        archived_dir = plans_dir / "archived"
        archived_dir.mkdir(parents=True)

        (plans_dir / "active.md").write_text("# active\n", encoding="utf-8")
        (plans_dir / "active.toml").write_text("", encoding="utf-8")
        (archived_dir / "done.md").write_text("# done\n", encoding="utf-8")
        (archived_dir / "done.toml").write_text("", encoding="utf-8")

        pairs = compiler_mod.discover_template_pairs(self.repo)
        self.assertEqual(len(pairs), 2)
        # The active pair must come first
        first_md = pairs[0][0]
        self.assertIn("active.md", str(first_md))
        second_md = pairs[1][0]
        self.assertIn("done.md", str(second_md))


class SamplePlanTests(unittest.TestCase):
    """7.10–7.13: Canonical sample plan pair."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_sample_toml_loads(self):
        """7.10 — load_plan preserves pause_before gates and dependency edges"""
        sample_dir = (
            Path(__file__).resolve().parents[2] / "orchestrator" / "samples"
        )
        toml_path = sample_dir / "sample-plan.toml"
        md_path = sample_dir / "sample-plan.md"
        self.assertTrue(toml_path.is_file(), f"sample not found: {toml_path}")
        self.assertTrue(md_path.is_file())

        cfg = self.opsx_plan.planref.load_plan(toml_path)
        self.assertEqual(cfg["name"], "sample-implementation-plan")
        self.assertEqual(cfg["adapter"], "opencode")

        changes = cfg["changes"]
        expected_ids = {
            "add-input-validation", "add-unit-tests",
            "fix-tax-calculation", "add-discount-code-verification",
            "integrate-payment-gateway-v2",
        }
        self.assertEqual(set(changes.keys()), expected_ids)

        # Dependency edges
        self.assertEqual(
            set(changes["fix-tax-calculation"]["depends_on"]),
            {"add-unit-tests"},
        )
        self.assertEqual(
            set(changes["integrate-payment-gateway-v2"]["depends_on"]),
            {"add-input-validation", "add-discount-code-verification"},
        )
        self.assertEqual(
            changes["add-unit-tests"]["depends_on"], [],
        )

        # pause_before gates
        self.assertTrue(
            changes["add-input-validation"]["pause_before"],
            "add-input-validation must have pause_before = true",
        )
        self.assertTrue(
            changes["integrate-payment-gateway-v2"]["pause_before"],
            "integrate-payment-gateway-v2 must have pause_before = true",
        )
        self.assertFalse(
            changes["add-unit-tests"]["pause_before"],
            "add-unit-tests must have pause_before = false",
        )
        gated = cfg["order"][0]
        self.assertEqual(gated, "add-input-validation")

    def test_sample_exercises_full_surface(self):
        """7.11"""
        import tomllib
        sample_dir = (
            Path(__file__).resolve().parents[2] / "orchestrator" / "samples"
        )
        toml_path = sample_dir / "sample-plan.toml"
        raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))

        plan = raw["plan"]
        # Every plan key the loader reads (except adapter defaults) must be present
        plan_keys = set(plan.keys())
        # The loader silently drops unknown keys — we assert the sample carries
        # no keys the loader ignores
        known_plan_keys = {
            "name", "adapter", "state_file",
            "implement_invoke", "review_invoke", "archive_invoke",
            "timeout_minutes", "max_rounds", "no_progress_limit",
            "escalate_after_review_fails", "finding_recurrence_limit",
            "fast_checks", "check_timeout_minutes", "require_clean_tracked",
            "skip_warning", "skip_suggestion",
            "notify_cmd", "plan_doc", "create_invoke",
            "create_timeout_minutes", "create_max_attempts",
            "review_created", "created_check", "git_delivery",
        }
        unknown = plan_keys - known_plan_keys
        self.assertEqual(
            unknown, set(),
            f"sample carries keys the loader ignores: {unknown}",
        )
        # Assert every known plan key is present in the sample
        missing_plan = known_plan_keys - plan_keys
        self.assertEqual(
            missing_plan, set(),
            f"sample is missing plan keys the loader reads: {missing_plan}",
        )

        known_change_keys = {
            "id", "phase", "depends_on", "pause_before", "enabled",
            "timeout_minutes", "create_invoke", "create_max_attempts",
        }
        all_change_keys: set[str] = set()
        for change in raw["changes"]:
            unknown = set(change.keys()) - known_change_keys
            self.assertEqual(
                unknown, set(),
                f"change {change.get('id')} carries keys the loader ignores: {unknown}",
            )
            all_change_keys |= set(change.keys())
        # Assert all known change keys appear in at least one change entry
        missing_change = known_change_keys - all_change_keys
        self.assertEqual(
            missing_change, set(),
            f"sample is missing change keys the loader reads: {missing_change}",
        )

    def test_sample_files_resolve_via_helper(self):
        """7.13"""
        pair = compiler_mod.resolve_sample_plan_pair()
        self.assertIsNotNone(pair, "resolve_sample_plan_pair must find the canonical pair")
        md, toml = pair
        self.assertTrue(toml.is_file())
        self.assertTrue(md.is_file())

    def test_build_compile_prompt_includes_sample(self):
        """7.12 — with no repo plans, sample appears and no 'no pairs' text"""
        prompt = compiler_mod.build_compile_prompt(
            "# Source\n", Path("docs/test.md"), self.repo, adapter="opencode",
        )
        self.assertIn("Sample plan (canonical)", prompt)
        self.assertIn("Sample manifest (canonical)", prompt)
        self.assertNotIn("No `openspec/plans/*.md` template plan pairs were found", prompt)

    def test_sample_pair_ordered_before_repo_pairs(self):
        """7.13"""
        plans_dir = self.repo / "openspec" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "repo-plan.md").write_text("# repo\n", encoding="utf-8")
        (plans_dir / "repo-plan.toml").write_text("", encoding="utf-8")

        prompt = compiler_mod.build_compile_prompt(
            "# Source\n", Path("docs/test.md"), self.repo, adapter="opencode",
        )
        sample_idx = prompt.find("Sample plan (canonical)")
        repo_idx = prompt.find("Repository template plans")
        self.assertNotEqual(sample_idx, -1)
        self.assertNotEqual(repo_idx, -1)
        self.assertLess(sample_idx, repo_idx,
                        "canonical sample must appear before repo pairs")

    def test_installed_samples_preferred_over_checkout(self):
        """Lifecycle/drift: installed samples take precedence over checkout
        when both exist.  The installed copy is the one the operator actually
        deployed, so it must be authoritative."""
        import pathlib
        import unittest.mock as um

        # Create an installed samples directory with distinct content.
        fake_home = self.repo / ".fake-home"
        installed_samples = (
            fake_home / ".local" / "lib" / "opsx-controller" / "samples"
        )
        installed_samples.mkdir(parents=True)
        installed_md = installed_samples / "sample-plan.md"
        installed_toml = installed_samples / "sample-plan.toml"
        installed_md.write_text("# installed sample precedence\n", encoding="utf-8")
        installed_toml.write_text(
            '[plan]\nname = "installed-precedence"\nadapter = "opencode"\n\n'
            '[[changes]]\nid = "precedence-ch"\n',
            encoding="utf-8",
        )

        # Patch Path.home() so the installed probe returns our fake home.
        # The function checks installed first, so it should return the
        # installed pair even when checkout samples also exist.
        with um.patch.object(pathlib.Path, "home", return_value=fake_home):
            pair = compiler_mod.resolve_sample_plan_pair()
            self.assertIsNotNone(
                pair, "resolve_sample_plan_pair must find installed sample"
            )
            md, toml = pair
            self.assertIn(
                "installed sample precedence",
                md.read_text(encoding="utf-8"),
                "must prefer installed sample content",
            )

    def test_checkout_fallback_when_installed_absent(self):
        """Lifecycle/drift: when installed samples are absent, fall back to
        the checkout copy."""
        import pathlib
        import unittest.mock as um

        temp_home = self.repo / ".empty-home"
        temp_home.mkdir(parents=True)
        # Ensure the installed samples path exists but is empty.
        (temp_home / ".local" / "lib" / "opsx-controller" / "samples").mkdir(
            parents=True
        )

        with um.patch.object(pathlib.Path, "home", return_value=temp_home):
            pair = compiler_mod.resolve_sample_plan_pair()
            self.assertIsNotNone(
                pair, "must fall back to checkout sample when installed is absent"
            )

    def test_resolve_sample_plan_pair_none_when_both_missing(self):
        """Lifecycle: resolve_sample_plan_pair returns None when neither
        installed nor checkout locations hold samples."""
        import pathlib
        import unittest.mock as um

        temp_home = self.repo / ".no-samples-home"
        temp_home.mkdir(parents=True)
        (temp_home / ".local" / "lib" / "opsx-controller" / "samples").mkdir(
            parents=True
        )

        # Patch base._RUNTIME_ROOTS to point at a directory without samples.
        fake_root = self.repo / ".fake-orchestrator-root"
        fake_root.mkdir(parents=True)
        (fake_root / "orchestrator").mkdir(parents=True)
        # No samples/ dir under orchestrator/.

        with um.patch.object(pathlib.Path, "home", return_value=temp_home):
            with um.patch.object(
                base_mod, "_RUNTIME_ROOTS", (fake_root,),
            ):
                pair = compiler_mod.resolve_sample_plan_pair()
                self.assertIsNone(
                    pair, "must return None when no sample pair exists"
                )

    def test_both_sample_gates_load_and_exercise_full_surface(self):
        """Assert the canonical sample pair passes load_plan + field surface
        checks from both the installed and checkout resolution gates."""
        import pathlib
        import tomllib
        import unittest.mock as um

        checkout_dir = (
            Path(__file__).resolve().parents[2] / "orchestrator" / "samples"
        )
        checkout_toml = checkout_dir / "sample-plan.toml"
        checkout_md = checkout_dir / "sample-plan.md"

        known_plan_keys = {
            "name", "adapter", "state_file",
            "implement_invoke", "review_invoke", "archive_invoke",
            "timeout_minutes", "max_rounds",
            "no_progress_limit", "escalate_after_review_fails",
            "finding_recurrence_limit",
            "fast_checks", "check_timeout_minutes",
            "require_clean_tracked", "skip_warning", "skip_suggestion",
            "notify_cmd", "plan_doc",
            "create_invoke", "create_timeout_minutes", "create_max_attempts",
            "review_created", "created_check", "git_delivery",
        }

        def _assert_sample_surface(toml_path, label):
            """Gate: load_plan succeeds and no loader-ignored keys exist."""
            cfg = self.opsx_plan.planref.load_plan(toml_path)
            self.assertEqual(cfg["name"], "sample-implementation-plan",
                             f"{label}: plan name mismatch")
            raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            unknown = set(raw["plan"].keys()) - known_plan_keys
            self.assertEqual(unknown, set(),
                             f"{label}: carries unknown plan keys: {unknown}")

        # --- Gate 1: checkout fallback (always available) ---
        empty_home = self.repo / ".empty-home-for-gate1"
        empty_home.mkdir(parents=True)
        (empty_home / ".local" / "lib" / "opsx-controller" / "samples").mkdir(
            parents=True
        )
        with um.patch.object(pathlib.Path, "home", return_value=empty_home):
            pair = compiler_mod.resolve_sample_plan_pair()
            self.assertIsNotNone(pair, "checkout fallback must resolve")
            md, toml = pair
            _assert_sample_surface(toml, "checkout gate")

        # --- Gate 2: installed samples (deterministic) ---
        fake_home = self.repo / ".fake-home-for-gate2"
        fake_home.mkdir(parents=True)
        installed_samples = (
            fake_home / ".local" / "lib" / "opsx-controller" / "samples"
        )
        installed_samples.mkdir(parents=True)
        # Copy the real sample files into the fake installed location so
        # the gate exercises identical content.
        import shutil
        shutil.copy2(checkout_toml, installed_samples / "sample-plan.toml")
        shutil.copy2(checkout_md, installed_samples / "sample-plan.md")

        with um.patch.object(pathlib.Path, "home", return_value=fake_home):
            pair = compiler_mod.resolve_sample_plan_pair()
            self.assertIsNotNone(pair, "installed gate must resolve")
            md, toml = pair
            self.assertIn(
                str(fake_home), str(toml),
                "installed gate must return the installed path, not checkout",
            )
            _assert_sample_surface(toml, "installed gate")

    def test_installer_deploys_samples(self):
        """Verify the installer deploys sample files to the runtime
        samples directory."""
        import pathlib
        import shutil
        from unittest import mock as um

        fake_home = self.repo / ".fake-home-install"
        fake_home.mkdir()

        installer = (
            Path(__file__).resolve().parents[2]
            / "scripts" / "install-orchestrator.sh"
        )
        checkout = Path(__file__).resolve().parents[2]

        env = {**os.environ, "HOME": str(fake_home)}
        result = subprocess.run(
            ["bash", str(installer), str(checkout)],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(
            result.returncode, 0,
            f"installer failed: {result.stderr}",
        )

        samples_dir = (
            fake_home / ".local" / "lib" / "opsx-controller" / "samples"
        )
        self.assertTrue(
            (samples_dir / "sample-plan.md").is_file(),
            "installer must deploy sample-plan.md",
        )
        self.assertTrue(
            (samples_dir / "sample-plan.toml").is_file(),
            "installer must deploy sample-plan.toml",
        )

    def test_installer_refreshes_samples(self):
        """Verify re-running the installer replaces previously installed
        sample files (refresh, not append)."""
        fake_home = self.repo / ".fake-home-refresh"
        fake_home.mkdir()

        installer = (
            Path(__file__).resolve().parents[2]
            / "scripts" / "install-orchestrator.sh"
        )
        checkout = Path(__file__).resolve().parents[2]
        env = {**os.environ, "HOME": str(fake_home)}

        # First install.
        result1 = subprocess.run(
            ["bash", str(installer), str(checkout)],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result1.returncode, 0)

        samples_dir = (
            fake_home / ".local" / "lib" / "opsx-controller" / "samples"
        )
        toml_path = samples_dir / "sample-plan.toml"
        original = toml_path.read_text(encoding="utf-8")

        # Corrupt the installed sample.
        toml_path.write_text("# corrupted\n", encoding="utf-8")

        # Re-install (refresh).
        result2 = subprocess.run(
            ["bash", str(installer), str(checkout)],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result2.returncode, 0)

        restored = toml_path.read_text(encoding="utf-8")
        self.assertEqual(
            restored, original,
            "re-running the installer must restore the original sample content",
        )


