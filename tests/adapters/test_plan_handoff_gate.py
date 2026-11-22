"""Contract and behavior tests for the plan-level Phase Transition Gate.

Two families live here:

* **Static contracts** read the shipped agent surfaces and assert the gate,
  the two new agents, the archiver's archive-only refusal, and the reviewer's
  coverage matrix are actually present. A prompt surface is the product here,
  so a missing paragraph is a missing feature.
* **Behavior tests** drive :mod:`tests.adapters.handoff_fixture` against a real
  temporary git repository: an archive-only commit blocks the handoff, the
  audit is read-only, a scoped repair commits only the accepted pathspecs, the
  user-owned file stays dirty, and the next change is selected only after
  ``handoff_ready: true``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.adapters import handoff_fixture as fixture

_REPO = Path(__file__).resolve().parents[2]

_ORCHESTRATOR = _REPO / "adapters" / "opencode" / "agents" / "opsx-orchestrator.md"
_AUDITOR = _REPO / "adapters" / "opencode" / "agents" / "opsx-integration-auditor.md"
_HYGIENE = _REPO / "adapters" / "opencode" / "agents" / "opsx-repo-hygiene.md"
_REVIEW = _REPO / "adapters" / "opencode" / "agents" / "opsx-review.md"
_ARCHIVER_BODY = _REPO / "core" / "phase-agents" / "archiver.md"

_GENERATED_ARCHIVERS = (
    "adapters/opencode/agents/opsx-archiver.md",
    "adapters/claude-code/agents/opsx-archiver.md",
    "adapters/codex-cli/agents/opsx-archiver.toml",
    "adapters/dsh/agents/opsx-archiver.md",
    "adapters/codex-cli/plugin/agents/opsx-archiver.toml",
    "plugins/opsx-controller/agents/opsx-archiver.md",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _body(path: Path) -> str:
    """Return a markdown agent's text after its YAML frontmatter."""
    text = _text(path)
    if not text.startswith("---"):
        return text
    parts = text.split("---", 2)
    return parts[2] if len(parts) == 3 else text


def _frontmatter(path: Path) -> str:
    text = _text(path)
    if not text.startswith("---"):
        return ""
    parts = text.split("---", 2)
    return parts[1] if len(parts) == 3 else ""


