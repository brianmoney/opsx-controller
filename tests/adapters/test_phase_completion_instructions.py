"""Cross-distribution prompt-contract tests for evidence-based phase completion.

Reads the shipped implementer/reviewer role instructions for every authored
distribution (opencode, claude-code, codex-cli, dsh) plus the legacy opencode
plugin role files, normalizes prose/TOML whitespace, and asserts the
completion, impact, acceptance-reference, and deferral contract clauses. A
dropped clause fails here while cosmetic line wrapping does not.

Contract clauses are asserted on authored sources. Separate parity tests guard
deployed prompt drift: the shared policy section must be byte-identical across
its core and distributed-skill references, and generated Codex plugin TOML must
be byte-identical to its authored adapter source after bundle regeneration.
"""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

_MARKDOWN = "markdown"
_TOML = "toml"

# Each entry is (distribution label, repo-relative path, loader kind).
_IMPLEMENTERS = (
    ("opencode", "adapters/opencode/agents/opsx-implementer.md", _MARKDOWN),
    ("claude-code", "adapters/claude-code/agents/opsx-implementer.md", _MARKDOWN),
    ("codex-cli", "adapters/codex-cli/agents/opsx-implementer.toml", _TOML),
    ("dsh", "adapters/dsh/agents/opsx-implementer.md", _MARKDOWN),
    ("plugin", "plugins/opsx-controller/agents/opsx-implementer.md", _MARKDOWN),
)

_REVIEWERS = (
    ("opencode", "adapters/opencode/agents/opsx-reviewer.md", _MARKDOWN),
    ("claude-code", "adapters/claude-code/agents/opsx-reviewer.md", _MARKDOWN),
    ("codex-cli", "adapters/codex-cli/agents/opsx-reviewer.toml", _TOML),
    ("dsh", "adapters/dsh/agents/opsx-reviewer.md", _MARKDOWN),
    ("plugin", "plugins/opsx-controller/agents/opsx-reviewer.md", _MARKDOWN),
)


def _normalize(text: str) -> str:
    """Strip inline code markers and collapse line wrapping."""
    return " ".join(text.replace("`", "").split())


def _load(entry: tuple[str, str, str]) -> str:
    _, relative, kind = entry
    path = _ROOT / relative
    if kind == _TOML:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return _normalize(data["developer_instructions"])
    return _normalize(path.read_text(encoding="utf-8"))


# Implementer clauses: evidence-based completion, scoped fixture/test-double
# evidence, risk ordering, recurring-critical handling, and the
# impact/acceptance-reference/deferral contract.
_IMPLEMENTER_CLAUSES = (
    "requirement -> execution path -> observation source -> applicable verification",
    "riskiest end-to-end slice first",
    "Mark a task complete only after its required behavior is implemented and "
    "supported by appropriate evidence that it actually runs",
    "Synthetic fixtures and test doubles are valid evidence only for the scope "
    "they actually exercise",
    "never promote caller assertions, scaffolding, or synthetic-only coverage "
    "to live, integration, or candidate proof when that is required",
    "Keep every claim scoped to what the evidence shows",
    "Genuine missing prerequisites may block execution",
    "they never imply the behavior is implemented",
    "root-cause correction plus a meaningful regression that would fail if the "
    "false-green returned",
    "When the same critical recurs, reassess why the previous fix failed",
    "A gap blocks acceptance when it is essential to the current agreed "
    "acceptance scope",
    "correct operation of implemented features",
    "required security or correctness guarantee",
    "accepted for deferral with a brief reason, its impact, and a follow-up",
    "A deferral is accepted only when a current agreement or scope artifact "
    "records it",
    "Never invent acceptance or silently narrow the scope",
    "The agreed deferral must appear in the current specs or tasks as an "
    "identified plain follow-up",
    "active unchecked tasks still gate",
    "Accepted deferrals do not trigger another implementation round and must be "
    "reported honestly",
    "replace the deferred task's checkbox line with a plain follow-up entry",
    "Never mark unimplemented work complete",
    "never relabel it (manual) to evade the task gate",
    "existing change artifacts and the existing summary",
    "do not add new protocol fields",
)

