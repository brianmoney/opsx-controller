"""Contract tests for the dsh adapter role instruction files.

These read the shipped ``adapters/dsh/agents/opsx-*.md`` files and assert the
required workflow directives from the ``dsh-adapter`` spec, so a role file
edit that drops a machine-relevant directive fails here rather than drifting
at runtime. Assertions normalize whitespace so mid-sentence line wrapping in
the prose does not break them.
"""

from __future__ import annotations

import unittest
from pathlib import Path

_AGENT_DIR = (
    Path(__file__).resolve().parents[2]
    / "adapters" / "dsh" / "agents"
)

_ROLES = ("implementer", "reviewer", "archiver")


def _read(role: str) -> str:
    return (_AGENT_DIR / f"opsx-{role}.md").read_text(encoding="utf-8")


def _flat(role: str) -> str:
    text = _read(role).replace("`", "")
    return " ".join(text.split())


class RoleInstructionFileShapeTests(unittest.TestCase):
    def test_all_three_role_files_present(self) -> None:
        for role in _ROLES:
            path = _AGENT_DIR / f"opsx-{role}.md"
            self.assertTrue(path.is_file(), f"missing role file {path}")
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.strip(), f"{path} is empty")


class ImplementerRoleInstructionTests(unittest.TestCase):
    """Assert the implementer directives from the dsh-adapter spec."""

    def setUp(self) -> None:
        self.text = _flat("implementer")

    def test_live_openspec_status_and_instructions(self) -> None:
        self.assertIn('openspec status --change "<change>" --json', self.text)
        self.assertIn('openspec instructions apply --change "<change>" --json', self.text)

    def test_reads_state_file_and_trusts_valid_cache(self) -> None:
        self.assertIn("Read STATE_FILE when it exists", self.text)
        self.assertIn(
            "If CONTEXT_CACHE_VALID=true and CONTEXT_CACHE_STATUS=ready",
            self.text,
        )

    def test_task_handling_requires_reread_and_immediate_marking(self) -> None:
        self.assertIn("Always reread the tasks file for the active change", self.text)
        self.assertIn(
            "Mark completed tasks in the change task file immediately after "
            "finishing them",
            self.text,
        )
        self.assertIn(
            "status=implemented requires every non-(manual) task in the "
            "change tasks file to be checked",
            self.text,
        )

    def test_fix_prompt_is_highest_priority_scope(self) -> None:
        self.assertIn(
            "If LATEST_FIX_PROMPT is non-empty, treat every finding, "
            "corrective guideline, and verification requirement in that "
            "handoff as the highest-priority retry scope for this round",
            self.text,
        )

    def test_no_git_mutations(self) -> None:
        self.assertIn(
            "Do not commit, push, archive, rebase, or create branches", self.text
        )

    def test_exactly_one_json_object_output(self) -> None:
        self.assertIn(
            "Your final assistant message MUST be exactly one physical line "
            "containing exactly one valid JSON object",
            self.text,
        )
        self.assertIn("Never include prose before or after the JSON.", self.text)
        self.assertIn("Never include code fences.", self.text)
        self.assertIn("Never include headings.", self.text)
        self.assertIn(
            "The final assistant message must still be exactly one JSON "
            "object line — never a prose summary",
            self.text,
        )


class ReviewerRoleInstructionTests(unittest.TestCase):
    """Assert the reviewer directives from the dsh-adapter spec."""

    def setUp(self) -> None:
        self.text = _flat("reviewer")

    def test_live_openspec_validation(self) -> None:
        self.assertIn('openspec status --change "<change>" --json', self.text)
        self.assertIn('openspec instructions apply --change "<change>" --json', self.text)
        self.assertIn("openspec validate <change> --strict", self.text)

    def test_finding_classification_rules(self) -> None:
        self.assertIn("Count missing or materially incorrect work as critical.", self.text)
        self.assertIn(
            "Count partial coverage, missing validation, missing tests, or "
            "notable design drift as warning.",
            self.text,
        )
        self.assertIn("Count minor notes and suggestions together as note.", self.text)

    def test_strict_review_gate_pass_only_when_all_counts_zero(self) -> None:
        self.assertIn(
            "This review gate is strict: any non-zero critical, warning, or "
            "note count is a failure.",
            self.text,
        )
        self.assertIn(
            "Return verdict=pass only when all three counts are zero.", self.text
        )

    def test_task_completeness_rule_blocks_unchecked_tasks(self) -> None:
        self.assertIn(
            "When TASK_COUNTS.complete < total, read the change tasks file "
            "and return verdict=fail with a blocking finding per unchecked "
            "non-(manual) task",
            self.text,
        )

    def test_fix_prompt_required_on_fail(self) -> None:
        self.assertIn(
            "When the verdict is fail, the fix_prompt must be a "
            "self-contained corrective handoff with labeled CHANGE, FINDINGS, "
            "CORRECTIVE GUIDANCE, and VERIFY sections.",
            self.text,
        )

    def test_exactly_one_json_object_output(self) -> None:
        self.assertIn("Respond with exactly one line of JSON.", self.text)
        self.assertIn("No markdown, headings, bullets, code fences, or extra commentary.", self.text)
        self.assertIn(
            "the JSON object line IS the review. A review that ends in prose "
            "is discarded in full by the controller",
            self.text,
        )