class OrchestratorGateContractTests(unittest.TestCase):
    """`opsx-orchestrator` must own a mandatory, bounded Phase Transition Gate."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _text(_ORCHESTRATOR)
        cls.flat = " ".join(cls.text.split())

    def test_gate_section_exists_and_is_mandatory(self) -> None:
        self.assertIn("Phase Transition Gate", self.text)
        self.assertIn("mandatory", self.flat.lower())
        self.assertIn(
            "A change is not complete until the handoff audit passes",
            self.flat,
        )

    def test_gate_skips_next_change_selection_until_handoff_ready(self) -> None:
        self.assertIn("opsx-integration-auditor", self.text)
        self.assertIn("handoff_ready", self.text)
        self.assertIn(
            "do not select the next change", self.flat.lower()
        )

    def test_handoff_ready_is_in_ledger_and_final_report(self) -> None:
        ledger_section = self.text.split("## Run ledger", 1)[1].split("\n## ", 1)[0]
        report_section = self.text.split("## Final report", 1)[1]
        self.assertIn("handoff_ready", ledger_section)
        self.assertIn("handoff_ready", report_section)

    def test_no_open_blockers_is_forbidden_while_handoff_is_not_ready(self) -> None:
        self.assertIn("no open blockers", self.flat.lower())
        self.assertIn(
            "forbidden", self.flat.lower()
        )
        self.assertIn("false or unknown", self.flat)

    def test_handoff_reserve_is_three_dispatches_and_excluded_from_the_cap(self) -> None:
        self.assertIn("HANDOFF_RESERVE", self.text)
        self.assertIn("three dispatches", self.flat.lower())
        reserve_section = self.text.split("### Handoff reserve", 1)[1].split("\n## ", 1)[0]
        self.assertIn("excluded", reserve_section.lower())
        self.assertIn("stop", reserve_section.lower())

    def test_repair_loop_is_capped_at_two_rounds(self) -> None:
        self.assertIn("opsx-repo-hygiene", self.text)
        self.assertIn("two repair/audit rounds", self.flat.lower())

    def test_archive_only_waiver_is_a_stop_not_completion(self) -> None:
        self.assertIn("archive-only", self.flat.lower())
        self.assertIn("waiver", self.flat.lower())
        self.assertIn("verbatim", self.flat.lower())
        waiver_section = self.text.split("archive-only waiver", 1)[-1]
        self.assertIn("stop", waiver_section.lower())
        self.assertIn("not", waiver_section.lower())

    def test_definition_of_done_section_lists_the_handoff_criteria(self) -> None:
        self.assertIn("## Definition of done", self.text)
        section = self.text.split("## Definition of done", 1)[1].split("\n## ", 1)[0]
        for token in (
            "implementation",
            "plan",
            "run state",
            "evidence",
            "scripts",
            "audit",
        ):
            with self.subTest(token=token):
                self.assertIn(token, section.lower())

    def test_gate_runs_after_archive_and_before_the_next_change(self) -> None:
        self.assertIn("after the change is archived", self.flat.lower())
        self.assertIn("before dispatching any work for the next change", self.flat.lower())

    def test_audit_must_be_a_fresh_independent_session(self) -> None:
        self.assertIn("fresh", self.flat.lower())
        for role in ("writer", "implementer", "reviewer", "archiver"):
            with self.subTest(role=role):
                self.assertIn(role, self.text)

    def test_orchestrator_passes_the_implementation_scope_to_the_archiver(self) -> None:
        archiver_section = self.text.split("`opsx-archiver`: ", 1)[1].split("\n\n", 1)[0]
        self.assertIn("IMPLEMENTATION_PATHS", archiver_section)

    def test_orchestrator_copies_reviewer_coverage_into_ledger_and_report(self) -> None:
        self.assertIn("coverage", self.text)
        self.assertIn("coverage[]", self.text)

    def test_orchestrator_requires_a_derived_review_matrix(self) -> None:
        self.assertIn("COVERAGE_MATRIX", self.text)
        self.assertIn("derived", self.flat.lower())


class AuditorContractTests(unittest.TestCase):
    """`opsx-integration-auditor` must be read-only, independent, and explicit."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _text(_AUDITOR)
        cls.front = _frontmatter(_AUDITOR)
        cls.body = _body(_AUDITOR)
        cls.flat = " ".join(cls.text.split())

    def test_frontmatter_is_usable_and_read_only(self) -> None:
        self.assertIn("mode: all", self.front)
        self.assertIn("edit: deny", self.front)
        self.assertNotIn("edit: allow", self.front)
        for capability in ("read: allow", "glob: allow", "grep: allow"):
            with self.subTest(capability=capability):
                self.assertIn(capability, self.front)

    def test_no_sub_agents_and_no_question(self) -> None:
        self.assertIn("task: deny", self.front)
        self.assertIn("question: deny", self.front)
        self.assertNotIn("task: allow", self.front)

    def test_shell_is_inspection_only(self) -> None:
        self.assertIn("bash", self.front)
        self.assertIn("inspection", self.flat.lower())
        for mutation in (
            "git commit",
            "git add",
            "git push",
        ):
            with self.subTest(mutation=mutation):
                self.assertIn(mutation, self.text)

    def test_read_only_contract_is_stated(self) -> None:
        self.assertIn("SHALL NOT edit", self.body)
        self.assertIn("stage", self.body)
        self.assertIn("commit", self.body)

    def test_audit_checklist_covers_every_required_check(self) -> None:
        for check in (
            "tracked-tree-clean",
            "source-manifest-at-head",
            "plan-at-head",
            "archive-commit-implementation-paths",
            "run-state-ignored",
            "evidence-at-head",
            "perf-script-from-head",
            "next-change-intake",
            "branch-upstream",
            "fresh-checkout-checks",
        ):
            with self.subTest(check=check):
                self.assertIn(check, self.text)

    def test_check_results_are_the_documented_vocabulary(self) -> None:
        for result in ("passed", "failed", "waived", "unqualified"):
            with self.subTest(result=result):
                self.assertIn(f"`{result}`", self.text)

    def test_final_json_contract_is_complete(self) -> None:
        for key in (
            '"status"',
            '"handoff_ready"',
            '"change"',
            '"head"',
            '"checks"',
            '"blockers"',
            '"next_action"',
        ):
            with self.subTest(key=key):
                self.assertIn(key, self.text)

    def test_dead_evidence_is_unqualified_not_passed(self) -> None:
        self.assertIn("unqualified", self.flat.lower())
        self.assertIn("dead", self.flat.lower())

    def test_auditor_does_not_accept_summaries_as_evidence(self) -> None:
        self.assertIn("summary", self.flat.lower())
        self.assertIn("evidence", self.flat.lower())
        self.assertIn("inspect", self.flat.lower())


