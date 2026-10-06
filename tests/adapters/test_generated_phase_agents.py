"""Deterministic-generation tests for the distributed phase agents.

These exercise ``scripts/generate-phase-agents.py`` directly: they render every
output, byte-compare it against the committed copy, render into a temporary
directory, assert the templates carry exactly one ``{{phase_body}}``
placeholder, and assert the per-adapter metadata survives generation.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_GENERATOR = _ROOT / "scripts" / "generate-phase-agents.py"

_PHASES = ("implementer", "reviewer", "archiver")

_TEMPLATE_DIRS = (
    "adapters/opencode/agent-templates",
    "adapters/claude-code/agent-templates",
    "adapters/codex-cli/agent-templates",
    "adapters/dsh/agent-templates",
    "plugins/opsx-controller/agent-templates",
)

_MODEL_TOKENS = {
    "implementer": ("{env:OPSX_IMPLEMENTER_MODEL}", "{env:OPSX_IMPLEMENTER_VARIANT}"),
    "reviewer": ("{env:OPSX_REVIEWER_MODEL}", "{env:OPSX_REVIEWER_VARIANT}"),
    "archiver": ("{env:OPSX_ARCHIVER_MODEL}", "{env:OPSX_ARCHIVER_VARIANT}"),
}


def _load_generator():
    loader = SourceFileLoader("generate_phase_agents", str(_GENERATOR))
    spec = importlib.util.spec_from_loader("generate_phase_agents", loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RenderParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.generator = _load_generator()

    def test_every_committed_output_matches_rendered_bytes(self) -> None:
        rendered = self.generator.render_outputs(_ROOT)
        self.assertEqual(
            len(rendered),
            18,
            "expected 18 generated outputs (six distributions x three phases)",
        )
        for relative, expected in rendered.items():
            with self.subTest(output=relative):
                committed = (_ROOT / relative).read_bytes()
                self.assertEqual(
                    committed,
                    expected.encode("utf-8"),
                    f"{relative} is stale; run scripts/generate-phase-agents.py",
                )

    def test_render_into_temporary_directory_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            written = self.generator.write_outputs(_ROOT, Path(tmp))
            self.assertEqual(len(written), 18)
            for destination in written:
                relative = destination.relative_to(tmp)
                self.assertEqual(
                    destination.read_bytes(),
                    (_ROOT / relative).read_bytes(),
                    f"temporary render differs for {relative}",
                )

    def test_generator_check_mode_passes_on_clean_tree(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(_GENERATOR), "--check"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_check_reports_a_hand_edited_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Build a minimal tree the checker can re-render from.
            shutil.copytree(_ROOT / "core" / "phase-agents", root / "core" / "phase-agents")
            for template_dir in _TEMPLATE_DIRS:
                shutil.copytree(_ROOT / template_dir, root / template_dir)
            rendered = self.generator.render_outputs(_ROOT)
            for relative in rendered:
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((_ROOT / relative).read_bytes())

            self.assertEqual(self.generator.check_outputs(root), [])

            edited = root / "adapters/opencode/agents/opsx-implementer.md"
            edited.write_text(
                edited.read_text(encoding="utf-8") + "\nhand edit\n",
                encoding="utf-8",
            )
            self.assertEqual(
                self.generator.check_outputs(root),
                ["adapters/opencode/agents/opsx-implementer.md"],
            )


class TemplateShapeTests(unittest.TestCase):
    def test_every_template_has_exactly_one_placeholder(self) -> None:
        templates = []
        for template_dir in _TEMPLATE_DIRS:
            templates.extend(sorted((_ROOT / template_dir).glob("*.tmpl")))
        self.assertEqual(
            len(templates),
            15,
            "expected 15 templates (five distributions x three phases); the "
            "Codex plugin copy reuses the codex-cli template",
        )
        for template in templates:
            with self.subTest(template=str(template.relative_to(_ROOT))):
                text = template.read_text(encoding="utf-8")
                self.assertEqual(
                    text.count("{{phase_body}}"),
                    1,
                    "template must expose exactly one {{phase_body}} placeholder",
                )

    def test_canonical_bodies_avoid_client_specific_steps(self) -> None:
        forbidden = (
            "CLAUDE.md",
            "DeepSeek Harness",
            "dsh-worker",
            "opencode",
            "OpenCode",
            "Codex",
        )
        for phase in _PHASES:
            text = (_ROOT / "core" / "phase-agents" / f"{phase}.md").read_text(
                encoding="utf-8"
            )
            for token in forbidden:
                with self.subTest(phase=phase, token=token):
                    self.assertNotIn(
                        token,
                        text,
                        f"client-specific {token!r} leaked into {phase} canonical body",
                    )


class MetadataPreservationTests(unittest.TestCase):
    def test_generated_header_present_in_first_lines(self) -> None:
        for phase in _PHASES:
            for relative in (
                f"adapters/opencode/agents/opsx-{phase}.md",
                f"adapters/claude-code/agents/opsx-{phase}.md",
                f"adapters/codex-cli/agents/opsx-{phase}.toml",
                f"adapters/dsh/agents/opsx-{phase}.md",
                f"adapters/codex-cli/plugin/agents/opsx-{phase}.toml",
                f"plugins/opsx-controller/agents/opsx-{phase}.md",
            ):
                with self.subTest(output=relative):
                    head = (_ROOT / relative).read_text(encoding="utf-8").splitlines()[:3]
                    joined = "\n".join(head)
                    self.assertIn("generate-phase-agents.py", joined)
                    self.assertIn(f"core/phase-agents/{phase}.md", joined)

    def test_opencode_env_tokens_survive(self) -> None:
        for phase, tokens in _MODEL_TOKENS.items():
            text = (
                _ROOT / "adapters/opencode/agents" / f"opsx-{phase}.md"
            ).read_text(encoding="utf-8")
            for token in tokens:
                with self.subTest(phase=phase, token=token):
                    self.assertIn(token, text)

    def test_codex_metadata_survives(self) -> None:
        sandbox_modes = {
            "implementer": "workspace-write",
            "reviewer": "read-only",
            "archiver": "danger-full-access",
        }
        for phase, sandbox_mode in sandbox_modes.items():
            for relative in (
                f"adapters/codex-cli/agents/opsx-{phase}.toml",
                f"adapters/codex-cli/plugin/agents/opsx-{phase}.toml",
            ):
                with self.subTest(output=relative):
                    data = tomllib.loads(
                        (_ROOT / relative).read_text(encoding="utf-8")
                    )
                    self.assertEqual(data["sandbox_mode"], sandbox_mode)
                    self.assertEqual(
                        data["model"], _MODEL_TOKENS[phase][0]
                    )
                    self.assertEqual(data["model_reasoning_effort"], "high")
                    self.assertTrue(data["developer_instructions"].strip())

    def test_claude_metadata_survives(self) -> None:
        expectations = {
            "implementer": ("Read, Edit, MultiEdit, Write, Glob, Grep, Bash", "high"),
            "reviewer": ("Read, Glob, Grep, Bash", "xhigh"),
            "archiver": ("Read, Edit, MultiEdit, Write, Glob, Grep, Bash", "high"),
        }
        for phase, (tools, effort) in expectations.items():
            for prefix in ("adapters/claude-code/agents", "plugins/opsx-controller/agents"):
                relative = f"{prefix}/opsx-{phase}.md"
                with self.subTest(output=relative):
                    text = (_ROOT / relative).read_text(encoding="utf-8")
                    self.assertIn(f"tools: {tools}", text)
                    self.assertIn("model: inherit", text)
                    self.assertIn(f"effort: {effort}", text)

    def test_dsh_files_have_no_frontmatter(self) -> None:
        for phase in _PHASES:
            text = (
                _ROOT / "adapters/dsh/agents" / f"opsx-{phase}.md"
            ).read_text(encoding="utf-8")
            with self.subTest(phase=phase):
                self.assertFalse(text.startswith("---"))
                self.assertIn("DeepSeek Harness", text)


class ClaudePluginParityTests(unittest.TestCase):
    def test_plugin_body_matches_generated_claude_code_body(self) -> None:
        for phase in _PHASES:
            with self.subTest(phase=phase):
                claude = (
                    _ROOT / "adapters/claude-code/agents" / f"opsx-{phase}.md"
                ).read_text(encoding="utf-8")
                plugin = (
                    _ROOT / "plugins/opsx-controller/agents" / f"opsx-{phase}.md"
                ).read_text(encoding="utf-8")
                self.assertEqual(
                    claude.split("---", 2)[2],
                    plugin.split("---", 2)[2],
                    "Claude plugin body drifted from the generated Claude Code agent",
                )


if __name__ == "__main__":
    unittest.main()
