"""Drift and capability-contract tests for the ``/opsx-ship`` command surfaces.

The ``/opsx-ship`` conductor body is authored once at
``core/commands/opsx-ship.md`` and rendered into four committed surfaces (the
OpenCode command, the Claude Code skill, the Codex CLI skill, and the Claude
Code plugin skill). The Codex plugin bundle copy is regenerated from the Codex
skill by ``adapters/codex-cli/install.sh --plugin`` and must stay
byte-identical.

The tests assert the rendered surfaces embed the canonical body verbatim, that
the committed output matches what the generator renders, that each surface
carries its client-specific capability restriction, and that neither the
canonical body nor the client-neutral surfaces leak client-specific tokens.
"""

from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL = _ROOT / "core" / "commands" / "opsx-ship.md"

# (label, repo-relative path) for every surface generated from the canonical
# body. The Codex plugin bundle copy is checked separately for byte parity.
_SURFACES = (
    ("opencode-command", "adapters/opencode/commands/opsx-ship.md"),
    ("claude-skill", "adapters/claude-code/skills/opsx-ship/SKILL.md"),
    ("codex-skill", "adapters/codex-cli/skills/opsx-ship/SKILL.md"),
    ("plugin-skill", "plugins/opsx-controller/skills/opsx-ship/SKILL.md"),
)

_COMMAND_TEMPLATES = (
    "adapters/opencode/command-templates/opsx-ship.md.tmpl",
    "adapters/claude-code/command-templates/opsx-ship.md.tmpl",
    "adapters/codex-cli/command-templates/opsx-ship.md.tmpl",
    "plugins/opsx-controller/command-templates/opsx-ship.md.tmpl",
)

_FORBIDDEN_TOKENS = (
    "CLAUDE.md",
    "DeepSeek Harness",
    "dsh-worker",
    "opencode",
    "OpenCode",
    "Claude",
    "Codex",
)

_EDITING_TOOLS = ("Edit", "Write", "MultiEdit")


def _read(relative: str) -> str:
    return (_ROOT / relative).read_text(encoding="utf-8")


class CanonicalBodyTests(unittest.TestCase):
    def test_canonical_body_is_present_and_client_neutral(self) -> None:
        body = _CANONICAL.read_text(encoding="utf-8")
        self.assertTrue(body.strip(), "canonical ship body must not be empty")
        for token in _FORBIDDEN_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(
                    token, body, f"client-specific {token!r} leaked into the body"
                )

    def test_canonical_body_states_the_boundary_and_ledger(self) -> None:
        body = " ".join(_CANONICAL.read_text(encoding="utf-8").split())
        for phrase in (
            "no durable state",
            "no plan manifest",
            "does not change `opsx-plan`, `opsx-run`",
            "delegation ledger",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, body)

    def test_canonical_body_discovers_and_threads_one_store_id(self) -> None:
        body = " ".join(_CANONICAL.read_text(encoding="utf-8").split())
        for phrase in (
            "openspec store list --json",
            "--store <id>",
            "root-aware",
            "never re-discover or switch",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, body)

    def test_canonical_body_defines_a_finite_retry_budget(self) -> None:
        body = " ".join(_CANONICAL.read_text(encoding="utf-8").split())
        for phrase in (
            "**Default**: `3`",
            "**Maximum**: `10`",
            "RETRIES: <n>",
            "retry_count = 0",
            "retry_limit",
            "exhaustion report",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, body)

    def test_canonical_body_orders_the_loop_implement_verify_review(self) -> None:
        body = " ".join(_CANONICAL.read_text(encoding="utf-8").split())
        self.assertIn("implement → verify → review", body)
        self.assertIn("**fresh** review", body)
        self.assertNotIn("Loop review → verify → implement", body)

    def test_canonical_body_gates_archive_on_critical_only(self) -> None:
        body = " ".join(_CANONICAL.read_text(encoding="utf-8").split())
        for phrase in (
            "A `critical` finding is blocking",
            "`warning` and `note` findings are advisory",
            "never block archive on their own",
            "as advisory when its only findings are",
            "run's notes",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, body)
        # The old strict gate forced a corrective round for every severity.
        self.assertNotIn("Any critical, warning, or note", body)
        self.assertNotIn("Require a fresh, clean review", body)