# Reviewer clauses: independent tracing, producer authenticity with scoped test
# doubles, blocking-finding evidence, impact-qualified classification,
# prior-critical closure, and deferral acceptance/disclosure.
_REVIEWER_CLAUSES = (
    "Independently trace each requirement to its execution path, observation "
    "source, and tests",
    "never accept the implementer's summary as proof",
    "Verify producer behavior and authenticity: confirm the real required "
    "execution path runs",
    "limit test doubles and fixtures to the evidence scope they actually "
    "exercise",
    "Do not demand live external services for unit or documentation tasks that "
    "do not require them",
    "Each blocking finding must cite the requirement it violates",
    "expected versus observed behavior",
    "a gap is blocking only when it is essential to the agreed acceptance scope",
    "correct operation of implemented features",
    "required security or correctness guarantee",
    "Qualify partial coverage, missing tests, and validation warnings to that "
    "accepted scope",
    "Independently verify that a prior critical finding is actually closed",
    "retain the prior locus",
    "explain why the earlier patch was insufficient",
    "An accepted deferral is valid only when all hold: a current agreement or "
    "scope artifact records it",
    "the implementer explicitly recorded a reason, impact, and follow-up",
    "Never infer acceptance from the implementer's summary",
    "never let a silent scope narrowing pass",
    "the agreed deferral appears in the current specs or tasks as an "
    "identified plain follow-up",
    "active unchecked tasks still gate",
    "Report accepted deferrals in the summary or existing change-artifact "
    "references",
    "never encode them in finding_counts, findings, or fix_prompt",
    "Do not downgrade an accepted deferral to a note",
    "Return verdict=pass only when agreed active automatable tasks are checked",
    "claimed execution, correctness, or security guarantee is supported by "
    "evidence",
    "Count blocking omissions or materially incorrect behavior in the accepted "
    "change scope as critical",
    "Count partial coverage, missing tests, validation warnings, or notable "
    "design drift that affect the accepted scope as warning",
)

# Wording that must not survive: the obsolete blanket classification would
# reclassify accepted deferrals as blocking findings and force extra rounds.
_OBSOLETE_REVIEWER_CLAUSES = (
    "Count missing or materially incorrect work as critical",
    "Count partial coverage, missing validation, missing tests, or notable "
    "design drift as warning",
)


class ImplementerInstructionContractTests(unittest.TestCase):
    def test_all_distributions_declare_the_contract(self) -> None:
        for label, relative, kind in _IMPLEMENTERS:
            with self.subTest(distribution=label):
                text = _load((label, relative, kind))
                for clause in _IMPLEMENTER_CLAUSES:
                    self.assertIn(clause, text, f"{label}: missing {clause!r}")

    def test_implementers_forbid_checked_or_manual_deferrals(self) -> None:
        for label, relative, kind in _IMPLEMENTERS:
            with self.subTest(distribution=label):
                text = _load((label, relative, kind))
                for clause in (
                    "Never mark unimplemented work complete",
                    "never leave deferred work as an unchecked checkbox",
                    "never relabel it (manual) to evade the task gate",
                ):
                    self.assertIn(clause, text, f"{label}: missing {clause!r}")


class ReviewerInstructionContractTests(unittest.TestCase):
    def test_all_distributions_declare_the_contract(self) -> None:
        for label, relative, kind in _REVIEWERS:
            with self.subTest(distribution=label):
                text = _load((label, relative, kind))
                for clause in _REVIEWER_CLAUSES:
                    self.assertIn(clause, text, f"{label}: missing {clause!r}")

    def test_reviewers_exclude_deferrals_from_findings(self) -> None:
        for label, relative, kind in _REVIEWERS:
            with self.subTest(distribution=label):
                text = _load((label, relative, kind))
                for clause in (
                    "never encode them in finding_counts, findings, or fix_prompt",
                    "Do not downgrade an accepted deferral to a note",
                ):
                    self.assertIn(clause, text, f"{label}: missing {clause!r}")

    def test_obsolete_blanket_classification_is_gone(self) -> None:
        for label, relative, kind in _REVIEWERS:
            with self.subTest(distribution=label):
                text = _load((label, relative, kind))
                for clause in _OBSOLETE_REVIEWER_CLAUSES:
                    self.assertNotIn(
                        clause, text, f"{label}: obsolete classification survived"
                    )

    def test_reviewers_keep_mechanical_task_gate(self) -> None:
        for label, relative, kind in _REVIEWERS:
            with self.subTest(distribution=label):
                text = _load((label, relative, kind))
                self.assertIn(
                    "verdict=fail with a blocking finding per unchecked "
                    "non-(manual) task",
                    text,
                )


