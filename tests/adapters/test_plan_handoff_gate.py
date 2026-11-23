"""Contract and behavior tests for the plan-level Phase Transition Gate.

Two families live here:

* **Static contracts** read the shipped agent surfaces and assert the gate,
  the two new agents, the archiver's archive-only refusal, and the reviewer's
  coverage matrix are actually present. A prompt surface is the product here,
  so a missing paragraph is a missing feature.
* **Behavior tests** drive :mod:`tests.adapters.handoff_fixture` against a real
  temporary git repository: an archive-only commit blocks the handoff, the
  audit is read-only, a scoped repair commits only the accepted pathspecs, the
  user-owned file stays dirty, an unauthored next change is not a blocker while
  a planned-slug collision is, local-only delivery passes without an upstream,
  an evidence retention gap is repaired or carried forward as ``unqualified``,
  a cap override permits another repair round, a gated change is never started,
  and the next change is selected only after ``handoff_ready: true``.
"""

from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

#: The checks the auditor must still run. ``next-change-intake`` and
#: ``branch-upstream`` are gone: an unauthored next change is expected after
#: handoff, and delivery is local.
_HARD_AUDIT_CHECKS = (
    "tracked-tree-clean",
    "source-manifest-at-head",
    "plan-at-head",
    "archive-commit-implementation-paths",
    "run-state-ignored",
    "evidence-at-head",
    "perf-script-from-head",
    "fresh-checkout-checks",
)

_REMOVED_AUDIT_CHECKS = ("next-change-intake", "branch-upstream")


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


