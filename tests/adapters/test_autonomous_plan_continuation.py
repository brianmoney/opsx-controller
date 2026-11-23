"""Contract tests for autonomous plan continuation.

These are static contract checks over the shipped agent surfaces. A prompt
surface is the product here, so a missing paragraph is a missing feature: each
test reads the real file and asserts one claim from
``openspec/changes/add-autonomous-plan-continuation``.

Four surfaces are covered:

* ``opsx-orchestrator`` — scoped autonomy, ``HUMAN_GATES`` resolution, local-only
  delivery, advisory caps, carry-forward, and the human-gate stop.
* ``opsx-integration-auditor`` — no next-proposal or upstream requirement, every
  hard check retained, and ``stop`` reserved for human-only boundaries.
* ``opsx-repo-hygiene`` — phased repair, evidence supersession, original
  preservation, and own-commit-only amendment.
* ``opsx-review``, ``opsx-change-writer``, and ``opsx-implementer`` (plan mode) —
  prior archived findings carried forward instead of reopened.

Assertions match against a whitespace-flexed copy of the text with markdown
emphasis markers removed, so a reflow or a bold/backtick change does not break
the suite while a removed or weakened claim does.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

_ORCHESTRATOR = _REPO / "adapters" / "opencode" / "agents" / "opsx-orchestrator.md"
_AUDITOR = _REPO / "adapters" / "opencode" / "agents" / "opsx-integration-auditor.md"
_HYGIENE = _REPO / "adapters" / "opencode" / "agents" / "opsx-repo-hygiene.md"
_REVIEW = _REPO / "adapters" / "opencode" / "agents" / "opsx-review.md"
_CHANGE_WRITER = _REPO / "adapters" / "opencode" / "agents" / "opsx-change-writer.md"
_IMPLEMENTER_BODY = _REPO / "core" / "phase-agents" / "implementer.md"

#: The audit checks that must survive this change. ``next-change-intake`` and
#: ``branch-upstream`` are retired: an unauthored next change is expected after
#: handoff, and delivery is local.
_HARD_AUDIT_CHECKS = (
    "tracked-tree-clean",
    "source-manifest-at-head",
    "plan-at-head",
    "archive-commit-implementation-paths",
    "run-state-ignored",
    "perf-script-from-head",
    "fresh-checkout-checks",
)

_RETIRED_AUDIT_CHECKS = ("next-change-intake", "branch-upstream")

#: Reviewer coverage case classes that carry-forward classification must not
#: weaken.
_REVIEW_CASE_CLASSES = (
    "Expected read errors",
    "Invalid parameters and cache-key collisions",
    "Maximum size",
    "Permission and malformed state",
    "Concurrency and no-write invariants",
    "Next-phase compatibility",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Collapse markdown wrapping and drop emphasis markers.

    ``**bold**`` and ```code``` spans wrap a single phrase across lines, so
    stripping the markers before collapsing whitespace keeps phrase assertions
    semantic rather than typographic.
    """

    return " ".join(text.replace("**", "").replace("`", "").split())


def _section(text: str, heading: str, level: int = 2) -> str:
    """Return the flattened body of one markdown section.

    Stops at the next heading of the same or a higher level so a phrase asserted
    against a section cannot be satisfied by an unrelated part of the document.
    """

    marker = "#" * level + " " + heading
    if marker not in text:
        raise AssertionError(f"missing markdown section: {marker!r}")
    rest = text.split(marker, 1)[1]
    stop = re.search(r"\n#{1,%d} " % level, rest)
    body = rest[: stop.start()] if stop else rest
    return _flat(body)


def _check_ids(text: str) -> list[str]:
    """Return the ``### N. `<check-id>` `` audit check ids in document order."""

    return re.findall(r"^###\s+\d+\.\s+`([a-z0-9-]+)`", text, re.MULTILINE)


def _check_body(text: str, number: int, check_id: str) -> str:
    """Return the flattened body of one numbered audit check."""

    pattern = re.compile(
        rf"^### {number}\. `{re.escape(check_id)}`\s*$", re.MULTILINE
    )
    match = pattern.search(text)
    if match is None:
        raise AssertionError(f"missing audit check heading: {check_id!r}")
    rest = text[match.end() :]
    stop = re.search(r"^#{2,3} ", rest, re.MULTILINE)
    return _flat(rest[: stop.start()] if stop else rest)


def _sentence_containing(flat_text: str, needle: str) -> str:
    """Return the sentence-sized fragment of ``flat_text`` containing ``needle``."""

    for fragment in re.split(r"(?<=[.;])\s+", flat_text):
        if needle in fragment:
            return fragment
    raise AssertionError(f"missing phrase: {needle!r}")




