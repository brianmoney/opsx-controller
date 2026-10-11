"""Contract test for the installed OpenCode ``/opsx-ship`` conductor.

Installs the OpenCode adapter into a temporary HOME (never the real
environment) and asserts the installed conductor agent and command carry the
delegation-enforcing capability contract: no file editing, no mutating shell,
and the task facility allowed. Mirrors the temporary-HOME pattern in
``tests/supervisor/test_agent_contracts.py``.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_OPENCODE_INSTALLER = _REPO / "adapters" / "opencode" / "install.sh"

_MUTATING_COMMANDS = (
    "git commit*",
    "git push*",
    "git checkout*",
    "git branch*",
    "rm *",
    "openspec archive*",
    "openspec new*",
    "openspec store setup*",
    "openspec store register*",
    "openspec store unregister*",
    "openspec store remove*",
)

# Commands that escape a broad prefix allowlist: a writable option, shell
# redirection, a pipeline, or chaining. The conductor holds no shell at all, so
# none of these may match any allow rule.
_SHELL_ESCAPES = (
    "git diff --output=/tmp/opsx-escape",
    "git diff --output /tmp/opsx-escape",
    "git log --output=/tmp/opsx-escape",
    "git diff > /tmp/opsx-escape",
    "git diff >> /tmp/opsx-escape",
    "git diff | tee /tmp/opsx-escape",
    "git diff; rm -rf /tmp/opsx-escape",
    "git diff && rm -rf /tmp/opsx-escape",
    "git status; touch /tmp/opsx-escape",
    "openspec validate > /tmp/opsx-escape",
    "openspec store list; rm -rf /tmp/opsx-escape",
)


def _bash_allow_patterns(agent_text: str) -> list[str]:
    """Return bash allow patterns from an OpenCode agent permission block.

    Handles both the scalar form (``bash: deny``) and the mapping form
    (``bash:`` with indented ``"pattern": allow`` entries). The conductor must
    expose no allow patterns, so the expected result is ``[]``.
    """
    bash_indent: int | None = None
    patterns: list[str] = []
    for raw in agent_text.splitlines():
        stripped = raw.strip()
        if bash_indent is None:
            if stripped.startswith("bash:"):
                rest = stripped.split("bash:", 1)[1].strip()
                indent = len(raw) - len(raw.lstrip(" "))
                if rest:
                    return [rest] if rest == "allow" else []
                bash_indent = indent
            continue
        if not stripped:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent <= bash_indent:
            break
        if ":" in stripped:
            resource, _, effect = stripped.rpartition(":")
            if effect.strip() == "allow":
                patterns.append(resource.strip().strip('"').strip("'"))
    return patterns


def _glob_matches(pattern: str, value: str) -> bool:
    """OpenCode whole-value shell glob: ``*`` any run, ``?`` one character."""
    if pattern.endswith(" *") and value == pattern[:-2]:
        return True
    regex = "^" + re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".") + "$"
    return re.match(regex, value) is not None


def _model_env() -> dict[str, str]:
    return {
        "OPSX_CONTROLLER_MODEL": "test-provider/test-controller",
        "OPSX_IMPLEMENTER_MODEL": "test-provider/test-implementer",
        "OPSX_REVIEWER_MODEL": "test-provider/test-reviewer",
        "OPSX_ARCHIVER_MODEL": "test-provider/test-archiver",
    }


class ShipConductorContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.env = {**os.environ, **_model_env(), "HOME": self.home.name}
        subprocess.run(
            ["bash", str(_OPENCODE_INSTALLER), "--global"],
            cwd=_REPO,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        self.agents = Path(self.home.name) / ".config" / "opencode" / "agents"
        self.commands = Path(self.home.name) / ".config" / "opencode" / "commands"

    def test_conductor_agent_is_installed_as_a_primary(self) -> None:
        path = self.agents / "opsx-ship.md"
        self.assertTrue(path.is_file(), f"conductor agent missing at {path}")
        text = path.read_text(encoding="utf-8")
        self.assertIn("mode: primary", text)
        self.assertNotIn("{env:", text, "unsubstituted placeholder in conductor")

    def test_conductor_cannot_edit_files(self) -> None:
        text = (self.agents / "opsx-ship.md").read_text(encoding="utf-8")
        self.assertIn("edit: deny", text)

    def test_conductor_allows_the_task_facility_and_questions(self) -> None:
        text = (self.agents / "opsx-ship.md").read_text(encoding="utf-8")
        self.assertIn("task: allow", text)
        self.assertIn("question: allow", text)

    def test_conductor_denies_all_shell(self) -> None:
        text = (self.agents / "opsx-ship.md").read_text(encoding="utf-8")
        self.assertIn("bash: deny", text)
        self.assertNotIn('"*": allow', text)
        # No allow patterns may remain: OpenCode matches raw command text with
        # wildcards, so any prefix allow is a mutation escape.
        self.assertEqual(_bash_allow_patterns(text), [])

    def test_conductor_denies_mutating_and_read_only_inspection_shell(self) -> None:
        text = (self.agents / "opsx-ship.md").read_text(encoding="utf-8")
        # Mutating commands must never be allowlisted.
        for command in _MUTATING_COMMANDS:
            with self.subTest(command=command):
                self.assertNotIn(f'"{command}": allow', text)
        self.assertNotIn('"openspec store*": allow', text)
        # Previously allowlisted read-only prefixes are gone too: they could
        # not exclude --output, redirection, pipelines, or chaining.
        for prefix in (
            '"openspec status*": allow',
            '"openspec validate*": allow',
            '"openspec store list*": allow',
            '"git status*": allow',
            '"git diff*": allow',
            '"git log*": allow',
            '"git rev-parse*": allow',
        ):
            with self.subTest(prefix=prefix):
                self.assertNotIn(prefix, text)

    def test_shell_escapes_match_no_allow_rule(self) -> None:
        text = (self.agents / "opsx-ship.md").read_text(encoding="utf-8")
        patterns = _bash_allow_patterns(text)
        for command in _SHELL_ESCAPES:
            with self.subTest(command=command):
                matched = [p for p in patterns if _glob_matches(p, command)]
                self.assertEqual(
                    matched,
                    [],
                    f"{command!r} matched bash allow rules {matched}; the "
                    "conductor must hold no shell",
                )

    def test_matcher_would_flag_a_reintroduced_broad_allow(self) -> None:
        # Guard the escape regression above: if a broad prefix allow were
        # reintroduced, the same matcher flags these escapes, so an empty match
        # set in test_shell_escapes_match_no_allow_rule is meaningful.
        synthetic = (
            "permission:\n"
            "  bash:\n"
            '    "*": deny\n'
            '    "git diff*": allow\n'
            '    "git status*": allow\n'
        )
        patterns = _bash_allow_patterns(synthetic)
        self.assertEqual(patterns, ["git diff*", "git status*"])
        for command in _SHELL_ESCAPES:
            if command.startswith(("git diff", "git status")):
                with self.subTest(command=command):
                    self.assertTrue(
                        any(_glob_matches(p, command) for p in patterns),
                        f"matcher failed to flag {command!r}",
                    )

    def test_command_is_installed_and_routes_to_the_conductor(self) -> None:
        path = self.commands / "opsx-ship.md"
        self.assertTrue(path.is_file(), f"ship command missing at {path}")
        text = path.read_text(encoding="utf-8")
        self.assertIn("agent: opsx-ship", text)


if __name__ == "__main__":
    unittest.main()