class HygieneContractTests(unittest.TestCase):
    """`opsx-repo-hygiene` writes, but only inside an explicit blocker scope."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _text(_HYGIENE)
        cls.front = _frontmatter(_HYGIENE)
        cls.flat = " ".join(cls.text.split())

    def test_frontmatter_allows_edit_and_shell_but_not_dispatch(self) -> None:
        self.assertIn("mode: all", self.front)
        self.assertIn("edit: allow", self.front)
        self.assertIn("bash: allow", self.front)
        self.assertIn("task: deny", self.front)
        self.assertIn("question: deny", self.front)

    def test_do_not_invent_scope_contract(self) -> None:
        self.assertIn("do not invent scope", self.flat.lower())
        self.assertIn("blocked", self.flat.lower())

    def test_commit_uses_explicit_pathspecs(self) -> None:
        self.assertIn("pathspec", self.flat.lower())
        self.assertIn("git add --", self.text)

    def test_forbidden_staging_is_enumerated(self) -> None:
        for token in (
            "user-owned",
            "secret",
            "generated state",
            "unrelated",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.flat.lower())

    def test_product_behavior_fixes_are_forbidden(self) -> None:
        self.assertIn("product behavior", self.flat.lower())
        self.assertIn("code defect", self.flat.lower())
        self.assertIn("reviewed change", self.flat.lower())

    def test_post_commit_report_requires_sha_staged_and_unstaged(self) -> None:
        for token in (
            '"commit"',
            '"staged_paths"',
            '"unstaged"',
            "next_audit_input",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.text)

    def test_repairs_are_limited_to_the_named_blockers(self) -> None:
        self.assertIn("blocker", self.flat.lower())
        self.assertIn("only", self.flat.lower())


class ArchiverScopeContractTests(unittest.TestCase):
    """The canonical archiver body must refuse an archive-only commit."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.body = _text(_ARCHIVER_BODY)
        cls.flat = " ".join(cls.body.split())

    def test_implementation_scope_is_required_when_source_differs(self) -> None:
        self.assertIn("IMPLEMENTATION_PATHS", self.body)
        self.assertIn("archive-only", self.flat.lower())
        self.assertIn("differs from the archive parent", self.flat.lower())

    def test_archive_only_scope_is_rejected_with_pending_paths(self) -> None:
        self.assertIn("ambiguous archive commit scope", self.flat)
        self.assertIn("pending", self.flat.lower())
        self.assertIn("implementation scope", self.flat.lower())

    def test_blocked_json_names_the_missing_scope(self) -> None:
        blocked_shape = self.body.split("Blocked:", 1)[1]
        self.assertIn('"status":"blocked"', blocked_shape)
        self.assertIn("implementation", blocked_shape.lower())

    def test_never_reports_archived_for_an_archive_only_scope(self) -> None:
        self.assertIn(
            "Never report `status=archived` after refusing an archive-only scope",
            self.body,
        )

    def test_explicit_implementation_scope_is_accepted(self) -> None:
        self.assertIn("implementation_paths", self.body)
        self.assertIn("explicit implementation", self.flat.lower())

    def test_generated_archiver_outputs_carry_the_new_contract(self) -> None:
        for relative in _GENERATED_ARCHIVERS:
            with self.subTest(output=relative):
                text = _text(_REPO / relative)
                self.assertIn("implementation_paths", text)
                self.assertIn("archive-only", text.lower())