class CodexTomlExtractionTests(unittest.TestCase):
    def test_codex_role_files_expose_developer_instructions(self) -> None:
        for label, relative, kind in _IMPLEMENTERS + _REVIEWERS:
            if kind != _TOML:
                continue
            with self.subTest(distribution=label):
                data = tomllib.loads(
                    (_ROOT / relative).read_text(encoding="utf-8")
                )
                self.assertTrue(data["developer_instructions"].strip())


_POLICY_HEADING = "## Evidence-based completion and accepted deferrals"
_CORE_CONTRACT = _ROOT / "core" / "controller-contract.md"
_SKILL_CONTRACT = (
    _ROOT / "skills" / "opsx-controller" / "references" / "controller-contract.md"
)
_CODEX_BUNDLE_FILES = (
    "opsx-implementer.toml",
    "opsx-reviewer.toml",
    "opsx-archiver.toml",
)

_CLAUDE_PLUGIN_PHASES = ("implementer", "reviewer", "archiver")


def _markdown_body(path: Path) -> str:
    """Return the content after a leading YAML frontmatter block."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return text
    return text.split("---", 2)[2]


def _policy_section_bytes(path: Path) -> bytes:
    """Return the shared policy section bytes, heading through next heading."""
    data = path.read_bytes()
    start = data.index(_POLICY_HEADING.encode("utf-8"))
    next_heading = data.find(b"\n## ", start)
    end = len(data) if next_heading == -1 else next_heading + 1
    return data[start:end]


class SharedPolicyParityTests(unittest.TestCase):
    def test_policy_section_is_byte_identical_across_references(self) -> None:
        core_section = _policy_section_bytes(_CORE_CONTRACT)
        skill_section = _policy_section_bytes(_SKILL_CONTRACT)
        self.assertTrue(core_section.strip())
        self.assertEqual(
            core_section,
            skill_section,
            "shared policy section drifted between core/controller-contract.md "
            "and skills/opsx-controller/references/controller-contract.md",
        )


class CodexBundleParityTests(unittest.TestCase):
    def test_generated_plugin_toml_matches_authored_sources(self) -> None:
        for name in _CODEX_BUNDLE_FILES:
            with self.subTest(file=name):
                authored = (
                    _ROOT / "adapters" / "codex-cli" / "agents" / name
                ).read_bytes()
                generated = (
                    _ROOT / "adapters" / "codex-cli" / "plugin" / "agents" / name
                ).read_bytes()
                self.assertEqual(
                    generated,
                    authored,
                    "generated Codex plugin copy differs from its authored "
                    "adapter source; awaiting bundle regeneration",
                )


class ClaudePluginParityTests(unittest.TestCase):
    """The plugin agents share the generated Claude Code agent content.

    Frontmatter differs (the plugin carries its own package description), so
    parity is asserted on the body after the frontmatter block: both are
    rendered from the same canonical body and client step.
    """

    def test_plugin_bodies_match_generated_claude_code_agents(self) -> None:
        for phase in _CLAUDE_PLUGIN_PHASES:
            with self.subTest(phase=phase):
                adapter = _markdown_body(
                    _ROOT / "adapters" / "claude-code" / "agents" / f"opsx-{phase}.md"
                )
                plugin = _markdown_body(
                    _ROOT / "plugins" / "opsx-controller" / "agents" / f"opsx-{phase}.md"
                )
                self.assertTrue(adapter.strip())
                self.assertEqual(
                    plugin,
                    adapter,
                    "Claude plugin agent body drifted from the generated "
                    "Claude Code agent; regenerate the phase agents",
                )


if __name__ == "__main__":
    unittest.main()