class OrchestratorAutonomyContractTests(unittest.TestCase):
    """`opsx-orchestrator` autonomy is scoped, local, capped, and gate-bounded."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _text(_ORCHESTRATOR)
        cls.flat = _flat(cls.text)

    # --- 8.1: HUMAN_GATES resolution ------------------------------------

    def test_human_gates_is_an_input_with_explicit_plan_empty_precedence(
        self,
    ) -> None:
        self.assertIn("HUMAN_GATES (optional)", self.flat)
        self.assertIn(
            "Resolve it in this order: the explicit invocation value, then the "
            "plan frontmatter's human_gates list, then the empty list.",
            self.flat,
        )
        # The same order is restated for preflight, before any writer runs.
        self.assertIn(
            "Resolve the human-gate set before dispatching any writer: explicit "
            "HUMAN_GATES, then the plan's human_gates, then empty.",
            self.flat,
        )

    def test_explicit_human_gates_replaces_the_plan_and_unknown_slug_fails_closed(
        self,
    ) -> None:
        self.assertIn(
            "An explicit value replaces the frontmatter list; it is never "
            "merged with it.",
            self.flat,
        )
        self.assertIn(
            "Every resolved entry must be an exact change slug present in the "
            "plan; an unknown slug is a fail-closed preflight error, not a "
            "silently ignored entry.",
            self.flat,
        )
        self.assertIn(
            "A resolved entry that is not an exact plan change slug is a "
            "fail-closed preflight error: stop before dispatching any worker "
            "and report the unknown slug.",
            self.flat,
        )
        self.assertIn(
            "HUMAN_GATES: none clears the plan's gates for this run and is "
            "recorded verbatim in the ledger as an operator override.",
            self.flat,
        )

    # --- 8.1: autonomy scope -------------------------------------------

    def test_autonomy_scope_section_separates_owned_from_human_gated(self) -> None:
        self.assertIn("## Autonomy scope", self.text)
        section = _section(self.text, "Autonomy scope")
        self.assertIn(
            "An orchestrator-owned change is in the plan and is not in the "
            "resolved human-gate set.",
            section,
        )
        self.assertIn(
            "For it you have standing authority to author, implement, review, "
            "repair, commit locally, amend the commits you own, hand off, and "
            "continue without asking the operator.",
            section,
        )
        self.assertIn(
            "A human-gated change is in the resolved human-gate set. It is a "
            "run boundary.",
            section,
        )
        self.assertIn(
            "You may plan around it; you SHALL NOT author, implement, review, "
            "archive, or dispatch any work for it.",
            section,
        )

    def test_internal_decisions_are_made_without_asking(self) -> None:
        section = _section(self.text, "Autonomy scope")
        for decision in (
            "authoring or amending the change's artifacts, and whether the "
            "next change's authoring may start;",
            "commit boundaries, commit messages, and the accepted-pathspec "
            "scope;",
            "local amendments of a commit you or your worker created in the "
            "current round;",
            "exceeding an advisory cap under the cap_override rule.",
        ):
            with self.subTest(decision=decision):
                self.assertIn(decision, section)
        self.assertIn(
            "Never ask for an internal repository, evidence, commit, or intake "
            "decision.",
            section,
        )

    def test_human_gate_stop_names_the_resume_instruction(self) -> None:
        self.assertIn(
            "stop before it: dispatch no author, implementer, reviewer, or "
            "archiver for it",
            self.flat,
        )
        self.assertIn(
            "the gated slug, why it is gated, the state the run will start "
            "from, and the single operator action that clears it",
            self.flat,
        )
        self.assertIn(
            "Do not skip past a gated change to a later change that depends on "
            "it",
            self.flat,
        )
        self.assertIn(
            "A later change whose dependency is gated is reported as not "
            "started, not dispatched.",
            self.flat,
        )
        self.assertIn(
            "Record the resolved human-gate set with its source, every "
            "human-gate stop with the exact resume instruction",
            self.flat,
        )

    # --- 8.1: local-only delivery --------------------------------------

    def test_delivery_is_local_with_no_remote_operations(self) -> None:
        self.assertIn("### Local delivery contract", self.text)
        self.assertIn(
            "never git push, never create a remote branch, never open a pull "
            "request, never merge",
            self.flat,
        )
        self.assertIn(
            "never require a remote upstream and never ask for a no-push "
            "waiver",
            self.flat,
        )
        self.assertIn(
            "The archive commit plus the scoped handoff repairs are the "
            "delivery evidence.",
            self.flat,
        )
        self.assertIn(
            "A configured upstream, if one happens to exist, is informational "
            "only and changes nothing about the result.",
            self.flat,
        )

    def test_amendment_is_limited_to_a_commit_the_worker_owns(self) -> None:
        self.assertIn(
            "never amend a commit you or the worker do not own, and never "
            "amend a commit that has descendants",
            self.flat,
        )
        self.assertIn(
            "every commit and amendment is local and limited to a commit the "
            "same worker created in the current round while it is still HEAD.",
            self.flat,
        )

    def test_local_only_delivery_replaces_branch_upstream(self) -> None:
        # The retired branch/upstream handoff criterion must not come back.
        # The hyphenated form `local-only-delivery` is the auditor's check id
        # (asserted in AuditorAutonomyContractTests); the orchestrator's
        # handoff-ready criterion names the same posture in prose.
        self.assertNotIn(
            "branch-upstream",
            self.text,
            "the retired branch-upstream handoff criterion must stay gone",
        )
        self.assertIn("local-only delivery", self.flat)
        self.assertIn(
            "the work is committed on the local branch and contained in HEAD, "
            "no push or pull request is required",
            self.flat,
        )
        self.assertIn(
            "a configured upstream — if one happens to exist — is reported as "
            "informational and changes nothing",
            self.flat,
        )

    # --- 8.1: advisory caps -------------------------------------------

    def test_cap_override_names_cap_consumption_work_justification_and_checkpoint(
        self,
    ) -> None:
        self.assertIn("## Advisory caps", self.text)
        section = _section(self.text, "Advisory caps")
        self.assertIn(
            "MAX_DISPATCHES, the two corrective rounds, and the two "
            "repair/audit rounds are advisory thresholds, not terminal "
            "conditions.",
            section,
        )
        for field in (
            "the cap and its value/source (invocation, plan metadata, or "
            "contract default);",
            "the count already consumed;",
            "the concrete remaining work that justifies continuing;",
            "why continuing is safer than stopping;",
            "the next checkpoint at which you re-evaluate the decision.",
        ):
            with self.subTest(field=field):
                self.assertIn(field, section)

    def test_a_cap_alone_is_never_a_stop_reason(self) -> None:
        section = _section(self.text, "Advisory caps")
        self.assertIn("Reaching a cap is never by itself a stop reason.", section)
        self.assertIn(
            "When a cap is reached and the only remaining action needs a "
            "human-only decision, stop and report that decision, not the cap.",
            section,
        )

    def test_three_dispatch_reserve_is_a_planning_allowance_not_terminal(self) -> None:
        self.assertIn("### Handoff reserve", self.text)
        section = _section(self.text, "Handoff reserve", level=3)
        self.assertIn("Reserve at least three dispatches per change", section)
        self.assertIn(
            "The reserve is a planning allowance, not a gate on arithmetic.",
            section,
        )
        self.assertIn(
            "do not stop solely because the reserve cannot be honored",
            section,
        )
        self.assertIn(
            "The reserve is excluded from MAX_DISPATCHES and from the "
            "review/implementation cap",
            section,
        )

    def test_canonical_stop_list_forbids_stopping_for_caps_or_the_reserve(
        self,
    ) -> None:
        self.assertIn("## Stopping conditions (canonical stop list)", self.text)
        section = _section(self.text, "Stopping conditions (canonical stop list)")
        self.assertIn(
            "Everything not on this list continues; a reached cap, a reserve "
            "shortfall, or a dispatch count is not on it.",
            section,
        )
        self.assertIn(
            "Never stop for: a dispatch, corrective-round, or repair-round "
            "cap; the three-dispatch handoff reserve;",
            section,
        )
        self.assertIn(
            "Everything not on that list continues — including a reached cap "
            "or a handoff-reserve shortfall.",
            self.flat,
        )

    # --- 8.1: carry-forward -------------------------------------------

    def test_carry_forward_records_the_origin_current_locus_impact_and_disposition(
        self,
    ) -> None:
        self.assertIn("## Post-archive carry-forward", self.text)
        section = _section(self.text, "Post-archive carry-forward")
        for field in (
            "the originating archived change and the current change;",
            "the locus (path[:symbol]);",
            "observed behavior and required behavior, with evidence;",
            "the impact, and whether it affects a later planned change "
            "(blocks-later-change: yes or no);",
            "the disposition: follow-up, carry-forward, or "
            "blocks-later-change.",
        ):
            with self.subTest(field=field):
                self.assertIn(field, section)

    def test_carry_forward_never_reopens_the_archive_or_inserts_a_change(
        self,
    ) -> None:
        section = _section(self.text, "Post-archive carry-forward")
        self.assertIn(
            "do not reopen the archived change, edit its archived delta, or "
            "re-run its review as if it were active;",
            section,
        )
        self.assertIn(
            "do not insert an unplanned follow-up change into the running "
            "plan",
            section,
        )
        self.assertIn(
            "a defect newly exposed or newly worsened by the active change's "
            "diff belongs to the active change, with the archived origin named",
            section,
        )

    # --- 8.1: the next proposal is not a handoff requirement -----------

    def test_handoff_criteria_do_not_require_the_next_changes_proposal(self) -> None:
        self.assertIn("### Handoff-ready criteria", self.text)
        criteria = _section(self.text, "Handoff-ready criteria", level=3)
        self.assertIn(
            "the next change's planned slug is free, already holds that same "
            "planned change as the active in-progress change, or carries only an "
            "archived directory — a different change occupying the slug is a "
            "collision: a plan-reachable occupant is an orchestrator-owned "
            "repair, while an out-of-plan occupant is a human-only stop with an "
            "operator decision to resolve or rename the external change; and "
            "every dependency of the next change is archived or recorded as an "
            "accepted deferral",
            criteria,
        )
        # The only mention of the next change's artifacts is the negation that
        # they are not required at HEAD before its authoring stage runs.
        mentions = re.findall(r"[^.;]*proposal\.md[^.;]*", criteria)
        self.assertEqual(
            len(mentions),
            1,
            f"handoff criteria must mention the next proposal only to negate "
            f"it: {mentions}",
        )
        self.assertIn("are not required at HEAD", mentions[0])
        self.assertIn(
            "are not required at HEAD",
            _sentence_containing(criteria, "proposal.md"),
        )
        self.assertIn(
            "the next change's own proposal.md, design, tasks, and spec deltas "
            "are not required at HEAD, because its authoring stage runs only "
            "after this gate releases it",
            criteria,
        )
        # Definition of done is the durable completion list; it must not add
        # the next change's authored artifacts either.
        done = _section(self.text, "Definition of done")
        self.assertNotIn(
            "proposal",
            done,
            "definition of done must not require the next change's proposal",
        )

    # --- 8.5: repair is split by the blocker's owner --------------------

    def test_repair_step_splits_the_blockers_by_repair_owner(self) -> None:
        sequence = _section(self.text, "Gate sequence", level=3)
        self.assertIn(
            "split the blockers by repair.owner",
            sequence,
        )

    def test_repair_dispatches_hygiene_only_with_hygiene_owned_blockers(self) -> None:
        sequence = _section(self.text, "Gate sequence", level=3)
        for phrase in (
            "Dispatch opsx-repo-hygiene only with the repository repairs — the "
            "exact blocker list whose repair.owner is opsx-repo-hygiene — and "
            "the round number.",
            "every blocker is a machine-actionable repair or a "
            "plan-reachable slug collision",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, sequence)

    def test_slug_collision_is_resolved_by_the_orchestrator_not_the_writer(
        self,
    ) -> None:
        sequence = _section(self.text, "Gate sequence", level=3)
        for phrase in (
            "A blocker whose repair.action is resolve-slug-collision is yours, "
            "not the writer's: resolve the plan-level conflict yourself, "
            "without dispatching opsx-repo-hygiene, by not starting or "
            "authoring the colliding change",
            "recording which artifact is authoritative",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, sequence)

    def test_a_plan_reachable_collision_is_not_a_stop(self) -> None:
        sequence = _section(self.text, "Gate sequence", level=3)
        self.assertIn(
            "never treat a plan-reachable slug collision as a stop or as a "
            "hygiene dispatch",
            sequence,
        )

    def test_an_out_of_plan_collision_is_a_stop(self) -> None:
        sequence = _section(self.text, "Gate sequence", level=3)
        self.assertIn(
            "An out-of-plan occupant is different: it is a human-only boundary, "
            "so return next_action: stop with the operator decision to resolve or "
            "rename that external change; you do not archive, rename, or rewrite "
            "it.",
            sequence,
        )

    def test_a_slug_collision_is_never_forwarded_to_the_writer(self) -> None:
        sequence = _section(self.text, "Gate sequence", level=3)
        self.assertIn(
            "Never ask the writer to rename, move, archive, or delete an active "
            "change directory",
            sequence,
        )

    def test_collision_resolution_edits_nothing_and_terminates(self) -> None:
        sequence = _section(self.text, "Gate sequence", level=3)
        # The resolution is mutation-free: no plan edit, no change-directory
        # surgery, and no operator question.
        self.assertNotIn("plan edit", sequence.lower())
        self.assertNotIn("plan-slug correction", sequence.lower())
        self.assertNotIn("edit the plan", sequence.lower())
        self.assertIn("you make no repository mutation to clear a slug", sequence)
        self.assertIn("you do not ask the operator", sequence)
        self.assertNotIn("plan edit", self.flat.lower())
        self.assertNotIn("plan-slug correction", self.flat.lower())

        # The pass condition is stated once and is reachable.
        resolution = _section(self.text, "Slug-collision resolution", level=3)
        self.assertIn("This is not a collision.", resolution)
        self.assertIn("no mutation and no new start are required", resolution)
        self.assertIn(
            "Resolve it by not starting or authoring the colliding change and "
            "continuing the pipeline",
            resolution,
        )
        self.assertIn(
            "the plan drives that occupying change to archive through its normal "
            "flow, which frees the slug, and the next audit of this same handoff "
            "then passes",
            resolution,
        )
        self.assertIn(
            "No plan file, change directory, or repository content is edited to "
            "clear the slug in any case.",
            resolution,
        )
        self.assertIn(
            "For a plan-reachable collision the check transitions from failed to "
            "passed",
            resolution,
        )
        self.assertIn(
            "An out-of-plan change occupies the slug. The active directory "
            "declares a change id that is not in the plan and is not a declared "
            "dependency. This is a human-only boundary",
            resolution,
        )
        self.assertIn(
            "the operator must resolve or rename that external change",
            resolution,
        )
        self.assertIn(
            "You SHALL NOT archive, rename, move, rewrite, or otherwise mutate an "
            "out-of-plan change",
            resolution,
        )


class AuditorAutonomyContractTests(unittest.TestCase):
    """`opsx-integration-auditor` keeps every hard check and narrows stop."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _text(_AUDITOR)
        cls.flat = _flat(cls.text)
        cls.ids = _check_ids(cls.text)

    # --- 8.2: intake replaced by a narrow readiness check ---------------

    def test_next_change_intake_is_retired_and_readiness_is_narrowed(self) -> None:
        self.assertTrue(self.ids, "the audit check headings were not found")
        self.assertNotIn(
            "next-change-intake",
            self.text,
            "the retired next-change-intake check must stay gone",
        )
        self.assertIn("next-change-readiness", self.ids)
        self.assertIn(
            "this check SHALL NOT require proposal.md, design.md, tasks.md, or "
            "spec deltas for NEXT_CHANGE to exist at HEAD, and their absence is "
            "never a blocker, never unqualified, and never a stop.",
            self.flat,
        )
        self.assertIn("An unauthored next change is passed.", self.flat)
        self.assertIn("Planned-slug collision", self.flat)
        self.assertIn("Dependency readiness", self.flat)

    def test_readiness_check_is_limited_to_collision_and_dependencies(self) -> None:
        body = _check_body(self.text, 8, "next-change-readiness")
        # The planned change already being active is a pass; only a different
        # change occupying the slug is a collision.
        self.assertIn("The planned change is already active", body)
        self.assertIn(
            "declares <NEXT_CHANGE> as its own change id, so the next change is "
            "already authored and in progress",
            body,
        )
        self.assertIn(
            "Planned-slug collision: a different change occupies the slug", body
        )
        self.assertIn(
            "declares a change id other than <NEXT_CHANGE>, so a different change "
            "occupies the planned slug",
            body,
        )
        self.assertIn(
            "every change NEXT_CHANGE depends on, as named by the plan, is "
            "archived under openspec/changes/archive/",
            body,
        )
        # The next proposal may be mentioned only to read its declaration or to
        # negate it as a requirement.
        self.assertEqual(
            body.count("proposal.md"),
            1,
            "the readiness check may mention the next proposal only to inspect "
            "its declaration or to negate it as a requirement",
        )
        self.assertIn(
            "A directory under openspec/changes/archive/ with the same slug is "
            "reported as informational context, not a blocker.",
            body,
        )
        # The plan-reachable failed-to-passed transition is stated and needs no
        # mutation; the out-of-plan branch is an operator stop.
        self.assertIn(
            "A plan-reachable collision clears only when the occupying directory "
            "leaves openspec/changes/<NEXT_CHANGE>/ through the normal pipeline "
            "reaching that change's own archive, which frees the slug",
            body,
        )
        self.assertIn(
            "An out-of-plan collision clears only when the operator resolves or "
            "renames the external change",
            body,
        )
        self.assertIn(
            "the auditor does not authorize archiving, renaming, moving, or "
            "rewriting it",
            body,
        )
        self.assertIn(
            "it edits no plan file, change directory, or repository content",
            body,
        )

    def test_local_only_delivery_replaces_branch_upstream_with_no_waiver(self) -> None:
        self.assertIn("local-only-delivery", self.ids)
        for retired in _RETIRED_AUDIT_CHECKS:
            with self.subTest(retired=retired):
                self.assertNotIn(retired, self.text)
                self.assertNotIn(retired, self.ids)
        self.assertIn(
            "There is no no-push waiver in this contract: do not request one, "
            "do not look for one, and do not report one as missing.",
            self.flat,
        )
        self.assertIn(
            "A missing upstream is never failed, never unqualified, and never a "
            "reason for next_action: stop.",
            self.flat,
        )

    # --- 8.2: every hard check survives --------------------------------

    def test_every_hard_audit_check_is_still_a_numbered_check(self) -> None:
        for check in _HARD_AUDIT_CHECKS + (
            "evidence-at-head",
            "next-change-readiness",
            "local-only-delivery",
        ):
            with self.subTest(check=check):
                self.assertIn(check, self.ids)

    def test_hard_check_paragraph_still_names_the_full_set(self) -> None:
        expected = (
            "Hard checks are never relaxed by a carry-forward: "
            + ", ".join(_HARD_AUDIT_CHECKS[:-1])
            + ", and "
            + _HARD_AUDIT_CHECKS[-1]
            + "."
        )
        self.assertIn(expected, self.flat)
        self.assertIn(
            "Any of them failed or unqualified is handoff_ready: false.",
            self.flat,
        )

    # --- 8.2: stop is reserved for human-only boundaries ---------------

    def test_stop_is_reserved_for_human_only_boundaries(self) -> None:
        self.assertIn(
            "### `next_action: stop` is reserved for human-only boundaries",
            self.text,
        )
        section = _section(
            self.text, "`next_action: stop` is reserved for human-only boundaries", 3
        )
        self.assertIn(
            "Use next_action: stop and repair.action: operator-decision only "
            "when the blocker needs a decision no worker is authorized to make:",
            section,
        )
        for boundary in (
            "the next change is in the resolved human-gate set;",
            "a safety, credential, secret, or trust exposure;",
            "a privacy decision;",
            "an operator authority restriction, such as an archive-only waiver;",
            "missing evidence that is the sole basis of an accepted claim the "
            "operator must adjudicate;",
            "a plan/artifact contradiction no worker may resolve.",
        ):
            with self.subTest(boundary=boundary):
                self.assertIn(boundary, section)

    def test_stop_is_forbidden_for_internal_repository_conditions(self) -> None:
        section = _section(
            self.text, "`next_action: stop` is reserved for human-only boundaries", 3
        )
        self.assertIn(
            "You SHALL NOT return stop, and SHALL NOT request an operator "
            "decision, for:",
            section,
        )
        for condition in (
            "an unauthored next change — its proposal not existing yet is "
            "expected;",
            "a missing remote upstream, a push, or a pull request;",
            "run-state ignore work such as the .opsx-run/ .gitignore entry;",
            "a perf/evidence script default path;",
            "a dispatch, review, or repair-round cap;",
            "an uncommitted accepted source baseline.",
        ):
            with self.subTest(condition=condition):
                self.assertIn(condition, section)
        self.assertIn(
            "Those return next_action: repair with an explicit action, or an "
            "unqualified carry-forward when no scoped repair exists.",
            section,
        )
        self.assertIn(
            "A cap is never a stop reason: report the concrete remaining work "
            "instead.",
            section,
        )

    # --- 8.2: repair-evidence and carry-forward exist -------------------

    def test_repair_evidence_action_is_documented(self) -> None:
        self.assertIn(
            "repair-evidence with manifest_reference, retained_artifact, and "
            "checksum;",
            self.flat,
        )
        body = _check_body(self.text, 6, "evidence-at-head")
        self.assertIn("repair.action: repair-evidence", body)
        self.assertIn(
            "A scoped writer may add evidence, append a superseding entry, or "
            "update a checksum; it preserves the original bytes unchanged.",
            body,
        )
        self.assertIn(
            "unqualified with a carry_forward record",
            body,
        )

    def test_carry_forward_is_part_of_the_output_contract(self) -> None:
        self.assertIn('"carry_forward"', self.text)
        self.assertIn('"disposition": "unqualified"', self.text)
        self.assertIn(
            "A populated carry_forward requires repair.action: \"none\" and "
            "severity: \"warning\"",
            self.flat,
        )
        # The carry-forward rule relaxes only the evidence check; the hard
        # checks stay hard.
        self.assertIn(
            "an unqualified check is handoff_ready: false, except "
            "evidence-at-head under the carry-forward rule below.",
            self.flat,
        )
        self.assertIn(
            "An unqualified evidence-at-head check MAY coexist with "
            "handoff_ready: true when both hold:",
            self.flat,
        )
        self.assertIn(
            "a recorded carry_forward is the one case where an unqualified "
            "evidence check can still coexist with handoff_ready: true",
            self.flat.lower(),
        )

    def test_stop_vocabulary_is_unchanged(self) -> None:
        self.assertIn('"next_action": "continue|repair|stop"', self.text)
        self.assertIn(
            "next_action is continue when ready, repair when a scoped repair "
            "can clear every blocker, and stop only for a true human-only "
            "boundary as listed in the stop reservation.",
            self.flat,
        )