class SurfaceParityTests(unittest.TestCase):
    def test_every_surface_embeds_the_canonical_body(self) -> None:
        body = _CANONICAL.read_text(encoding="utf-8").strip("\n")
        for label, relative in _SURFACES:
            with self.subTest(surface=label):
                rendered = _read(relative)
                self.assertIn(
                    body,
                    rendered,
                    f"{label} does not embed the canonical body verbatim",
                )

    def test_every_surface_carries_the_generated_marker(self) -> None:
        for label, relative in _SURFACES:
            with self.subTest(surface=label):
                head = _read(relative).splitlines()[:3]
                joined = "\n".join(head)
                self.assertIn("generate-phase-agents.py", joined)
                self.assertIn("core/commands/opsx-ship.md", joined)

    def test_codex_plugin_bundle_copy_matches_adapter_skill(self) -> None:
        adapter = (_ROOT / "adapters/codex-cli/skills/opsx-ship/SKILL.md").read_bytes()
        bundle = (
            _ROOT / "adapters/codex-cli/plugin/skills/opsx-ship/SKILL.md"
        ).read_bytes()
        self.assertEqual(
            bundle,
            adapter,
            "regenerate the Codex plugin bundle with "
            "adapters/codex-cli/install.sh --plugin",
        )

    def test_opencode_command_is_client_neutral_and_routes_to_the_conductor(
        self,
    ) -> None:
        text = _read("adapters/opencode/commands/opsx-ship.md")
        self.assertIn("agent: opsx-ship", text)
        for token in _FORBIDDEN_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, text)

    def test_opencode_conductor_agent_embeds_the_canonical_body(self) -> None:
        # The OpenCode conductor agent is hand-authored (not a generator
        # output) but must carry the canonical body verbatim so the surfaces
        # cannot drift apart.
        body = _CANONICAL.read_text(encoding="utf-8").strip("\n")
        text = _read("adapters/opencode/agents/opsx-ship.md")
        marker = "# Drive one OpenSpec change to conclusion"
        self.assertIn(marker, text, "opencode conductor agent is missing its body")
        self.assertEqual(text[text.index(marker):].strip("\n"), body)

    def test_command_templates_expose_exactly_one_placeholder(self) -> None:
        for relative in _COMMAND_TEMPLATES:
            with self.subTest(template=relative):
                text = _read(relative)
                self.assertEqual(
                    text.count("{{phase_body}}"),
                    1,
                    "command template must expose exactly one "
                    "{{phase_body}} placeholder",
                )


class CapabilityRestrictionTests(unittest.TestCase):
    def _allowed_tools(self, relative: str) -> set[str]:
        text = _read(relative)
        self.assertIn("allowed-tools:", text)
        allowed_line = next(
            line for line in text.splitlines() if "allowed-tools:" in line
        )
        return {
            entry.strip()
            for entry in allowed_line.split("allowed-tools:", 1)[1].split(",")
        }

    def test_claude_skill_allowed_tools_exclude_file_editing(self) -> None:
        for label, relative in (
            ("claude-skill", "adapters/claude-code/skills/opsx-ship/SKILL.md"),
            ("plugin-skill", "plugins/opsx-controller/skills/opsx-ship/SKILL.md"),
        ):
            with self.subTest(surface=label):
                allowed = self._allowed_tools(relative)
                self.assertIn("Task", allowed)
                for tool in _EDITING_TOOLS:
                    self.assertNotIn(
                        tool,
                        allowed,
                        f"{label} must not allow the file-editing tool {tool}",
                    )

    def test_claude_skill_grants_no_bash_at_all(self) -> None:
        # Claude Code prefix Bash(...) rules cannot exclude --output,
        # redirection, pipelines, or chaining, so the conductor must hold no
        # Bash grant at all. Absence of every Bash entry is the enforced
        # boundary; there is no allowlist left to escape.
        for label, relative in (
            ("claude-skill", "adapters/claude-code/skills/opsx-ship/SKILL.md"),
            ("plugin-skill", "plugins/opsx-controller/skills/opsx-ship/SKILL.md"),
        ):
            with self.subTest(surface=label):
                allowed = self._allowed_tools(relative)
                self.assertNotIn("Bash", allowed)
                bash_entries = [entry for entry in allowed if entry.startswith("Bash")]
                self.assertEqual(
                    bash_entries,
                    [],
                    f"{label} must grant no Bash entry at all, found {bash_entries}",
                )
                self.assertNotIn("Bash", _read(relative))

    def test_codex_skill_fails_closed_as_unsupported(self) -> None:
        text = " ".join(_read("adapters/codex-cli/skills/opsx-ship/SKILL.md").split())
        self.assertIn("unsupported on Codex CLI", text)
        # The honest capability refusal names the missing capability precisely.
        self.assertIn(
            "cannot dispatch a separately writable sub-agent beneath a read-only conductor",
            text,
        )
        self.assertIn("parent session's permission profile", text)
        self.assertIn("cannot override", text)
        self.assertIn("top-level", text)
        # It must not claim a working escalation or a shipped profile.
        self.assertNotIn("opsx-ship-profile", text)
        self.assertNotIn("sandbox_mode = \"read-only\"", text)

    def test_codex_nonoperational_profile_is_not_shipped(self) -> None:
        profile = _ROOT / "adapters/codex-cli/templates/project/opsx-ship-profile.toml"
        self.assertFalse(
            profile.exists(),
            "Codex CLI /opsx-ship is unsupported; do not ship a nonoperational "
            "profile prerequisite",
        )


if __name__ == "__main__":
    unittest.main()