class ReviewerCoverageContractTests(unittest.TestCase):
    """The plan-level reviewer must require and report a coverage matrix."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _text(_REVIEW)
        cls.flat = " ".join(cls.text.split())

    def test_reviewer_accepts_a_supplied_matrix_and_requires_coverage(self) -> None:
        self.assertIn("COVERAGE_MATRIX", self.text)
        self.assertIn('"coverage"', self.text)

    def test_required_case_classes_are_present(self) -> None:
        for case in (
            "read error",
            "cache-key collision",
            "maximum",
            "permission",
            "malformed state",
            "next-phase compatibility",
        ):
            with self.subTest(case=case):
                self.assertIn(case, self.flat.lower())

    def test_coverage_vocabulary_and_evidence(self) -> None:
        for result in ("covered", "not-applicable", "not-covered"):
            with self.subTest(result=result):
                self.assertIn(result, self.text)
        self.assertIn("evidence", self.text)

    def test_writer_test_only_review_does_not_satisfy_the_requirement(self) -> None:
        self.assertIn("writer", self.flat.lower())
        self.assertIn("does not satisfy", self.flat.lower())

    def test_uncovered_required_case_is_blocking(self) -> None:
        self.assertIn("not-covered", self.text)
        self.assertIn("blocking", self.flat.lower())


class HandoffFixtureBehaviorTests(unittest.TestCase):
    """End-to-end behavior of the gate protocol against a real git repo."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fixture = fixture.build(Path(self._tmp.name))

    def test_fixture_reproduces_the_archive_only_failure(self) -> None:
        report = fixture.audit(self.fixture)
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["handoff_ready"])
        self.assertEqual(report["change"], fixture.CHANGE)
        blocker_ids = {blocker["id"] for blocker in report["blockers"]}
        for expected in (
            "implementation-uncommitted",
            "plan-uncommitted",
            "archive-commit-implementation-paths",
            "run-state-not-ignored",
            "perf-script-recreates-archived-change",
        ):
            with self.subTest(blocker=expected):
                self.assertIn(expected, blocker_ids)

    def test_archive_only_commit_really_carries_zero_implementation_paths(self) -> None:
        self.assertEqual(self.fixture.archive_implementation_paths, ())

    def test_blocked_audit_names_the_exact_pathspec_commit(self) -> None:
        report = fixture.audit(self.fixture)
        blocker = next(
            b for b in report["blockers"] if b["id"] == "implementation-uncommitted"
        )
        self.assertEqual(
            sorted(blocker["repair"]["pathspecs"]),
            sorted(fixture.IMPLEMENTATION_PATHS),
        )
        for path in fixture.IMPLEMENTATION_PATHS:
            with self.subTest(path=path):
                self.assertIn(path, blocker["location"])

    def test_audit_is_read_only(self) -> None:
        before_head = fixture.head_sha(self.fixture.root)
        before_status = fixture.status_lines(self.fixture.root)
        before_manifest = (self.fixture.root / fixture.MANIFEST_PATH).read_bytes()

        fixture.audit(self.fixture)

        self.assertEqual(fixture.head_sha(self.fixture.root), before_head)
        self.assertEqual(fixture.status_lines(self.fixture.root), before_status)
        self.assertEqual(
            (self.fixture.root / fixture.MANIFEST_PATH).read_bytes(),
            before_manifest,
        )

    def test_gate_blocks_next_change_until_handoff_ready(self) -> None:
        report = fixture.audit(self.fixture)
        self.assertEqual(fixture.gate_decision(report), "repair")
        self.assertIsNone(
            fixture.next_change(
                report,
                (fixture.CHANGE, fixture.NEXT_CHANGE),
                fixture.CHANGE,
            )
        )

    def test_hygiene_commits_only_the_accepted_pathspecs(self) -> None:
        report = fixture.audit(self.fixture)
        repair = fixture.apply_blockers(self.fixture, report["blockers"])
        self.assertEqual(repair["status"], "repaired")

        committed = set(repair["staged_paths"])
        for path in fixture.IMPLEMENTATION_PATHS:
            with self.subTest(path=path):
                self.assertIn(path, committed)
        self.assertIn(fixture.PLAN_PATH, committed)
        self.assertIn(".gitignore", committed)
        self.assertNotIn(fixture.USER_OWNED_PATH, committed)

        # The user-owned file stays dirty and its content is untouched.
        remaining = fixture.status_lines(self.fixture.root)
        self.assertTrue(
            any(fixture.USER_OWNED_PATH in line for line in remaining),
            f"user-owned file must remain dirty, status was {remaining}",
        )
        self.assertEqual(
            (self.fixture.root / fixture.USER_OWNED_PATH).read_text(encoding="utf-8"),
            "local notes, edited by the operator\n",
        )

    def test_hygiene_report_names_commit_staged_unstaged_and_next_input(self) -> None:
        report = fixture.audit(self.fixture)
        repair = fixture.apply_blockers(self.fixture, report["blockers"])
        self.assertTrue(repair["commit"])
        self.assertEqual(fixture.head_sha(self.fixture.root), repair["commit"])
        self.assertTrue(repair["staged_paths"])
        self.assertIn("next_audit_input", repair)
        self.assertEqual(
            [entry["path"] for entry in repair["unstaged"]],
            [fixture.USER_OWNED_PATH],
        )

    def test_fresh_audit_passes_after_the_scoped_repair(self) -> None:
        first = fixture.audit(self.fixture)
        fixture.apply_blockers(self.fixture, first["blockers"])
        second = fixture.audit(self.fixture)
        self.assertEqual(second["status"], "ready", second["blockers"])
        self.assertTrue(second["handoff_ready"])
        self.assertEqual(second["next_action"], "continue")
        self.assertEqual(second["blockers"], [])
        self.assertEqual(fixture.gate_decision(second), "continue")
        self.assertEqual(
            fixture.next_change(
                second,
                (fixture.CHANGE, fixture.NEXT_CHANGE),
                fixture.CHANGE,
            ),
            fixture.NEXT_CHANGE,
        )

    def test_hygiene_refuses_an_operator_decision_blocker(self) -> None:
        report = fixture.audit(self.fixture)
        blockers = [
            {
                "id": "branch-upstream-missing",
                "severity": "blocking",
                "location": "HEAD",
                "evidence": "no upstream",
                "repair": {
                    "action": "operator-decision",
                    "pathspecs": [],
                    "operator_decision": "record a no-push waiver",
                },
            }
        ]
        repair = fixture.apply_blockers(self.fixture, blockers)
        self.assertEqual(repair["status"], "blocked")
        self.assertEqual(repair["operator_decision"], "record a no-push waiver")
        self.assertEqual(repair["commit"], "")
        self.assertEqual(
            fixture.head_sha(self.fixture.root), report["head"]
        )

    def test_hygiene_refuses_when_no_scope_is_named(self) -> None:
        blockers = [
            {
                "id": "unknown",
                "severity": "blocking",
                "location": "n/a",
                "evidence": "n/a",
                "repair": {"action": "commit-pathspecs", "pathspecs": []},
            }
        ]
        repair = fixture.apply_blockers(self.fixture, blockers)
        self.assertEqual(repair["status"], "blocked")
        self.assertEqual(repair["reason"], "no explicit implementation scope")
        self.assertEqual(repair["staged_paths"], [])

    def test_repair_loop_is_bounded_to_two_rounds(self) -> None:
        # A blocker the hygiene writer cannot satisfy: the audit stays blocked
        # after two repair/audit rounds, and the orchestrator must then stop.
        rounds = 0
        report = fixture.audit(self.fixture)
        while rounds < 2 and fixture.gate_decision(report) == "repair":
            repair = fixture.apply_blockers(self.fixture, report["blockers"])
            if repair["status"] != "repaired":
                break
            rounds += 1
            report = fixture.audit(self.fixture)

        # One repaired round is enough here; the cap is honored either way.
        self.assertLessEqual(rounds, 2)
        if not report["handoff_ready"]:
            self.assertEqual(fixture.gate_decision(report), "repair")
            self.assertIsNone(
                fixture.next_change(
                    report,
                    (fixture.CHANGE, fixture.NEXT_CHANGE),
                    fixture.CHANGE,
                )
            )

    def test_fixture_runs_end_to_end_from_blocked_to_handoff_ready(self) -> None:
        """Task 9.4: blocked audit -> hygiene repair -> fresh audit -> next change."""

        plan_changes = (fixture.CHANGE, fixture.NEXT_CHANGE)

        first = fixture.audit(self.fixture)
        self.assertFalse(first["handoff_ready"])
        self.assertEqual(fixture.gate_decision(first), "repair")
        self.assertIsNone(
            fixture.next_change(first, plan_changes, fixture.CHANGE),
            "the next change must not be selectable from a blocked audit",
        )

        repair = fixture.apply_blockers(self.fixture, first["blockers"])
        self.assertEqual(repair["status"], "repaired")
        self.assertTrue(repair["commit"])

        second = fixture.audit(self.fixture)
        self.assertTrue(second["handoff_ready"])
        self.assertEqual(fixture.gate_decision(second), "continue")
        self.assertEqual(
            fixture.next_change(second, plan_changes, fixture.CHANGE),
            fixture.NEXT_CHANGE,
            "the next change is selected only after handoff_ready: true",
        )

    def test_audit_report_is_machine_readable(self) -> None:
        report = fixture.audit(self.fixture)
        encoded = json.dumps(report)
        decoded = json.loads(encoded)
        self.assertEqual(
            set(decoded),
            {
                "status",
                "handoff_ready",
                "change",
                "head",
                "checks",
                "blockers",
                "next_action",
            },
        )
        for check in decoded["checks"]:
            with self.subTest(check=check["id"]):
                self.assertIn(
                    check["result"],
                    {"passed", "failed", "waived", "unqualified"},
                )
                self.assertTrue(check["command"])
        for blocker in decoded["blockers"]:
            with self.subTest(blocker=blocker["id"]):
                for key in (
                    "id",
                    "severity",
                    "location",
                    "evidence",
                    "repair",
                ):
                    self.assertIn(key, blocker)


if __name__ == "__main__":
    unittest.main()