class HygienePhaseContractTests(unittest.TestCase):
    """`opsx-repo-hygiene` repairs in phases and amends only its own HEAD."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _text(_HYGIENE)
        cls.flat = _flat(cls.text)

    # --- 8.3: phase ordering ------------------------------------------

    def test_phase_a_safe_baseline_precedes_phase_b_evidence(self) -> None:
        self.assertIn("## Repair in phases", self.text)
        self.assertIn(
            "Repair happens in two ordered phases. One phase's blocker never "
            "withholds the other phase's commit.",
            self.flat,
        )
        self.assertIn("### Phase A — safe baseline", self.text)
        self.assertIn("### Phase B — evidence", self.text)
        self.assertLess(
            self.text.index("### Phase A — safe baseline"),
            self.text.index("### Phase B — evidence"),
            "Phase A must be documented before Phase B",
        )
        self.assertIn("Phase B runs after Phase A is committed", self.flat)
        phase_a = _section(self.text, "Phase A — safe baseline", level=3)
        self.assertIn(
            "the accepted implementation files the auditor named", phase_a
        )
        self.assertIn(
            "the run-state ignore entry for .opsx-run/ in .gitignore", phase_a
        )
        self.assertIn(
            "any retarget-script repair of a perf/evidence script default path.",
            phase_a,
        )

    def test_repairable_evidence_blocker_cannot_hold_back_phase_a(self) -> None:
        phase_a = _section(self.text, "Phase A — safe baseline", level=3)
        self.assertIn(
            "Commit Phase A even while an evidence or process blocker is still "
            "open.",
            phase_a,
        )
        self.assertIn(
            "A repairable evidence, manifest, checksum, or script-path "
            "question SHALL NOT prevent the baseline commit",
            phase_a,
        )
        self.assertIn("Never let a Phase B blocker prevent this commit.", self.flat)
        self.assertIn("Never let a Phase B question stop a Phase A commit", self.flat)

    # --- 8.3: evidence authority --------------------------------------

    def test_evidence_may_be_added_superseded_or_repointed(self) -> None:
        self.assertIn("## Evidence authority (Phase B, bounded)", self.text)
        section = _section(self.text, "Evidence authority (Phase B, bounded)")
        for action in (
            "add a new evidence file that a named blocker calls for;",
            "append a superseding manifest entry, or add a new versioned file "
            "beside an existing one;",
            "update a checksum or hash record for an artifact it can read right "
            "now;",
            "record a blocker as unqualified with the dead manifest reference.",
        ):
            with self.subTest(action=action):
                self.assertIn(action, section)

    def test_originals_are_preserved_byte_for_byte(self) -> None:
        section = _section(self.text, "Evidence authority (Phase B, bounded)")
        self.assertIn(
            "preserve historical originals byte-for-byte. Superseding means a "
            "new entry or a new versioned file; it SHALL NOT overwrite, "
            "truncate, re-encode, or delete the retained original;",
            section,
        )
        self.assertIn(
            "read the artifact before writing any hash, size, timestamp, or "
            "command output into a manifest, and never fabricate one or carry "
            "one over from a summary or a previous report;",
            section,
        )

    def test_a_missing_artifact_is_never_claimed_verified(self) -> None:
        section = _section(self.text, "Evidence authority (Phase B, bounded)")
        self.assertIn(
            "name a missing artifact as missing. A dead manifest reference is "
            "reported as unqualified evidence, never as verified; no exit code, "
            "manifest line, or prior claim makes it verified.",
            section,
        )
        self.assertIn(
            "Never claim a missing artifact was verified, and never fabricate a "
            "hash, command output, size, or timestamp.",
            self.flat,
        )

    # --- 8.3: amendment authority -------------------------------------

    def test_amendment_requires_own_current_round_commit_still_at_head(self) -> None:
        self.assertIn("## Amendment authority", self.text)
        section = _section(self.text, "Amendment authority")
        self.assertIn("A commit is amendable only when both conditions hold:", section)
        self.assertIn(
            "this writer created it during the current repair round;", section
        )
        self.assertIn("it is still HEAD.", section)
        self.assertIn(
            "A Phase A commit that already has a Phase B descendant is not HEAD "
            "and therefore is not amendable.",
            section,
        )
        self.assertIn(
            "When either condition does not hold — the commit belongs to the "
            "archiver, another worker, an earlier change, or the operator, or it "
            "already has descendants — create a new commit instead.",
            section,
        )

    def test_history_rewrites_are_forbidden(self) -> None:
        self.assertIn(
            "Never rewrite history: no rebase, no reset (including reset "
            "--hard), no force-push, no branch surgery, no commit reordering.",
            self.flat,
        )
        self.assertIn(
            "Never amend a commit created by the archiver, another worker, an "
            "earlier change, or the operator, and never amend a commit that has "
            "descendants.",
            self.flat,
        )
        # History rewrite must remain forbidden even while a local amend is
        # permitted.
        self.assertIn(
            "The single exception is the amend in \"Amendment authority\" below.",
            self.flat,
        )

    # --- 8.3: scope, product behavior, report contract ------------------

    def test_explicit_pathspecs_and_no_invented_scope_are_still_required(self) -> None:
        self.assertIn("## Do not invent scope", self.text)
        self.assertIn(
            "Commit only paths the blocker list names explicitly, as a "
            "pathspecs array or a single named path.",
            self.flat,
        )
        self.assertIn(
            "If a blocker does not name an explicit scope, return blocked with "
            "reason no explicit implementation scope.",
            self.flat,
        )
        self.assertIn(
            "Never stage the whole worktree, git add -A, git add ., or a "
            "directory glob to make progress.",
            self.flat,
        )
        self.assertIn("git add -- <path> <path>", self.text)
        self.assertIn(
            "Never stage the whole worktree or a bare directory to satisfy a "
            "blocker.",
            self.flat,
        )

    def test_product_behavior_changes_and_review_closure_stay_forbidden(self) -> None:
        self.assertIn(
            "Never change product behavior, \"fix\" a failing test by editing "
            "product code, or close a code-review finding.",
            self.flat,
        )
        self.assertIn(
            "A handoff repair is never the place a review finding gets closed.",
            self.flat,
        )
        self.assertIn(
            "A code defect requires a reviewed change or an explicit re-review "
            "by the change owner; say so and return blocked.",
            self.flat,
        )

    def test_final_json_contract_names_phases_and_evidence_actions(self) -> None:
        for key in (
            '"phases"',
            '"phase_commits"',
            '"amended_commit"',
            '"evidence_actions"',
            '"unqualified"',
            '"remaining"',
        ):
            with self.subTest(key=key):
                self.assertIn(key, self.text)
        self.assertIn('"name": "safe baseline"', self.text)
        self.assertIn('"name": "evidence"', self.text)
        self.assertIn(
            '"action": "add-evidence|supersede-manifest|supersede-versioned-file'
            '|update-checksum|record-unqualified"',
            self.text,
        )
        self.assertIn('"original_preserved": true', self.text)
        self.assertIn(
            "unqualified lists every item still unverified after both phases; "
            "an empty list means none remain.",
            self.flat,
        )

    # --- 8.5: `resolve-slug-collision` is not a hygiene action ----------

    def test_resolve_slug_collision_is_not_a_repair_the_writer_performs(
        self,
    ) -> None:
        section = _section(
            self.text, "`resolve-slug-collision` is not a hygiene action", level=3
        )
        for phrase in (
            "is not a repair you perform",
            "It is orchestrator-owned",
            "because you neither perform nor repair it",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)

    def test_resolve_slug_collision_involves_no_repository_mutation(self) -> None:
        section = _section(
            self.text, "`resolve-slug-collision` is not a hygiene action", level=3
        )
        for phrase in (
            "it involves no repository mutation",
            "return blocked naming it as orchestrator-owned and make no "
            "repository mutation: leave the tree exactly as you found it",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)
        # The action is not part of the writer's report vocabulary either.
        enum = self.text.split('"actions": ["', 1)[1].split('"]', 1)[0]
        self.assertNotIn("resolve-slug-collision", enum)

    def test_resolve_slug_collision_forbids_renaming_an_active_change_directory(
        self,
    ) -> None:
        section = _section(
            self.text, "`resolve-slug-collision` is not a hygiene action", level=3
        )
        self.assertIn(
            "you SHALL NOT rename, move, archive, or delete an active change "
            "directory to clear a plan-reachable slug",
            section,
        )
        self.assertIn(
            "An out-of-plan slug collision is an operator-decision blocker, not a "
            "hygiene action either: only the operator may resolve or rename that "
            "external change.",
            section,
        )

    def test_never_do_section_forbids_clearing_a_slug_by_touching_the_change_dir(
        self,
    ) -> None:
        never = _section(self.text, "Never do")
        for phrase in (
            "Never rename, move, archive, or delete an active change directory "
            "to clear a planned-slug collision",
            "resolve-slug-collision is orchestrator-owned and involves no "
            "repository mutation",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, never)

    def test_input_contract_declines_an_orchestrator_owned_collision_blocker(
        self,
    ) -> None:
        contract = _section(self.text, "Input contract")
        self.assertIn(
            "A blocker whose repair.action is resolve-slug-collision, and whose "
            "repair.owner is orchestrator, is not a repair this writer performs",
            contract,
        )


class CarryForwardContractTests(unittest.TestCase):
    """Reviewer, change writer, and implementer carry archived findings forward."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.review_text = _text(_REVIEW)
        cls.review_flat = _flat(cls.review_text)
        cls.auditor_flat = _flat(_text(_AUDITOR))
        cls.writer_text = _text(_CHANGE_WRITER)
        cls.writer_flat = _flat(cls.writer_text)
        cls.implementer_text = _text(_IMPLEMENTER_BODY)
        cls.implementer_flat = _flat(cls.implementer_text)

    # --- 8.4: reviewer --------------------------------------------------

    def test_reviewer_classifies_archived_findings_as_carry_forward(self) -> None:
        self.assertIn(
            "## Post-archive findings are carry-forward items", self.review_text
        )
        section = _section(
            self.review_text, "Post-archive findings are carry-forward items"
        )
        self.assertIn(
            "A finding whose target is an already archived change is not a "
            "finding against that archived change: record it as a "
            "carry-forward item.",
            section,
        )
        for field in (
            "archived_origin — the archived change the defect belongs to;",
            "current_change — the change under review;",
            "locus — path[:symbol] where the behavior lives;",
            "blocks_later_change — true only when the item invalidates a "
            "contract a later planned change depends on;",
            "disposition — follow-up, carry-forward, or blocks-later-change.",
        ):
            with self.subTest(field=field):
                self.assertIn(field, section)

    def test_reviewer_returns_no_corrective_handoff_against_the_archive(self) -> None:
        section = _section(
            self.review_text, "Post-archive findings are carry-forward items"
        )
        self.assertIn(
            "A carry-forward item is not a fix-now finding and not a corrective "
            "handoff against the archive.",
            section,
        )
        self.assertIn(
            "Return no corrective prompt, corrective scope, or fix verdict "
            "whose target is the archived change, and never edit, reopen, or "
            "re-review it.",
            section,
        )
        self.assertIn(
            "Report the active change's verdict on the active change's own "
            "scope, and report carry-forward items separately.",
            section,
        )

    def test_newly_exposed_defect_belongs_to_the_active_change(self) -> None:
        section = _section(
            self.review_text, "Post-archive findings are carry-forward items"
        )
        self.assertIn(
            "If the archived defect is newly exposed or newly worsened by the "
            "active change's diff, report it against the active change instead, "
            "with both the active locus and the archived origin named.",
            section,
        )
        self.assertIn("naming the archived origin is still required.", section)

    def test_reviewer_coverage_contract_is_preserved(self) -> None:
        self.assertIn("## Coverage matrix", self.review_text)
        matrix = _section(self.review_text, "Coverage matrix")
        for case_class in _REVIEW_CASE_CLASSES:
            with self.subTest(case_class=case_class):
                self.assertIn(case_class, matrix)
        for result in ("covered", "not-applicable", "not-covered"):
            with self.subTest(result=result):
                self.assertIn(result, self.review_text)
        self.assertIn('"case"', self.review_text)
        self.assertIn('"class"', self.review_text)
        self.assertIn('"coverage"', self.review_text)
        self.assertIn(
            "one coverage[] entry per supplied matrix case",
            self.review_flat,
        )
        self.assertIn(
            "A carry-forward item never excuses a not-covered case, never "
            "blocks the active change by itself, and never reopens the archived "
            "change",
            self.review_flat,
        )

    # --- 8.4: change writer --------------------------------------------

    def test_change_writer_declines_prior_archived_scope(self) -> None:
        self.assertIn(
            "Do not amend or fix a prior archived change while authoring a "
            "later change. This holds for every later change in the plan, "
            "including changes 2 and 3.",
            self.writer_flat,
        )
        self.assertIn(
            "record the item as an out-of-scope carry-forward item naming the "
            "archived change, the current change, the locus (path[:symbol]), the "
            "observed behavior, the required behavior, the evidence, the impact, "
            "and whether it blocks a later planned change, then author only "
            "CHANGE.",
            self.writer_flat,
        )
        self.assertIn(
            "Do not turn the item into a task, requirement, or accepted "
            "deferral of the change you are authoring.",
            self.writer_flat,
        )
        self.assertIn(
            "do not touch the archived change's directory or delta",
            self.writer_flat,
        )

    def test_change_writer_output_contract_carries_forward(self) -> None:
        self.assertIn('"carry_forward"', self.writer_text)
        self.assertIn('"archived_origin"', self.writer_text)
        self.assertIn('"observed"', self.writer_text)
        self.assertIn('"required"', self.writer_text)
        self.assertIn('"blocks_later_change"', self.writer_text)
        self.assertIn(
            "carry_forward SHALL be [] when no prior-archived defect was "
            "encountered. An entry records scope you declined: it is not a "
            "task, requirement, or deferred scope of CHANGE, and it never "
            "authorizes editing the archived change.",
            self.writer_flat,
        )

    # --- 8.4: implementer plan mode ------------------------------------

    def test_implementer_plan_mode_carries_forward_prior_findings(self) -> None:
        self.assertIn("## Plan-orchestrator mode", self.implementer_text)
        plan_mode = _section(self.implementer_text, "Plan-orchestrator mode")
        self.assertIn("Post-archive findings are carry-forward items:", plan_mode)
        self.assertIn(
            "A defect whose target is an already archived change is out of "
            "scope for CHANGE. Do not reopen the archived change, edit its "
            "archived delta or directory, or adopt the archived defect as this "
            "change's scope.",
            plan_mode,
        )
        self.assertIn(
            "A defect newly exposed or newly worsened by this change's diff "
            "belongs to CHANGE, with the archived origin named.",
            plan_mode,
        )
        self.assertIn(
            "Do not insert a follow-up change or an unplanned fix into the plan.",
            plan_mode,
        )
        self.assertIn('"carry_forward"', plan_mode)
        self.assertIn(
            "carry_forward SHALL be [] when no prior-archived defect was "
            "encountered, and it never authorizes work on an archived change.",
            plan_mode,
        )

    def test_implementer_plan_mode_carry_forward_stays_out_of_controller_mode(
        self,
    ) -> None:
        controller_round = _section(self.implementer_text, "Controller-round workflow")
        self.assertNotIn(
            "carry_forward",
            controller_round,
            "the controller-round output protocol must not gain plan-mode fields",
        )
        self.assertIn(
            "The controller-round output contract later in this file does not "
            "apply in plan-orchestrator mode.",
            self.implementer_flat,
        )

    # --- 8.4: the prohibition is stated in every surface ----------------

    def test_reopening_an_archive_is_forbidden_in_every_surface(self) -> None:
        surfaces = {
            "orchestrator": (
                _flat(_text(_ORCHESTRATOR)),
                "do not reopen the archived change, edit its archived delta, or "
                "re-run its review as if it were active;",
            ),
            "auditor": (
                self.auditor_flat,
                "you SHALL NOT resurrect an archived change directory to "
                "satisfy a check",
            ),
            "reviewer": (
                self.review_flat,
                "never edit, reopen, or re-review it",
            ),
            "change-writer": (
                self.writer_flat,
                "Do not edit source code, tests, archived changes, or unrelated "
                "files.",
            ),
            "implementer": (
                self.implementer_flat,
                "Do not reopen the archived change, edit its archived delta or "
                "directory",
            ),
        }
        for surface, (haystack, prohibition) in surfaces.items():
            with self.subTest(surface=surface):
                self.assertIn(prohibition, haystack)

    def test_inserting_an_unplanned_change_is_forbidden_in_the_plan_surfaces(
        self,
    ) -> None:
        surfaces = {
            "orchestrator": (
                _flat(_text(_ORCHESTRATOR)),
                "do not insert an unplanned follow-up change into the running "
                "plan",
            ),
            "implementer": (
                self.implementer_flat,
                "Do not insert a follow-up change or an unplanned fix into the "
                "plan.",
            ),
            "reviewer": (
                self.review_flat,
                "does not authorize work on the archived change",
            ),
        }
        for surface, (haystack, prohibition) in surfaces.items():
            with self.subTest(surface=surface):
                self.assertIn(prohibition, haystack)


if __name__ == "__main__":
    unittest.main()