class ArchiverRoleInstructionTests(unittest.TestCase):
    """Assert the archiver directives from the dsh-adapter spec."""

    def setUp(self) -> None:
        self.text = _flat("archiver")

    def test_state_file_tracked_change_files_scope(self) -> None:
        self.assertIn(
            "Read STATE_FILE when it exists. Use the controller-owned "
            "tracked_change_files list as the default implementation file "
            "set for explicit archive staging",
            self.text,
        )

    def test_archive_readiness_validation(self) -> None:
        self.assertIn("openspec status --change", self.text)
        self.assertIn("openspec validate <change> --strict", self.text)
        self.assertIn(
            "fail closed if any unchecked - [ ] task remains whose line does "
            "not end in (manual)",
            self.text,
        )

    def test_explicit_archive_scope_before_mutation(self) -> None:
        self.assertIn(
            "Determine the narrow explicit archive commit scope before "
            "mutating files.",
            self.text,
        )
        self.assertIn("openspec/changes/<change>/ (the deletion left by the move)", self.text)

    def test_fails_closed_on_ambiguous_scope(self) -> None:
        self.assertIn(
            "If you cannot name that narrow staged set up front, return "
            "blocked JSON with reason ambiguous archive commit scope before "
            "syncing or moving anything",
            self.text,
        )

    def test_delta_spec_sync(self) -> None:
        self.assertIn(
            "If delta specs exist, sync them into openspec/specs/ when the "
            "change is unambiguous. If sync is ambiguous, fail closed.",
            self.text,
        )

    def test_change_directory_tracking_and_deletion_staging(self) -> None:
        self.assertIn("git ls-files -- openspec/changes/<change>", self.text)
        self.assertIn(
            "If it lists any files, run git add -A -- "
            "openspec/changes/<change> so the move commits as one rename.",
            self.text,
        )
        self.assertIn(
            "If it lists no files, the change directory was never committed: "
            "there is no deletion to stage",
            self.text,
        )

    def test_staged_file_inspection_before_commit(self) -> None:
        self.assertIn(
            "Inspect git diff --cached --name-status before committing.", self.text
        )
        self.assertIn(
            "Fail closed if any staged file falls outside the explicit "
            "archive set.",
            self.text,
        )

    def test_exact_commit_message(self) -> None:
        self.assertIn(
            "archive(<change>): archive completed OpenSpec change", self.text
        )

    def test_restore_on_commit_failure(self) -> None:
        self.assertIn(
            "If a failure happens after step 11 but before the archive "
            "commit succeeds, move openspec/changes/archive/YYYY-MM-DD-<change> "
            "back to openspec/changes/<change> before returning blocked JSON.",
            self.text,
        )

    def test_never_asks_and_never_reports_success_on_failure(self) -> None:
        self.assertIn("Never ask a question.", self.text)
        self.assertIn(
            "Never report success if validation, sync, move, or commit work "
            "fails.",
            self.text,
        )

    def test_exactly_one_json_object_output(self) -> None:
        self.assertIn("Respond with exactly one line of JSON.", self.text)
        self.assertIn("No markdown, headings, bullets, code fences, or extra commentary.", self.text)
        self.assertIn(
            "the JSON object line IS the result. Output that ends in prose is "
            "discarded in full by the controller",
            self.text,
        )


if __name__ == "__main__":
    unittest.main()