def _check_ids(text: str) -> set[str]:
    """Return the ``### N. `<check-id>` `` headings of an audit checklist."""

    return set(re.findall(r"^###\s+\d+\.\s+`([a-z0-9-]+)`", text, re.MULTILINE))


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

    def test_repair_loop_cap_is_advisory(self) -> None:
        # The two-round limit remains a planning threshold, but exceeding it is
        # a recorded `cap_override`, never a terminal condition.
        self.assertIn("opsx-repo-hygiene", self.text)
        self.assertIn("cap_override", self.text)
        self.assertIn("advisory", self.flat.lower())
        self.assertIn("repair/audit rounds", self.flat.lower())

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
        for check in _HARD_AUDIT_CHECKS + ("local-only-delivery",):
            with self.subTest(check=check):
                self.assertIn(check, self.text)

    def test_removed_checks_are_no_longer_check_ids(self) -> None:
        ids = _check_ids(self.text)
        self.assertTrue(ids, "the audit checklist headings were not found")
        for check in _HARD_AUDIT_CHECKS + ("local-only-delivery",):
            with self.subTest(check=check):
                self.assertIn(check, ids)
        for retired in _REMOVED_AUDIT_CHECKS:
            with self.subTest(retired=retired):
                self.assertNotIn(retired, ids)

    def test_next_proposal_is_not_an_intake_requirement(self) -> None:
        self.assertNotIn(
            "its required intake artifacts exist at `HEAD`",
            self.flat,
            "the auditor must not require the next change's proposal before handoff",
        )
        self.assertIn("slug", self.flat.lower())

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

    def test_stop_is_reserved_for_a_human_only_boundary(self) -> None:
        self.assertIn("human-only", self.flat.lower())
        self.assertIn("unauthored", self.flat.lower())

    def test_slug_collision_is_narrowed_to_plan_reachable_occupants(self) -> None:
        self.assertIn(
            "A plan-reachable slug collision is `failed` with `severity: blocking` "
            "and `repair.action: resolve-slug-collision`",
            self.flat,
        )
        self.assertIn(
            "`next_action` is `repair`. An out-of-plan collision is `failed` with "
            "`severity: blocking` and `repair.action: operator-decision`",
            self.flat,
        )
        self.assertIn(
            "`resolve-slug-collision` is owned by the orchestrator, not by "
            "`opsx-repo-hygiene`.",
            self.flat,
        )
        self.assertIn(
            "The hygiene writer performs no repository mutation for it", self.flat
        )
        self.assertIn(
            "No worker may archive, rename, move, or rewrite an out-of-plan change "
            "to free the slug",
            self.flat,
        )
        # An archived same-slug directory stays informational, never a blocker.
        self.assertIn("informational context", self.flat)
        self.assertIn("not a blocker", self.flat)
        # The action is part of the enumerated contract the orchestrator reads.
        enum = self.text.split('"action": "', 1)[1].split('"', 1)[0]
        self.assertIn("resolve-slug-collision", enum)
        for action in (
            "commit-pathspecs",
            "ignore-run-state",
            "retarget-script",
            "repair-evidence",
            "operator-decision",
            "none",
        ):
            with self.subTest(action=action):
                self.assertIn(action, enum)

    def test_sole_claim_basis_rule_names_the_required_support(self) -> None:
        self.assertIn("Sole-basis rule", self.flat)
        self.assertIn(
            "another committed artifact that supports the same claim", self.flat
        )
        self.assertIn(
            "the committed plan's explicit acceptance of the residual risk for "
            "that claim",
            self.flat,
        )
        self.assertIn(
            "a passing hard check in this report that proves the claim without "
            "the missing artifact",
            self.flat,
        )
        self.assertIn(
            "When none of the three exists at `HEAD`, the artifact is the sole "
            "basis.",
            self.flat,
        )

    def test_sole_claim_support_must_substantiate_the_claim(self) -> None:
        # Existing at HEAD is not support: the committed content must cover the
        # claim, so an unrelated committed file cannot qualify.
        self.assertIn(
            "show that its content substantiates the claim, because a file that "
            "merely exists at `HEAD` is not support",
            self.flat,
        )

    def test_an_unreadable_manifest_is_a_check_result_not_a_crash(self) -> None:
        self.assertIn(
            "A committed manifest that exists but cannot be read, parsed, or "
            "structurally used",
            self.flat,
        )
        self.assertIn("never a crash, never a silent pass", self.flat)
        self.assertIn("`repair.action` is `repair-evidence`", self.flat)
        self.assertIn(
            "`handoff_ready` is `false` and `next_action` is `repair`", self.flat
        )
        self.assertIn(
            "`source-manifest-at-head` and `plan-at-head` — are `unqualified`",
            self.flat,
        )
        # A malformed nested member, including an empty or null path/sha256, is
        # the same unreadable state, not a crash and not a silent pass.
        self.assertIn(
            "including a member that is not an object, lacks a required "
            "`path`/`sha256` key, or carries an empty, `null`, or non-string "
            "`path` or `sha256`",
            self.flat,
        )

    def test_absent_and_unreadable_manifests_are_distinguished(self) -> None:
        self.assertIn(
            "When the repository has no committed evidence manifest at all, the "
            "check is `passed` with that statement as its evidence",
            self.flat,
        )
        self.assertIn(
            "A plan or claim that does reference a manifest or artifact which is "
            "absent is the missing-artifact case above",
            self.flat,
        )


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

    def _repaired(self) -> dict:
        """Run the scoped repair and return the fresh, handoff-ready report."""

        report = fixture.audit(self.fixture)
        repair = fixture.apply_blockers(self.fixture, report["blockers"])
        self.assertEqual(repair["status"], "repaired", repair.get("reason"))
        fresh = fixture.audit(self.fixture)
        self.assertTrue(fresh["handoff_ready"], fresh["blockers"])
        return fresh

    def _assert_sole_basis_blocker(self, report: dict) -> dict:
        """Assert the report stops because the claim's only support is missing."""

        self.assertFalse(report["handoff_ready"], report["blockers"])
        self.assertEqual(report["next_action"], "stop")
        self.assertEqual(fixture.gate_decision(report), "stop")
        blocker = next(
            b for b in report["blockers"] if b["id"] == "evidence-sole-claim-basis"
        )
        self.assertEqual(blocker["severity"], "blocking")
        self.assertEqual(blocker["repair"]["action"], "operator-decision")
        self.assertEqual(blocker["location"], fixture.GONE_CLAIM_ID)
        self.assertIn(
            fixture.GONE_CLAIM_STATEMENT, blocker["repair"]["operator_decision"]
        )
        self.assertIn(fixture.GONE_EVIDENCE_PATH, blocker["evidence"])
        return blocker

    # --- baseline failure and repair ------------------------------------

    def test_fixture_reproduces_the_archive_only_failure(self) -> None:
        report = fixture.audit(self.fixture)
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["handoff_ready"])
        self.assertEqual(report["change"], fixture.CHANGE)
        self.assertEqual(report["next_action"], "repair")
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
        # Nothing internal is escalated to an operator decision.
        for blocker in report["blockers"]:
            with self.subTest(blocker=blocker["id"]):
                self.assertNotEqual(
                    blocker["repair"]["action"], "operator-decision"
                )

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

    def test_hygiene_stops_on_an_operator_authority_restriction(self) -> None:
        waiver = fixture.apply_operator_waiver(self.fixture)
        report = fixture.audit(self.fixture)

        # The operator waiver is the true human-only boundary here, and it is
        # reported verbatim rather than as an internal repair.
        self.assertEqual(report["next_action"], "stop")
        self.assertEqual(fixture.gate_decision(report), "stop")
        blocker = next(
            b for b in report["blockers"] if b["id"] == "archive-only-waiver"
        )
        self.assertEqual(blocker["repair"]["action"], "operator-decision")
        self.assertEqual(blocker["repair"]["operator_decision"], waiver)

        repair = fixture.apply_blockers(self.fixture, report["blockers"])
        self.assertEqual(repair["status"], "blocked")
        self.assertEqual(repair["operator_decision"], waiver)
        self.assertEqual(repair["commit"], "")
        self.assertEqual(repair["phases"], [])
        self.assertEqual(repair["staged_paths"], [])
        self.assertEqual(fixture.head_sha(self.fixture.root), report["head"])

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

    # --- 1.1 / 8.6: unauthored next change versus a slug collision ------

    def test_unauthored_next_change_passes_the_handoff(self) -> None:
        fresh = self._repaired()
        self.assertFalse(
            (self.fixture.root / fixture.NEXT_CHANGE_REL).exists(),
            "the fixture must not pre-author the next change",
        )
        state = fixture.next_change_slug_state(self.fixture)
        self.assertEqual(state["state"], "unauthored")
        self.assertFalse(state["active"])
        self.assertFalse(state["authored"])
        self.assertFalse(state["collision"])
        self.assertFalse(state["proposal_required"])

        check = next(c for c in fresh["checks"] if c["id"] == "next-change-slug")
        self.assertEqual(check["result"], "passed")
        self.assertIn("not required", check["evidence"])
        blocker_ids = {b["id"] for b in fresh["blockers"]}
        self.assertNotIn("next-change-intake-missing", blocker_ids)
        self.assertNotIn("next-change-slug-collision", blocker_ids)
        self.assertTrue(fresh["handoff_ready"])

    def test_slug_collision_fails_the_handoff(self) -> None:
        self._repaired()
        colliding = fixture.inject_slug_collision(self.fixture)

        state = fixture.next_change_slug_state(self.fixture)
        self.assertEqual(state["state"], "active-collision")
        self.assertTrue(state["collision"])
        self.assertTrue(state["authored"])
        self.assertEqual(state["collision_directory"], colliding)

        report = fixture.audit(self.fixture)
        self.assertFalse(report["handoff_ready"])
        blocker = next(
            b for b in report["blockers"] if b["id"] == "next-change-slug-collision"
        )
        self.assertIn(colliding, blocker["location"])
        self.assertIn(fixture.NEXT_CHANGE, blocker["evidence"])

        # The default occupant is out-of-plan: no worker may archive, rename,
        # or rewrite it, so the collision is a human-only operator decision.
        self.assertTrue(state["external_collision"])
        self.assertFalse(state["plan_reachable"])
        self.assertEqual(blocker["repair"]["action"], "operator-decision")
        self.assertIn(fixture.OCCUPYING_CHANGE, blocker["repair"]["operator_decision"])
        self.assertIn("out-of-plan", blocker["repair"]["operator_decision"])
        self.assertEqual(report["next_action"], "stop")
        self.assertEqual(fixture.gate_decision(report), "stop")
        self.assertIsNone(
            fixture.next_change(report, fixture.PLAN_CHANGES, fixture.CHANGE)
        )

        # The hygiene writer performs no repository mutation for it either.
        head = fixture.head_sha(self.fixture.root)
        repair = fixture.apply_blockers(self.fixture, report["blockers"])
        self.assertEqual(repair["status"], "blocked")
        self.assertEqual(repair["blocker"], "next-change-slug-collision")
        self.assertIn("operator decision", repair["reason"])
        self.assertIn(fixture.OCCUPYING_CHANGE, repair["operator_decision"])
        self.assertEqual(repair["staged_paths"], [])
        self.assertEqual(fixture.head_sha(self.fixture.root), head)

    def test_the_planned_change_already_active_is_not_a_collision(self) -> None:
        self._repaired()
        active = fixture.inject_active_planned_change(self.fixture)

        state = fixture.next_change_slug_state(self.fixture)
        self.assertEqual(state["state"], "active-planned")
        self.assertTrue(state["active"])
        self.assertTrue(state["same_planned_change"])
        self.assertFalse(state["different_change"])
        self.assertFalse(state["collision"])
        self.assertEqual(state["declared_change_id"], fixture.NEXT_CHANGE)
        self.assertEqual(state["collision_directory"], "")
        self.assertEqual(state["active_directory"], active)

        head = fixture.head_sha(self.fixture.root)
        report = fixture.audit(self.fixture)

        # The planned change is already the active one: readiness passes with no
        # blocker, and the audit mutates nothing.
        self.assertEqual(fixture.head_sha(self.fixture.root), head)
        self.assertTrue(report["handoff_ready"], report["blockers"])
        self.assertEqual(report["blockers"], [])
        check = next(c for c in report["checks"] if c["id"] == "next-change-slug")
        self.assertEqual(check["result"], "passed")
        self.assertIn("not a collision", check["evidence"])
        self.assertIn("no mutation", check["evidence"])
        self.assertEqual(report["next_action"], "continue")
        self.assertEqual(fixture.gate_decision(report), "continue")

    def test_an_out_of_plan_collision_stops_without_mutating_the_repository(
        self,
    ) -> None:
        self._repaired()
        fixture.inject_slug_collision(self.fixture)

        state = fixture.next_change_slug_state(self.fixture)
        self.assertEqual(state["state"], "active-collision")
        self.assertTrue(state["different_change"])
        self.assertTrue(state["external_collision"])
        self.assertFalse(state["plan_reachable"])
        self.assertEqual(state["occupying_change"], fixture.OCCUPYING_CHANGE)
        self.assertIn("not a plan change", state["informational"])

        blocked = fixture.audit(self.fixture)
        self.assertFalse(blocked["handoff_ready"])
        self.assertEqual(blocked["next_action"], "stop")
        self.assertEqual(fixture.gate_decision(blocked), "stop")
        blocker = next(
            b for b in blocked["blockers"] if b["id"] == "next-change-slug-collision"
        )
        self.assertEqual(blocker["repair"]["action"], "operator-decision")
        self.assertFalse(blocker["plan_reachable"])
        self.assertTrue(blocker["external_collision"])
        self.assertIn(fixture.OCCUPYING_CHANGE, blocker["repair"]["operator_decision"])
        self.assertIn("may not archive", blocker["repair"]["operator_decision"])

        # The resolution is the operator's: the orchestrator and the hygiene
        # writer make no repository mutation and do not archive the external
        # change.
        head = fixture.head_sha(self.fixture.root)
        resolve = fixture.apply_blockers(self.fixture, blocked["blockers"])
        self.assertEqual(resolve["status"], "blocked")
        self.assertEqual(resolve["operator_decision"], blocker["repair"]["operator_decision"])
        self.assertEqual(fixture.head_sha(self.fixture.root), head)
        self.assertTrue(
            (self.fixture.root / fixture.NEXT_CHANGE_REL / "proposal.md").is_file(),
            "an out-of-plan change's directory must not be moved or archived",
        )

    def test_a_plan_reachable_collision_is_an_orchestrator_repair(self) -> None:
        self._repaired()
        self.fixture.plan_changes = fixture.GATED_PLAN_CHANGES
        fixture.inject_slug_collision(
            self.fixture, occupying_change=fixture.GATED_CHANGE
        )

        state = fixture.next_change_slug_state(self.fixture)
        self.assertEqual(state["state"], "active-collision")
        self.assertTrue(state["different_change"])
        self.assertTrue(state["plan_reachable"])
        self.assertFalse(state["external_collision"])
        self.assertEqual(state["occupying_change"], fixture.GATED_CHANGE)

        blocked = fixture.audit(self.fixture)
        self.assertFalse(blocked["handoff_ready"])
        self.assertEqual(blocked["next_action"], "repair")
        self.assertEqual(fixture.gate_decision(blocked), "repair")
        blocker = next(
            b for b in blocked["blockers"] if b["id"] == "next-change-slug-collision"
        )
        self.assertEqual(blocker["repair"]["action"], "resolve-slug-collision")
        self.assertEqual(blocker["repair"]["owner"], "orchestrator")
        self.assertTrue(blocker["plan_reachable"])
        self.assertIn(fixture.GATED_CHANGE, blocker["evidence"])

        # The orchestrator-owned resolution performs no repository mutation.
        head = fixture.head_sha(self.fixture.root)
        resolve = fixture.apply_blockers(self.fixture, blocked["blockers"])
        self.assertEqual(resolve["status"], "blocked")
        self.assertEqual(fixture.head_sha(self.fixture.root), head)

    def test_an_archived_same_slug_directory_is_informational(self) -> None:
        self._repaired()
        archived = fixture.inject_archived_next_change(self.fixture)

        state = fixture.next_change_slug_state(self.fixture)
        self.assertEqual(state["state"], "archived-only")
        self.assertFalse(state["collision"])
        self.assertTrue(state["archived_only"])
        self.assertEqual(state["archived_directory"], archived)
        self.assertEqual(state["collision_directory"], "")
        self.assertTrue(state["informational"])

        report = fixture.audit(self.fixture)
        self.assertTrue(report["handoff_ready"], report["blockers"])
        self.assertEqual(report["blockers"], [])
        check = next(c for c in report["checks"] if c["id"] == "next-change-slug")
        self.assertEqual(check["result"], "passed")
        self.assertIn(archived, check["evidence"])
        self.assertIn("informational", check["evidence"])
        self.assertTrue(
            any(archived in note for note in report["informational"]),
            report["informational"],
        )
        self.assertEqual(report["next_action"], "continue")
        self.assertEqual(fixture.gate_decision(report), "continue")

    def test_dependency_readiness_is_still_checked(self) -> None:
        self._repaired()
        self.fixture.dependencies = {fixture.NEXT_CHANGE: ("after-gate",)}

        state = fixture.dependencies_state(self.fixture)
        self.assertFalse(state["ready"])
        self.assertEqual(state["unmet"], ("after-gate",))

        report = fixture.audit(self.fixture)
        self.assertFalse(report["handoff_ready"])
        blocker = next(
            b for b in report["blockers"] if b["id"] == "next-change-dependency-unmet"
        )
        self.assertIn("after-gate", blocker["evidence"])

        # An accepted deferral clears the same dependency.
        self.fixture.deferred = ("after-gate",)
        deferred = fixture.dependencies_state(self.fixture)
        self.assertTrue(deferred["ready"])
        self.assertEqual(deferred["deferred"], ("after-gate",))
        self.assertTrue(fixture.audit(self.fixture)["handoff_ready"])

    # --- 1.2 / 8.6: local-only delivery --------------------------------

    def test_local_branch_without_upstream_passes(self) -> None:
        fresh = self._repaired()
        delivery = fixture.delivery_state(self.fixture)
        self.assertTrue(delivery["local_only"])
        self.assertEqual(delivery["upstream"], "")

        check = next(c for c in fresh["checks"] if c["id"] == "local-only-delivery")
        self.assertEqual(check["result"], "passed")
        self.assertIn("local", check["evidence"])
        self.assertIn("no push", check["evidence"])
        self.assertTrue(fresh["handoff_ready"])
        self.assertNotIn("no_push_waiver", fixture.load_manifest(self.fixture))

    def test_a_configured_upstream_is_informational(self) -> None:
        self._repaired()
        upstream = fixture.add_remote_upstream(self.fixture)
        report = fixture.audit(self.fixture)

        delivery = fixture.delivery_state(self.fixture)
        self.assertEqual(delivery["upstream"], upstream)
        self.assertFalse(delivery["local_only"])
        self.assertTrue(delivery["informational"])

        check = next(c for c in report["checks"] if c["id"] == "local-only-delivery")
        self.assertEqual(check["result"], "passed")
        self.assertIn(upstream, check["evidence"])
        self.assertIn("informational", check["evidence"])
        self.assertTrue(
            any(upstream in note for note in report["informational"]),
            report["informational"],
        )
        self.assertTrue(report["handoff_ready"])

    # --- 1.3 / 8.6: evidence retention and phased repair ----------------

    def test_baseline_commits_before_evidence(self) -> None:
        fixture.inject_stale_evidence_checksum(self.fixture)
        original = (self.fixture.root / fixture.EVIDENCE_PATH).read_bytes()
        original_sha = fixture.sha256_bytes(original)

        report = fixture.audit(self.fixture)
        blocker_ids = {b["id"] for b in report["blockers"]}
        self.assertIn("implementation-uncommitted", blocker_ids)
        self.assertIn("evidence-retention-gap", blocker_ids)

        repair = fixture.apply_blockers(self.fixture, report["blockers"])
        self.assertEqual(repair["status"], "repaired", repair.get("reason"))
        self.assertEqual(
            [phase["phase"] for phase in repair["phases"]],
            ["baseline", "evidence"],
        )
        baseline, evidence = repair["phases"]
        self.assertNotEqual(baseline["commit"], evidence["commit"])
        self.assertTrue(
            fixture.is_ancestor(self.fixture, baseline["commit"], evidence["commit"]),
            "the evidence commit must land on top of the baseline commit",
        )
        self.assertIn(fixture.IMPLEMENTATION_PATHS[0], baseline["staged_paths"])
        self.assertIn(fixture.PLAN_PATH, baseline["staged_paths"])
        self.assertNotIn(fixture.EVIDENCE_SUPERSEDE_PATH, baseline["staged_paths"])
        # The manifest is committed content, so the superseding entry and the
        # recomputed checksum travel in the same explicit Phase B commit.
        self.assertEqual(
            evidence["staged_paths"],
            [fixture.MANIFEST_PATH, fixture.EVIDENCE_SUPERSEDE_PATH],
        )

        # The retained original is preserved byte-for-byte; the new version
        # carries the updated checksum.
        self.assertEqual(
            (self.fixture.root / fixture.EVIDENCE_PATH).read_bytes(), original
        )
        manifest = fixture.load_manifest(self.fixture)
        superseding = next(
            entry
            for entry in manifest["evidence"]
            if entry["path"] == fixture.EVIDENCE_SUPERSEDE_PATH
        )
        self.assertEqual(superseding["supersedes"], fixture.EVIDENCE_PATH)
        self.assertEqual(
            superseding["sha256"],
            fixture.sha256_path(
                self.fixture.root / fixture.EVIDENCE_SUPERSEDE_PATH
            ),
        )
        pointer = next(
            entry
            for entry in manifest["evidence"]
            if entry.get("path") == fixture.EVIDENCE_PATH
        )
        self.assertEqual(pointer["superseded_by"], fixture.EVIDENCE_SUPERSEDE_PATH)
        self.assertEqual(
            pointer["superseded_sha256"], superseding["sha256"]
        )
        self.assertNotEqual(superseding["sha256"], original_sha)

        self.assertTrue(fixture.audit(self.fixture)["handoff_ready"])

    def test_a_repairable_evidence_gap_returns_a_repair_action(self) -> None:
        self._repaired()
        fixture.inject_stale_evidence_checksum(self.fixture)

        report = fixture.audit(self.fixture)
        self.assertFalse(report["handoff_ready"])
        self.assertEqual(report["next_action"], "repair")
        blocker = next(
            b for b in report["blockers"] if b["id"] == "evidence-retention-gap"
        )
        self.assertEqual(blocker["repair"]["action"], "repair-evidence")
        self.assertEqual(blocker["repair"]["operator_decision"], "none")
        self.assertEqual(
            blocker["repair"]["supersede_path"], fixture.EVIDENCE_SUPERSEDE_PATH
        )
        self.assertEqual(report["carry_forwards"], [])

    def test_missing_artifact_stays_unqualified(self) -> None:
        self._repaired()
        fixture.inject_missing_evidence_artifact(
            self.fixture, sole_claim_basis=False
        )

        # The claim keeps independent committed support whose content actually
        # substantiates it (it names the claim id), so the gap is recordable
        # rather than adjudicable.
        claims = fixture.claim_support_state(self.fixture)
        self.assertFalse(claims["sole_claim_basis"])
        self.assertEqual(
            claims["claims"][0]["committed_support"], [fixture.SUPPORT_PATH]
        )
        self.assertIn(
            fixture.GONE_CLAIM_ID,
            (self.fixture.root / fixture.SUPPORT_PATH).read_text(encoding="utf-8"),
        )

        report = fixture.audit(self.fixture)
        check = next(c for c in report["checks"] if c["id"] == "evidence-at-head")
        self.assertEqual(check["result"], "unqualified")
        self.assertIn(fixture.GONE_EVIDENCE_PATH, check["evidence"])

        # Not an operator decision, not a blocking repair, and not a pass.
        self.assertEqual(report["blockers"], [])
        self.assertNotEqual(report["next_action"], "stop")
        self.assertTrue(report["carry_forwards"])
        record = report["carry_forwards"][0]
        self.assertEqual(record["check"], "evidence-at-head")
        self.assertEqual(record["evidence"], fixture.GONE_EVIDENCE_PATH)
        self.assertEqual(record["manifest"], fixture.MANIFEST_PATH)
        self.assertFalse(record["verified"])
        self.assertTrue(record["missing"])
        self.assertEqual(record["disposition"], "unqualified")
        self.assertTrue(report["handoff_ready"])

    def test_a_missing_artifact_that_is_the_sole_claim_basis_stops(self) -> None:
        self._repaired()
        fixture.inject_missing_evidence_artifact(
            self.fixture, sole_claim_basis=True
        )

        claims = fixture.claim_support_state(self.fixture)
        self.assertTrue(claims["sole_claim_basis"])
        self.assertEqual(claims["adjudicated"][0]["committed_support"], [])

        report = fixture.audit(self.fixture)
        check = next(c for c in report["checks"] if c["id"] == "evidence-at-head")
        self.assertEqual(check["result"], "unqualified")
        self.assertFalse(report["handoff_ready"])
        self.assertEqual(report["next_action"], "stop")
        self.assertEqual(fixture.gate_decision(report), "stop")

        blocker = next(
            b for b in report["blockers"] if b["id"] == "evidence-sole-claim-basis"
        )
        self.assertEqual(blocker["severity"], "blocking")
        self.assertEqual(blocker["repair"]["action"], "operator-decision")
        self.assertIn(
            fixture.GONE_CLAIM_STATEMENT, blocker["repair"]["operator_decision"]
        )
        self.assertIn(fixture.GONE_EVIDENCE_PATH, blocker["evidence"])

        # The carry-forward record still documents what is missing.
        self.assertTrue(report["carry_forwards"])

        # Hygiene does not act on a human-only decision.
        repair = fixture.apply_blockers(self.fixture, report["blockers"])
        self.assertEqual(repair["status"], "blocked")
        self.assertEqual(repair["commit"], "")
        self.assertIn("operator decision", repair["reason"])

    def test_declared_support_that_is_not_committed_at_head_is_not_proof(self) -> None:
        self._repaired()

        # A rendering kept only in run state: readable in the worktree, absent
        # from HEAD, and inside the ignored directory so the tree stays clean.
        worktree_only = self.fixture.root / fixture.WORKTREE_ONLY_SUPPORT_PATH
        worktree_only.parent.mkdir(parents=True, exist_ok=True)
        worktree_only.write_text("a rendering kept in run state\n", encoding="utf-8")

        variants = (
            (
                "absent-at-head",
                fixture.UNCOMMITTED_SUPPORT_PATH,
                fixture.sha256_bytes(b"declared but never written\n"),
            ),
            (
                "worktree-only",
                fixture.WORKTREE_ONLY_SUPPORT_PATH,
                fixture.sha256_path(worktree_only),
            ),
        )
        for label, path, recorded in variants:
            with self.subTest(label=label):
                fixture.inject_missing_evidence_artifact(
                    self.fixture,
                    independent_support=[{"path": path, "sha256": recorded}],
                )

                # A declared path is not support: it must resolve at HEAD.
                claims = fixture.claim_support_state(self.fixture)
                claim = claims["claims"][0]
                self.assertEqual(claim["committed_support"], [], claim)
                self.assertEqual(claim["resolved_evidence"], [], claim)
                self.assertTrue(claims["sole_claim_basis"], claim)

                # So the missing artifact is the sole basis: a human-only stop.
                report = fixture.audit(self.fixture)
                self._assert_sole_basis_blocker(report)

    def test_declared_support_with_a_mismatched_hash_is_not_proof(self) -> None:
        self._repaired()

        # The path is committed at HEAD, so only the recorded hash can be
        # wrong: the check must compare HEAD content, not merely find the path.
        recorded = fixture.sha256_bytes(b"a different rendering of the bench\n")
        actual = fixture.sha256_path(self.fixture.root / fixture.EVIDENCE_PATH)
        self.assertNotEqual(recorded, actual)

        fixture.inject_missing_evidence_artifact(
            self.fixture,
            independent_support=[
                {"path": fixture.EVIDENCE_PATH, "sha256": recorded}
            ],
        )

        claims = fixture.claim_support_state(self.fixture)
        claim = claims["claims"][0]
        self.assertEqual(claim["committed_support"], [], claim)
        self.assertEqual(claim["resolved_evidence"], [], claim)
        self.assertTrue(claims["sole_claim_basis"], claim)

        report = fixture.audit(self.fixture)
        self._assert_sole_basis_blocker(report)

    def test_a_structurally_malformed_manifest_fails_the_handoff(self) -> None:
        self._repaired()
        fixture.inject_malformed_manifest(self.fixture, '{"accepted": [')

        # The audit must report the read failure, not raise it.
        report = fixture.audit(self.fixture)
        self.assertFalse(report["handoff_ready"])
        self.assertEqual(report["next_action"], "repair")
        self.assertEqual(fixture.gate_decision(report), "repair")

        blocker = next(
            b for b in report["blockers"] if b["id"] == "evidence-manifest-unreadable"
        )
        self.assertEqual(blocker["severity"], "blocking")
        self.assertEqual(blocker["repair"]["action"], "repair-evidence")
        self.assertIn("invalid JSON", blocker["evidence"])

        evidence_check = next(
            c for c in report["checks"] if c["id"] == "evidence-at-head"
        )
        self.assertEqual(evidence_check["result"], "failed")
        for check_id in ("source-manifest-at-head", "plan-at-head"):
            with self.subTest(check=check_id):
                entry = next(c for c in report["checks"] if c["id"] == check_id)
                self.assertEqual(entry["result"], "unqualified")

    def test_a_wrong_shaped_manifest_fails_the_handoff(self) -> None:
        self._repaired()
        fixture.inject_malformed_manifest(
            self.fixture, '{"accepted": "src/api/cache.py", "plan": []}'
        )

        report = fixture.audit(self.fixture)
        self.assertFalse(report["handoff_ready"])
        self.assertEqual(report["next_action"], "repair")
        blocker = next(
            b for b in report["blockers"] if b["id"] == "evidence-manifest-unreadable"
        )
        self.assertIn("malformed manifest shape", blocker["evidence"])
        self.assertEqual(blocker["repair"]["action"], "repair-evidence")

    def test_an_unreadable_manifest_fails_the_handoff_without_raising(self) -> None:
        self._repaired()
        fixture.make_manifest_unreadable(self.fixture)

        report = fixture.audit(self.fixture)
        self.assertFalse(report["handoff_ready"])
        self.assertEqual(report["next_action"], "repair")
        blocker = next(
            b for b in report["blockers"] if b["id"] == "evidence-manifest-unreadable"
        )
        self.assertEqual(blocker["severity"], "blocking")
        self.assertEqual(blocker["repair"]["action"], "repair-evidence")
        self.assertIn("unreadable bytes", blocker["evidence"])
        self.assertEqual(blocker["location"], fixture.MANIFEST_PATH)
        check = next(c for c in report["checks"] if c["id"] == "evidence-at-head")
        self.assertEqual(check["result"], "failed")

    def test_a_permission_denied_manifest_fails_the_handoff_without_raising(
        self,
    ) -> None:
        """Drive the PermissionError branch deterministically.

        Patch target: ``tests.adapters.handoff_fixture._read_manifest_text`` —
        the single callable ``read_manifest`` uses — so the rest of the fixture
        keeps reading real files. A real ``chmod 000`` is unreliable and useless
        as root.
        """

        self._repaired()
        with mock.patch.object(
            fixture,
            "_read_manifest_text",
            side_effect=PermissionError(13, "permission denied"),
        ):
            report = fixture.audit(self.fixture)

        self.assertFalse(report["handoff_ready"])
        self.assertEqual(report["next_action"], "repair")
        self.assertEqual(fixture.gate_decision(report), "repair")
        blocker = next(
            b for b in report["blockers"] if b["id"] == "evidence-manifest-unreadable"
        )
        self.assertEqual(blocker["severity"], "blocking")
        self.assertEqual(blocker["repair"]["action"], "repair-evidence")
        self.assertEqual(blocker["location"], fixture.MANIFEST_PATH)
        # Pins the dedicated branch: falling through to the generic OSError
        # handler would report "unreadable bytes" instead.
        self.assertEqual(
            blocker["evidence"], f"permission denied: {fixture.MANIFEST_PATH}"
        )
        self.assertNotIn("unreadable bytes", blocker["evidence"])
        check = next(c for c in report["checks"] if c["id"] == "evidence-at-head")
        self.assertEqual(check["result"], "failed")

    def test_a_malformed_manifest_member_fails_without_raising(self) -> None:
        self._repaired()
        plan_entry = (
            '{"path": "docs/plans/sample-plan.md", "sha256": "recorded-plan-hash"}'
        )
        variants = (
            ("missing-member", f'{{"accepted": [{{}}], "plan": {plan_entry}}}'),
            (
                "missing-sha256",
                '{"accepted": [{"path": "src/api/cache.py"}], "plan": '
                f"{plan_entry}}}",
            ),
            (
                "empty-path-and-sha256",
                '{"accepted": [{"path": "", "sha256": ""}], "plan": '
                f"{plan_entry}}}",
            ),
            (
                "null-path-and-sha256",
                '{"accepted": [{"path": null, "sha256": null}], "plan": '
                f"{plan_entry}}}",
            ),
            (
                "wrong-typed-member",
                '{"accepted": [{"path": 5, "sha256": []}], "plan": '
                f"{plan_entry}}}",
            ),
            (
                "evidence-member-not-an-object",
                '{"accepted": [{"path": "src/api/cache.py", "sha256": "x"}], '
                f'"plan": {plan_entry}, "evidence": ["docs/evidence/bench.txt"]}}',
            ),
            (
                "evidence-empty-path-and-sha256",
                '{"accepted": [{"path": "src/api/cache.py", "sha256": "x"}], '
                f'"plan": {plan_entry}, "evidence": [{{"path": "", "sha256": ""}}]}}',
            ),
            (
                "evidence-missing-sha256",
                '{"accepted": [{"path": "src/api/cache.py", "sha256": "x"}], '
                f'"plan": {plan_entry}, "evidence": [{{"path": "docs/evidence/bench.txt"}}]}}',
            ),
            (
                "evidence-null-path-and-sha256",
                '{"accepted": [{"path": "src/api/cache.py", "sha256": "x"}], '
                f'"plan": {plan_entry}, "evidence": [{{"path": null, "sha256": null}}]}}',
            ),
        )
        for label, text in variants:
            with self.subTest(label=label):
                fixture.inject_malformed_manifest(self.fixture, text)

                # The audit must classify the structural violation inside the
                # envelope, never raise KeyError or AttributeError.
                report = fixture.audit(self.fixture)
                self.assertFalse(report["handoff_ready"])
                self.assertEqual(report["next_action"], "repair")
                blocker = next(
                    b
                    for b in report["blockers"]
                    if b["id"] == "evidence-manifest-unreadable"
                )
                self.assertEqual(blocker["severity"], "blocking")
                self.assertEqual(blocker["repair"]["action"], "repair-evidence")
                self.assertIn("malformed manifest shape", blocker["evidence"])
                check = next(
                    c for c in report["checks"] if c["id"] == "evidence-at-head"
                )
                self.assertEqual(check["result"], "failed")

    def test_a_non_string_supersession_pointer_fails_without_raising(self) -> None:
        self._repaired()
        base_manifest = copy.deepcopy(fixture.load_manifest(self.fixture))
        variants = (
            ("superseded_by-not-a-string", {"superseded_by": 5}),
            ("superseded_sha256-not-a-string", {"superseded_sha256": 5}),
        )
        for label, extra in variants:
            with self.subTest(label=label):
                manifest = copy.deepcopy(base_manifest)
                manifest["evidence"] = [
                    {
                        "path": fixture.EVIDENCE_PATH,
                        "sha256": fixture.sha256_path(
                            self.fixture.root / fixture.EVIDENCE_PATH
                        ),
                        **extra,
                    }
                ]
                fixture.write_manifest(self.fixture, manifest)

                # The evidence check joins the supersession pointer into a HEAD
                # path and a checksum comparison, so a non-string there must be
                # classified structurally rather than reaching path arithmetic.
                report = fixture.audit(self.fixture)
                self.assertFalse(report["handoff_ready"])
                self.assertEqual(report["next_action"], "repair")
                self.assertEqual(fixture.gate_decision(report), "repair")
                blocker = next(
                    b
                    for b in report["blockers"]
                    if b["id"] == "evidence-manifest-unreadable"
                )
                self.assertEqual(blocker["severity"], "blocking")
                self.assertEqual(blocker["repair"]["action"], "repair-evidence")
                self.assertIn("malformed manifest shape", blocker["evidence"])
                self.assertIn("is not a string", blocker["evidence"])
                check = next(
                    c for c in report["checks"] if c["id"] == "evidence-at-head"
                )
                self.assertEqual(check["result"], "failed")

    def test_no_committed_manifest_at_all_passes_the_evidence_check(self) -> None:
        fresh = self._repaired()
        self.assertTrue(fresh["handoff_ready"])
        fixture.remove_manifest(self.fixture)

        report = fixture.audit(self.fixture)
        # No manifest, nothing referencing one: the contract's pass, not the
        # old absent-manifest blocker.
        self.assertNotIn(
            "evidence-manifest-missing",
            {blocker["id"] for blocker in report["blockers"]},
        )
        check = next(c for c in report["checks"] if c["id"] == "evidence-at-head")
        self.assertEqual(check["result"], "passed")
        self.assertIn("no committed evidence manifest", check["evidence"])
        self.assertEqual(report["blockers"], [])
        self.assertTrue(report["handoff_ready"], report["blockers"])
        self.assertEqual(report["next_action"], "continue")
        self.assertEqual(fixture.gate_decision(report), "continue")

    def test_a_manifest_present_only_in_the_worktree_never_passes(self) -> None:
        self._repaired()
        fixture.uncommit_manifest(self.fixture)

        manifest_state = fixture.read_manifest(self.fixture)
        self.assertEqual(manifest_state["state"], "uncommitted")
        self.assertTrue(manifest_state["worktree_present"])
        self.assertIn("only in the worktree", manifest_state["reason"])

        report = fixture.audit(self.fixture)
        self.assertFalse(report["handoff_ready"])
        self.assertEqual(report["next_action"], "repair")
        blocker = next(
            b
            for b in report["blockers"]
            if b["id"] == "evidence-manifest-uncommitted"
        )
        self.assertEqual(blocker["severity"], "blocking")
        self.assertEqual(blocker["repair"]["action"], "commit-pathspecs")
        self.assertEqual(blocker["repair"]["pathspecs"], [fixture.MANIFEST_PATH])
        check = next(c for c in report["checks"] if c["id"] == "evidence-at-head")
        self.assertEqual(check["result"], "failed")
        for check_id in ("source-manifest-at-head", "plan-at-head"):
            with self.subTest(check=check_id):
                entry = next(c for c in report["checks"] if c["id"] == check_id)
                self.assertEqual(entry["result"], "unqualified")

    def test_a_committed_manifest_missing_from_the_worktree_reads_from_head(
        self,
    ) -> None:
        self._repaired()
        fixture.remove_worktree_manifest(self.fixture)

        # HEAD is authoritative: a missing worktree copy is not a missing
        # manifest, and the committed entries still resolve.
        manifest_state = fixture.read_manifest(self.fixture)
        self.assertEqual(manifest_state["state"], "ok")
        self.assertFalse(manifest_state["worktree_present"])
        self.assertIsNotNone(manifest_state["manifest"])
        self.assertEqual(
            [entry["path"] for entry in manifest_state["manifest"]["evidence"]],
            [fixture.EVIDENCE_PATH],
        )

        report = fixture.audit(self.fixture)
        evidence_check = next(
            c for c in report["checks"] if c["id"] == "evidence-at-head"
        )
        self.assertEqual(evidence_check["result"], "passed")
        # The deleted worktree file is a tracked-tree issue, not an evidence
        # read failure; the check must not claim the manifest is unreadable.
        self.assertNotIn(
            "evidence-manifest-unreadable",
            {blocker["id"] for blocker in report["blockers"]},
        )

    def test_unrelated_committed_support_is_not_claim_support(self) -> None:
        self._repaired()
        unrelated = self.fixture.root / fixture.UNRELATED_SUPPORT_PATH
        # The file is committed at HEAD with a matching hash, but its content
        # says nothing about the accepted claim.
        self.assertNotIn(fixture.GONE_CLAIM_ID.encode(), unrelated.read_bytes())

        variants = (
            (
                "declared-supports-claim",
                {
                    "path": fixture.UNRELATED_SUPPORT_PATH,
                    "sha256": fixture.sha256_path(unrelated),
                    "supports_claim": fixture.GONE_CLAIM_ID,
                },
            ),
            (
                "no-declaration",
                {
                    "path": fixture.UNRELATED_SUPPORT_PATH,
                    "sha256": fixture.sha256_path(unrelated),
                },
            ),
        )
        for label, reference in variants:
            with self.subTest(label=label):
                fixture.inject_missing_evidence_artifact(
                    self.fixture, independent_support=[reference]
                )

                claims = fixture.claim_support_state(self.fixture)
                claim = claims["claims"][0]
                self.assertEqual(claim["committed_support"], [], claim)
                self.assertEqual(claim["resolved_evidence"], [], claim)
                self.assertTrue(claims["sole_claim_basis"], claim)

                report = fixture.audit(self.fixture)
                self._assert_sole_basis_blocker(report)
                self.assertFalse(report["handoff_ready"])
                self.assertEqual(report["next_action"], "stop")
                blocker = next(
                    b
                    for b in report["blockers"]
                    if b["id"] == "evidence-sole-claim-basis"
                )
                self.assertEqual(blocker["repair"]["action"], "operator-decision")

    def test_a_support_declaration_for_a_different_claim_does_not_count(self) -> None:
        self._repaired()
        support = self.fixture.root / fixture.SUPPORT_PATH
        self.assertIn(fixture.GONE_CLAIM_ID.encode(), support.read_bytes())

        fixture.inject_missing_evidence_artifact(
            self.fixture,
            independent_support=[
                {
                    "path": fixture.SUPPORT_PATH,
                    "sha256": fixture.sha256_path(support),
                    "supports_claim": "some-other-claim",
                }
            ],
        )

        claims = fixture.claim_support_state(self.fixture)
        self.assertEqual(claims["claims"][0]["committed_support"], [])
        self.assertTrue(claims["sole_claim_basis"])
        self._assert_sole_basis_blocker(fixture.audit(self.fixture))

    def test_a_gone_artifact_is_recorded_unqualified_not_claimed_repaired(self) -> None:
        self._repaired()
        blocker = {
            "id": "evidence-retention-gap",
            "severity": "blocking",
            "location": fixture.GONE_EVIDENCE_PATH,
            "evidence": "artifact gone",
            "repair": {
                "action": "repair-evidence",
                "manifest": fixture.MANIFEST_PATH,
                "path": fixture.GONE_EVIDENCE_PATH,
                "supersede_path": fixture.EVIDENCE_SUPERSEDE_PATH,
                "pathspecs": [fixture.EVIDENCE_SUPERSEDE_PATH],
                "operator_decision": "none",
            },
        }
        repair = fixture.apply_blockers(self.fixture, [blocker])
        self.assertEqual(repair["status"], "partial")
        self.assertEqual(repair["remaining"], ["evidence-retention-gap"])
        self.assertEqual(repair["unqualified"][0]["disposition"], "unqualified")
        self.assertFalse(repair["unqualified"][0]["verified"])
        self.assertFalse(
            (self.fixture.root / fixture.EVIDENCE_SUPERSEDE_PATH).exists(),
            "the writer must not fabricate evidence for a missing artifact",
        )

    # --- 1.4 / 8.6: human gates ----------------------------------------

    def test_human_gates_resolve_from_frontmatter_then_explicit_input(self) -> None:
        plan = fixture.plan_text(self.fixture)
        plan_changes = fixture.GATED_PLAN_CHANGES

        from_plan = fixture.resolve_human_gates(plan_changes, plan=plan)
        self.assertTrue(from_plan["ok"])
        self.assertEqual(from_plan["gates"], (fixture.GATED_CHANGE,))
        self.assertEqual(from_plan["source"], "plan")

        explicit = fixture.resolve_human_gates(
            plan_changes, explicit=(fixture.NEXT_CHANGE,), plan=plan
        )
        self.assertEqual(explicit["gates"], (fixture.NEXT_CHANGE,))
        self.assertEqual(explicit["source"], "invocation")
        self.assertNotIn(fixture.GATED_CHANGE, explicit["gates"])

        cleared = fixture.resolve_human_gates(
            plan_changes, explicit="none", plan=plan
        )
        self.assertEqual(cleared["gates"], ())
        self.assertEqual(cleared["source"], "invocation")
        self.assertEqual(cleared["recorded"], "none")

        empty = fixture.resolve_human_gates(plan_changes, plan="")
        self.assertEqual(empty["gates"], ())
        self.assertEqual(empty["source"], "default")

    def test_unknown_human_gate_slug_fails_preflight(self) -> None:
        resolution = fixture.resolve_human_gates(
            fixture.GATED_PLAN_CHANGES,
            explicit="not-in-the-plan",
            plan=fixture.plan_text(self.fixture),
        )
        self.assertFalse(resolution["ok"])
        self.assertEqual(resolution["gates"], ())
        self.assertIn("not-in-the-plan", resolution["error"])

        stop = fixture.human_gate_stop(
            resolution, fixture.GATED_PLAN_CHANGES, fixture.CHANGE
        )
        self.assertTrue(stop["stop"])
        self.assertEqual(stop["kind"], "preflight-error")

    def test_a_gated_change_is_not_started(self) -> None:
        fresh = self._repaired()
        plan_changes = fixture.GATED_PLAN_CHANGES
        resolution = fixture.resolve_human_gates(
            plan_changes, plan=fixture.plan_text(self.fixture)
        )

        stop = fixture.human_gate_stop(resolution, plan_changes, fixture.NEXT_CHANGE)
        self.assertTrue(stop["stop"])
        self.assertEqual(stop["kind"], "human-gate")
        self.assertEqual(stop["gated_change"], fixture.GATED_CHANGE)
        self.assertEqual(stop["runnable"], ())
        self.assertIn(fixture.GATED_CHANGE, stop["reason"])

        self.assertIsNone(
            fixture.next_change(
                fresh,
                plan_changes,
                fixture.NEXT_CHANGE,
                human_gates=resolution["gates"],
            ),
            "a gated change is never started",
        )
        self.assertEqual(
            fixture.next_change(fresh, plan_changes, fixture.NEXT_CHANGE),
            fixture.GATED_CHANGE,
        )

    def test_the_changes_before_the_gate_are_still_runnable(self) -> None:
        fresh = self._repaired()
        resolution = fixture.resolve_human_gates(
            fixture.GATED_PLAN_CHANGES, plan=fixture.plan_text(self.fixture)
        )
        stop = fixture.human_gate_stop(
            resolution, fixture.GATED_PLAN_CHANGES, fixture.CHANGE
        )
        self.assertTrue(stop["stop"])
        self.assertEqual(stop["gated_change"], fixture.GATED_CHANGE)
        self.assertEqual(stop["runnable"], (fixture.NEXT_CHANGE,))
        self.assertEqual(
            fixture.next_change(
                fresh,
                fixture.GATED_PLAN_CHANGES,
                fixture.CHANGE,
                human_gates=resolution["gates"],
            ),
            fixture.NEXT_CHANGE,
        )

    # --- 1.5 / 1.7 / 8.7: caps are advisory ----------------------------

    def test_cap_alone_does_not_produce_a_stop(self) -> None:
        for remaining in ("one scoped evidence repair and a re-audit", ""):
            with self.subTest(remaining=remaining or "<none>"):
                decision = fixture.cap_decision(
                    cap_reached=True, remaining_work=remaining
                )
                self.assertNotEqual(decision["decision"], "stop")
                self.assertFalse(decision["cap_used_as_reason"])
        self.assertEqual(
            fixture.cap_decision(
                cap_reached=True, remaining_work=""
            )["decision"],
            "complete",
        )
        unreached = fixture.cap_decision(
            cap_reached=False, remaining_work="a scoped repair remains"
        )
        self.assertEqual(unreached["decision"], "continue")
        self.assertFalse(unreached["override_required"])

        # Only a human-only decision stops, and the cap is never the reason.
        human_only = fixture.cap_decision(
            cap_reached=True,
            remaining_work="a scoped repair remains",
            human_only_decision="the operator must clear the archive-only waiver",
        )
        self.assertEqual(human_only["decision"], "stop")
        self.assertFalse(human_only["cap_used_as_reason"])
        self.assertIn("waiver", human_only["reason"])

        # An override without a justification is not a valid ledger record.
        invalid = fixture.cap_override(
            cap="repair/audit rounds",
            cap_value="two repair/audit rounds",
            consumed=2,
            remaining_work="a scoped repair",
            justification="",
            checkpoint="the next audit",
        )
        self.assertFalse(invalid["valid"])
        pending = fixture.cap_decision(
            cap_reached=True,
            remaining_work="a scoped repair",
            override=invalid,
        )
        self.assertEqual(pending["decision"], "continue")
        self.assertTrue(pending["override_required"])

    def test_cap_override_permits_a_third_repair_round(self) -> None:
        cap = 2
        rounds: list[dict] = []
        overrides: list[dict] = []
        injections = {
            2: fixture.inject_stale_evidence_checksum,
            3: fixture.inject_regressed_perf_script,
        }

        report = fixture.audit(self.fixture)
        self.assertEqual(fixture.gate_decision(report), "repair")

        for round_no in (1, 2, 3, 4):
            inject = injections.get(round_no)
            if inject is not None:
                inject(self.fixture)
                report = fixture.audit(self.fixture)
            if fixture.gate_decision(report) != "repair":
                break
            if len(rounds) >= cap:
                override = fixture.cap_override(
                    cap="repair/audit rounds",
                    cap_value="two repair/audit rounds",
                    consumed=len(rounds),
                    remaining_work=(
                        "a scoped "
                        + report["blockers"][0]["id"]
                        + " repair plus one fresh audit"
                    ),
                    justification=(
                        "the remaining repair is scoped and safe; stopping "
                        "would leave accepted work unqualified"
                    ),
                    checkpoint="the fresh audit that follows this round",
                )
                self.assertTrue(override["valid"])
                decision = fixture.cap_decision(
                    cap_reached=True,
                    remaining_work=override["remaining_work"],
                    override=override,
                )
                self.assertEqual(decision["decision"], "continue")
                self.assertFalse(decision["cap_used_as_reason"])
                self.assertFalse(decision["override_required"])
                overrides.append(override)

            repair = fixture.apply_blockers(self.fixture, report["blockers"])
            self.assertEqual(repair["status"], "repaired", repair.get("reason"))
            self.assertTrue(repair["commit"])
            rounds.append(repair)
            report = fixture.audit(self.fixture)

        self.assertEqual(
            len(rounds), 3, "the third repair round must be permitted"
        )
        self.assertEqual(
            [phase["phase"] for phase in rounds[0]["phases"]], ["baseline"]
        )
        self.assertEqual(
            [phase["phase"] for phase in rounds[1]["phases"]], ["evidence"]
        )
        self.assertEqual(
            [phase["phase"] for phase in rounds[2]["phases"]], ["baseline"]
        )
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0]["consumed"], 2)
        self.assertEqual(overrides[0]["event"], "cap_override")
        self.assertTrue(report["handoff_ready"], report["blockers"])
        self.assertEqual(fixture.gate_decision(report), "continue")

    # --- end-to-end -----------------------------------------------------

    def test_fixture_runs_end_to_end_from_blocked_to_handoff_ready(self) -> None:
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
                "carry_forwards",
                "informational",
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
