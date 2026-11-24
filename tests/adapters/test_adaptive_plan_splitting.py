"""Contract and transition tests for adaptive plan splitting.

The runtime is prompt-only, so two kinds of test appear here:

* static contract checks over ``core/plan-authoring.md``, the OpenCode
  ``/opsx-plan`` command, ``opsx-orchestrator``, and
  ``opsx-split-materializer``. A missing paragraph is a missing feature.
* pure positive/negative transition cases over ``split_fixture.py``. These
  prove the normative eligibility, remap, freeze, binding, ledger, and
  convergence rules; they do **not** prove live model behavior, which remains
  a contract regression until a real orchestrator run exercises it.
"""

from __future__ import annotations

import re
import unittest
from dataclasses import replace
from pathlib import Path

from tests.adapters import split_fixture as fx

_REPO = Path(__file__).resolve().parents[2]

_PLAN_AUTHORING = _REPO / "core" / "plan-authoring.md"
_PLAN_COMMAND = _REPO / "adapters" / "opencode" / "commands" / "opsx-plan.md"
_ORCHESTRATOR = _REPO / "adapters" / "opencode" / "agents" / "opsx-orchestrator.md"
_MATERIALIZER = (
    _REPO / "adapters" / "opencode" / "agents" / "opsx-split-materializer.md"
)
_AGENTS = _REPO / "AGENTS.md"
_README = _REPO / "README.md"
_ADAPTERS_DOC = _REPO / "docs" / "adapters.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.replace("**", "").replace("`", "").split())


def _section(text: str, heading: str, level: int = 2) -> str:
    marker = "#" * level + " " + heading
    if marker not in text:
        raise AssertionError(f"missing markdown section: {marker!r}")
    rest = text.split(marker, 1)[1]
    stop = re.search(r"\n#{1,%d} " % level, rest)
    body = rest[: stop.start()] if stop else rest
    return _flat(body)


class AuthoringGuidanceContractTests(unittest.TestCase):
    """The shared reference and `/opsx-plan` surface teach exact replacement."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.reference = _text(_PLAN_AUTHORING)
        cls.flat_reference = _flat(cls.reference)
        cls.command = _text(_PLAN_COMMAND)
        cls.flat_command = _flat(cls.command)

    def test_reference_requires_sizing_before_implementation(self) -> None:
        self.assertIn("### Size Before Implementation", self.reference)
        section = _section(self.reference, "Size Before Implementation", level=3)
        self.assertIn(
            "before implementation, not after two failed corrective rounds",
            section,
        )
        self.assertIn(
            "records this assessment before its implement dispatch",
            section,
        )
        self.assertIn("Do not rely on repeated cap_override entries", section)

    def test_reference_documents_the_exact_frontmatter_shape(self) -> None:
        self.assertIn("split_authorizations:", self.reference)
        for field in (
            "parent:",
            "children:",
            "child_dependencies:",
            "obligation_map:",
            "aggregate_exit:",
            "rehome:",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.reference)

    def test_reference_defines_exact_and_model_level_semantics(self) -> None:
        section = _section(self.reference, "Split Authorizations", level=3)
        self.assertIn(
            "exact preauthorized named replacements",
            section,
        )
        self.assertIn(
            "adds no compiled-manifest key, and opsx-plan compile does not read it",
            section,
        )
        self.assertIn(
            "tracked plan is revised to contain only the chosen nodes",
            section,
        )
        self.assertIn(
            "the parent and every unchosen node are absent",
            section,
        )
        self.assertIn(
            "General autonomy, a reviewer verdict, a cap_override, and a "
            "supervisor role are not split authority",
            section,
        )
        self.assertIn("conditional prose authorizes nothing", section)

    def test_reference_preserves_obligations_and_dependencies(self) -> None:
        section = _section(self.reference, "Split Authorizations", level=3)
        self.assertIn(
            "every parent obligation (requirement, finding, success parameter, "
            "or deferral) appears in exactly one child's obligation_map",
            section,
        )
        self.assertIn(
            "no child depends on the parent, and every existing successor "
            "depends on all children",
            section,
        )
        self.assertIn("aggregate_exit proves the parent's original success parameters", section)

    def test_reference_forbids_tier_gate_human_and_archive_changes(self) -> None:
        section = _section(self.reference, "Split Authorizations", level=3)
        self.assertIn(
            "SHALL NOT list a parent or child that is in human_gates, change "
            "acceptance, add or remove a phase or approval gate, change the "
            "trust/privacy posture, or touch an archived change",
            section,
        )
        self.assertIn(
            "parent SHALL NOT be archived or spec-synced as part of the "
            "replacement",
            section,
        )
        self.assertIn(
            "checked tasks are never reused as child acceptance",
            section,
        )

    def test_reference_documents_the_convergence_decision(self) -> None:
        self.assertIn("### Split-or-Continue After Two Corrective Rounds", self.reference)
        section = _section(
            self.reference, "Split-or-Continue After Two Corrective Rounds", level=3
        )
        for phrase in (
            "Prefer split when a previously corrected blocker class has reopened",
            "the blocker set is not shrinking across rounds",
            "orthogonal to the parent's stated concern",
            "exactly one bounded corrective round, a named closure test",
            "A failed closure test reopens the split-or-redecision choice",
            "Propose a re-scope/authority decision",
            "not a stop on the raw round count",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)

    def test_command_requires_sizing_and_exact_authorizations(self) -> None:
        self.assertIn("Size each change for one implement-review-archive loop", self.flat_command)
        self.assertIn(
            "Declare split_authorizations only when the plan preauthorizes an "
            "exact named replacement for an oversized change",
            self.flat_command,
        )
        for phrase in (
            "one exact parent",
            "two or more exact child slugs",
            "obligation map assigning every parent obligation to exactly one child",
            "aggregate exit proving the parent's original success parameters",
            "Omit the field when no exact replacement is authorized",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.flat_command)

    def test_command_states_no_manifest_or_conditional_surface(self) -> None:
        self.assertIn(
            "adds no compiled-manifest key, opsx-plan compile does not read it",
            self.flat_command,
        )
        self.assertIn(
            "must not be expressed as conditional prose or a conditional DAG",
            self.flat_command,
        )
        self.assertIn(
            "the tracked plan contains only the chosen nodes",
            self.flat_command,
        )

    def test_command_validates_and_reports_declarations(self) -> None:
        self.assertIn(
            "Verify every split_authorizations entry",
            self.flat_command,
        )
        self.assertIn(
            "the declared split_authorizations (or that the plan declares none)",
            self.flat_command,
        )


class OrchestratorSplitContractTests(unittest.TestCase):
    """The conductor sizes, decides, and delegates the single authorized write."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _text(_ORCHESTRATOR)
        cls.flat = _flat(cls.text)

    def test_sizing_precedes_implementation(self) -> None:
        self.assertIn("## Sizing and split decisions", self.text)
        section = _section(self.text, "Sizing and split decisions")
        self.assertIn(
            "before the implement dispatch",
            section,
        )
        self.assertIn(
            "cannot plausibly fit one loop, or the plan contains an exact "
            "split_authorizations entry",
            section,
        )
        self.assertIn(
            "rather than spending the corrective allowance first",
            section,
        )
        selection = _section(self.text, "Stage selection")
        self.assertIn("4. Size —", selection)
        self.assertIn("decide split-or-continue before implementation", selection)

    def test_two_round_convergence_is_explicit(self) -> None:
        section = _section(self.text, "Sizing and split decisions")
        self.assertIn(
            "After two corrective rounds, and before granting a third-round "
            "override for the same change",
            section,
        )
        for phrase in (
            "a blocker class that an earlier corrective round claimed to close "
            "has reopened",
            "the blocking set is not shrinking across rounds",
            "orthogonal to the change's stated concern",
            "exactly one bounded corrective round, naming a closure test",
            "does not authorize a second override for the same blocker class",
            "concrete decision request for that parent, not a stop on the raw "
            "round count",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)

    def test_general_autonomy_is_not_split_authority(self) -> None:
        self.assertIn("General autonomy is not split authority", self.flat)
        section = _section(self.text, "Preauthorized split replacement")
        self.assertIn(
            "General autonomy, a cap_override, a reviewer verdict, a passing "
            "audit, and a supervisor role are not authority for a split",
            section,
        )
        self.assertIn(
            "A vague instruction such as \"split this if it gets hard\" "
            "authorizes nothing",
            section,
        )

    def test_exact_authorization_and_no_conditional_dag(self) -> None:
        section = _section(self.text, "Preauthorized split replacement")
        self.assertIn(
            "model-level plan revision, not a conditional manifest node",
            section,
        )
        self.assertIn(
            "tracked plan is revised to contain only the chosen nodes",
            section,
        )
        self.assertIn(
            "No conditional DAG format and no auto-compilation of prose is "
            "introduced",
            section,
        )
        self.assertIn("unlisted child slug", section)
        self.assertIn("no orphan and no duplicated obligation", section)

    def test_eligibility_protects_tier_gates_human_boundary_and_history(self) -> None:
        section = _section(self.text, "Preauthorized split replacement")
        for phrase in (
            "the parent is in the plan and is not in the resolved human-gate set",
            "the parent is unaccepted: it is not archived",
            "not human-gated, not active as a different change, not archived",
            "every child stays in the parent's phase and inherits the same "
            "effective acceptance tier",
            "the split adds, removes, or moves no gate",
            "no archived change, archived delta, or synced specification is edited",
            "Never archive the original parent, never sync its delta spec, and "
            "never reuse its checkmarks as child acceptance",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)

    def test_dependencies_and_aggregate_exit_are_preserved(self) -> None:
        section = _section(self.text, "Preauthorized split replacement")
        self.assertIn(
            "every parent predecessor remains a prerequisite of each child that "
            "needs it, and no child depends on the parent",
            section,
        )
        self.assertIn(
            "every existing successor of the parent depends on all children",
            section,
        )
        self.assertIn(
            "the aggregate exit proves the parent's original success parameters "
            "after the last child archives",
            section,
        )

    def test_freeze_and_named_materialization_are_required(self) -> None:
        section = _section(self.text, "Preauthorized split replacement")
        self.assertIn(
            "A dirty candidate is never moved before it can be restored",
            section,
        )
        self.assertIn(
            "record a restorable donor freeze with the donor identity, source "
            "and evidence hashes, and blob or copy lineage before any rehome",
            section,
        )
        self.assertIn("opsx-split-materializer", section)
        self.assertIn(
            "never run it concurrently with the change writer, implementer, "
            "archiver, or hygiene writer",
            section,
        )
        self.assertIn(
            "If the materializer is unavailable and no named supervision "
            "capability declares the split action",
            section,
        )
        self.assertIn(
            "do not substitute opsx-repo-hygiene, opsx-implementer, or an "
            "installed model's generic edit ability",
            section,
        )

    def test_no_child_starts_before_a_clean_bound_revision(self) -> None:
        section = _section(self.text, "Preauthorized split replacement")
        self.assertIn(
            "Do not dispatch any child until the selected plan revision is "
            "clean, committed at HEAD, contains only the chosen nodes, and its "
            "compiled graph and hash are verified and bound in the ledger",
            section,
        )

    def test_ledger_resume_and_drift_fail_closed(self) -> None:
        section = _section(self.text, "Preauthorized split replacement")
        self.assertIn("selected → frozen → materialized → bound", section)
        self.assertIn("At most one non-terminal split exists per parent", section)
        self.assertIn(
            "A bound record with the same selected plan hash is a no-op",
            section,
        )
        self.assertIn(
            "A selected, frozen, or materialized record resumes at the earliest "
            "incomplete state only after revalidating its captured hashes",
            section,
        )
        self.assertIn(
            "Parent, child, or plan-hash mismatch is plan drift: fail closed",
            section,
        )
        self.assertIn(
            "An active or archived child-slug collision, an unlisted child, or "
            "an unmet prerequisite change is refused",
            section,
        )

    def test_proposed_decision_replaces_a_raw_count_stop(self) -> None:
        section = _section(self.text, "Preauthorized split replacement")
        self.assertIn(
            "return a concrete proposed re-scope/authority decision",
            section,
        )
        self.assertIn(
            "Do not stop on the raw round count, do not edit the plan yourself, "
            "and do not dispatch child work from an unbound revision",
            section,
        )
        self.assertIn(
            "Independent work that is already authorized by the plan may continue",
            section,
        )

    def test_worker_fields_include_the_materializer(self) -> None:
        self.assertIn("`opsx-split-materializer`", self.text)
        self.assertIn("SPLIT_ID", self.flat)
        self.assertIn("AUTHORIZATION_HASH", self.flat)
        self.assertIn("FREEZE_DIR", self.flat)


class MaterializerContractTests(unittest.TestCase):
    """The one authorized writer is narrow, frozen, and archive-free."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _text(_MATERIALIZER)
        cls.flat = _flat(cls.text)

    def test_permissions_are_writer_only(self) -> None:
        self.assertIn("edit: allow", self.text)
        self.assertIn("bash: allow", self.text)
        self.assertIn("task: deny", self.text)
        self.assertIn("question: deny", self.text)
        self.assertNotIn("{env:OPSX_SPLIT", self.text)

    def test_input_contract_names_the_exact_authorization(self) -> None:
        for field_name in (
            "SPLIT_ID",
            "RUN_ID",
            "PLAN_PATH",
            "PARENT",
            "CHILDREN",
            "CHILD_DEPENDENCIES",
            "OBLIGATION_MAP",
            "AGGREGATE_EXIT",
            "REHOME",
            "AUTHORIZATION_HASH",
            "BASE_REV",
            "PLAN_REVISION_SHA",
            "ACCEPTANCE",
            "FREEZE_DIR",
        ):
            with self.subTest(field=field_name):
                self.assertIn(field_name, self.text)

    def test_freeze_precedes_rehome_and_requires_hashes_and_lineage(self) -> None:
        self.assertIn(
            "Before rehoming any dirty candidate, write a freeze manifest",
            self.flat,
        )
        for phrase in (
            "the sha256 of the exact bytes you read",
            "the source revision or blob identity when tracked",
            "the child owner from REHOME",
            "the restore instruction and lineage to the parent and selected plan",
            "A dirty candidate with no authorized rehome path is left in place",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.flat)

    def test_it_never_archives_or_changes_authority(self) -> None:
        for phrase in (
            "Never archive, spec-sync, or mark accepted the original parent",
            "Never edit an archived change, archived delta, or synced spec",
            "Never change acceptance, phase tier, human_gates, pause_before intent, "
            "trust/privacy posture, or acceptance policy",
            "Never reuse the parent's checked tasks as child acceptance",
            "Never push, create a remote branch, open a pull request, or merge",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.flat)

    def test_selected_plan_is_unconditional_and_verified(self) -> None:
        self.assertIn(
            "The revised plan must contain only the chosen nodes: no parent, no "
            "unchosen child, no conditional prose, no conditional DAG",
            self.flat,
        )
        self.assertIn(
            "verifying the compiled node set is exactly the chosen nodes, the "
            "parent is absent, and every edge matches the approved map",
            self.flat,
        )


class RepositoryDocumentationTests(unittest.TestCase):
    def test_agents_names_the_materializer_and_explicit_authority(self) -> None:
        text = _text(_AGENTS)
        flat = _flat(text)
        self.assertIn("opsx-split-materializer", text)
        self.assertIn("split_authorizations", flat)
        self.assertIn("exact", flat)
        self.assertIn("Split authority is not implicit", flat)

    def test_readme_and_adapter_docs_describe_the_split_path(self) -> None:
        for path in (_README, _ADAPTERS_DOC):
            with self.subTest(path=path.name):
                text = _text(path)
                flat = _flat(text)
                self.assertIn("opsx-split-materializer", text)
                self.assertIn("split_authorizations", flat)


class SplitEligibilityTransitionTests(unittest.TestCase):
    """Positive and negative normative transition cases."""

    def assertFails(self, spec: fx.SplitSpec, reason: str) -> None:
        decision = fx.validate_split(spec)
        self.assertFalse(decision.ok)
        self.assertTrue(
            any(reason in item for item in decision.reasons),
            decision.reasons,
        )
        self.assertIsNotNone(decision.proposed)
        self.assertEqual(decision.proposed.parent, spec.parent)

    def test_valid_exact_replacement_binds(self) -> None:
        decision = fx.validate_split(fx.SplitSpec())
        self.assertTrue(decision.ok, decision.reasons)
        self.assertEqual(decision.state, "bound")
        self.assertFalse(decision.idempotent)

    def test_general_autonomy_and_roles_are_not_authority(self) -> None:
        for authority in ("general-autonomy", "supervisor-role", "cap-override", "none"):
            with self.subTest(authority=authority):
                self.assertFails(
                    replace(fx.SplitSpec(), authority=authority),
                    "not-authorized",
                )

    def test_missing_or_unlisted_authorization_is_refused(self) -> None:
        no_auth = replace(
            fx.valid_plan(),
            authorizations=(),
        )
        self.assertFails(
            replace(fx.SplitSpec(), plan=no_auth),
            "no-exact-authorization",
        )
        self.assertFails(
            replace(fx.SplitSpec(), children=(fx.CHILD_A, fx.CHILD_B, "third-child")),
            "unlisted-child-slug",
        )

    def test_human_gated_parent_or_child_is_refused(self) -> None:
        gated_parent = replace(
            fx.valid_plan(),
            human_gates=(fx.PARENT,),
        )
        self.assertFails(
            replace(fx.SplitSpec(), plan=gated_parent),
            "human-gated-parent",
        )
        gated_child = replace(
            fx.valid_plan(),
            human_gates=(fx.CHILD_B,),
        )
        self.assertFails(
            replace(fx.SplitSpec(), plan=gated_child),
            "human-gated-child",
        )

    def test_archived_or_accepted_parent_is_refused(self) -> None:
        for spec, reason in (
            (replace(fx.SplitSpec(), parent_archived=True), "archived-parent"),
            (replace(fx.SplitSpec(), parent_accepted=True), "accepted-parent"),
            (
                replace(fx.SplitSpec(), parent_spec_synced=True),
                "parent-spec-synced",
            ),
            (
                replace(fx.SplitSpec(), archived_touched=(fx.PARENT,)),
                "archived-work-touched",
            ),
        ):
            with self.subTest(reason=reason):
                self.assertFails(spec, reason)

    def test_child_collisions_archived_and_planned_children_are_refused(self) -> None:
        cases = (
            (
                replace(
                    fx.valid_plan(),
                    archived=frozenset({fx.CHILD_A}),
                ),
                "child-archived",
            ),
            (
                replace(
                    fx.valid_plan(),
                    active={fx.CHILD_A: "legacy-occupant"},
                ),
                "child-slug-collision",
            ),
            (
                replace(
                    fx.valid_plan(),
                    changes={
                        **fx.valid_plan().changes,
                        fx.CHILD_A: fx.ChangeNode(fx.CHILD_A, 1, ()),
                    },
                ),
                "child-already-planned",
            ),
        )
        for plan, reason in cases:
            with self.subTest(reason=reason):
                self.assertFails(replace(fx.SplitSpec(), plan=plan), reason)

    def test_tier_gate_human_trust_and_acceptance_changes_are_refused(self) -> None:
        for flag in (
            "tier_changed",
            "phase_changed",
            "human_gates_changed",
            "gates_changed",
            "trust_changed",
            "acceptance_policy_changed",
        ):
            with self.subTest(flag=flag):
                self.assertFails(
                    replace(fx.SplitSpec(), **{flag: True}),
                    "authority-boundary-change",
                )

    def test_selected_plan_must_contain_only_chosen_nodes(self) -> None:
        self.assertFails(
            replace(
                fx.SplitSpec(),
                selected_plan_nodes=(
                    fx.PARENT,
                    fx.PREREQ,
                    fx.CHILD_A,
                    fx.CHILD_B,
                    fx.SUCCESSOR,
                    fx.UNRELATED,
                ),
            ),
            "selected-plan-node-set",
        )

    def test_orphaned_and_duplicated_obligations_are_refused(self) -> None:
        auth = fx.valid_plan().authorizations[0]
        orphaned = {
            fx.CHILD_A: ("req-cache-policy", "sp-cache"),
            fx.CHILD_B: ("req-cache-expiry",),
        }
        self.assertEqual(auth.obligation_map.keys(), {fx.CHILD_A, fx.CHILD_B})
        self.assertFails(
            replace(fx.SplitSpec(), obligation_map=orphaned),
            "orphaned-obligation",
        )
        duplicated = {
            fx.CHILD_A: ("req-cache-policy", "sp-cache", "finding-race"),
            fx.CHILD_B: ("req-cache-expiry", "finding-race"),
        }
        self.assertFails(
            replace(fx.SplitSpec(), obligation_map=duplicated),
            "duplicated-obligation",
        )

    def test_unparented_obligation_is_refused(self) -> None:
        extra = {
            fx.CHILD_A: ("req-cache-policy", "sp-cache"),
            fx.CHILD_B: ("req-cache-expiry", "finding-race", "invented-obligation"),
        }
        self.assertFails(
            replace(fx.SplitSpec(), obligation_map=extra),
            "unparented-obligation",
        )

    def test_dependency_remap_protects_predecessors_successors_and_slugs(self) -> None:
        # Dropping the parent's predecessor from the entry child.
        auth = fx.valid_plan().authorizations[0]
        bad_auth = replace(
            auth,
            child_dependencies={
                **auth.child_dependencies,
                fx.CHILD_A: (),
            },
        )
        plan = replace(fx.valid_plan(), authorizations=(bad_auth,))
        self.assertFails(
            replace(fx.SplitSpec(), plan=plan),
            "dropped-predecessor",
        )

        # A successor that still points only at one child drops the other edge.
        self.assertFails(
            replace(
                fx.SplitSpec(),
                result_dependencies={
                    **fx.valid_dependencies(),
                    fx.SUCCESSOR: (fx.CHILD_A,),
                },
            ),
            "dependency-remap",
        )

        # An unknown dependency slug is not a real plan edge.
        bad_unknown = replace(
            auth,
            child_dependencies={
                **auth.child_dependencies,
                fx.CHILD_A: ("not-a-plan-change",),
            },
        )
        self.assertFails(
            replace(
                fx.SplitSpec(),
                plan=replace(fx.valid_plan(), authorizations=(bad_unknown,)),
                result_dependencies={
                    **fx.valid_dependencies(),
                    fx.CHILD_A: ("not-a-plan-change",),
                },
            ),
            "unknown-child-dependency",
        )

    def test_unmet_prerequisite_is_refused(self) -> None:
        self.assertFails(
            replace(fx.SplitSpec(), completed_changes=frozenset()),
            "unmet-prerequisite-change",
        )

    def test_child_depending_on_parent_is_refused(self) -> None:
        auth = fx.valid_plan().authorizations[0]
        bad = replace(
            auth,
            child_dependencies={**auth.child_dependencies, fx.CHILD_A: (fx.PARENT,)},
        )
        self.assertFails(
            replace(
                fx.SplitSpec(),
                plan=replace(fx.valid_plan(), authorizations=(bad,)),
                result_dependencies={
                    **fx.valid_dependencies(),
                    fx.CHILD_A: (fx.PARENT,),
                },
            ),
            "child-depends-on-parent",
        )

    def test_missing_aggregate_exit_is_refused(self) -> None:
        auth = fx.valid_plan().authorizations[0]
        self.assertFails(
            replace(
                fx.SplitSpec(),
                plan=replace(
                    fx.valid_plan(),
                    authorizations=(replace(auth, aggregate_exit=""),),
                ),
            ),
            "missing-aggregate-exit",
        )
        self.assertFails(
            replace(fx.SplitSpec(), aggregate_exit="a different exit"),
            "aggregate-exit-mismatch",
        )

    def test_old_checkmarks_are_not_acceptance(self) -> None:
        self.assertFails(
            replace(
                fx.SplitSpec(),
                reused_checkmarks=("tasks.md:1.1", "tasks.md:1.2"),
            ),
            "old-checkmarks-used-as-acceptance",
        )

    def test_dirty_candidate_requires_a_complete_restorable_freeze(self) -> None:
        self.assertFails(
            replace(fx.SplitSpec(), dirty_candidate=True),
            "dirty-candidate-without-freeze",
        )
        self.assertFails(
            replace(
                fx.SplitSpec(),
                dirty_candidate=True,
                freeze=fx.FreezeRecord(
                    donor="openspec/changes/split-parent",
                    path_hashes={},
                    lineage=(),
                    restorable=True,
                ),
            ),
            "freeze-missing-hash",
        )
        self.assertFails(
            replace(
                fx.SplitSpec(),
                dirty_candidate=True,
                freeze=fx.FreezeRecord(
                    donor="openspec/changes/split-parent",
                    path_hashes={"src/cache/policy.py": "sha"},
                    lineage=("parent",),
                    restorable=False,
                ),
            ),
            "freeze-not-restorable",
        )

    def test_frozen_rehome_path_binds(self) -> None:
        spec = fx.with_freeze(
            fx.SplitSpec(),
            {"src/cache/policy.py": "sha-policy"},
        )
        decision = fx.validate_split(spec)
        self.assertTrue(decision.ok, decision.reasons)

    def test_materializer_is_required_unless_named_supervision_declares_it(self) -> None:
        self.assertFails(
            replace(fx.SplitSpec(), materializer_available=False),
            "no-authorized-materializer",
        )
        allowed = replace(
            fx.SplitSpec(),
            materializer_available=False,
            named_supervision_declared=True,
        )
        self.assertTrue(fx.validate_split(allowed).ok)

    def test_no_child_dispatch_before_bound(self) -> None:
        self.assertFails(
            replace(fx.SplitSpec(), child_dispatched_before_bound=True),
            "child-dispatched-before-bound",
        )

    def test_plan_drift_is_refused(self) -> None:
        self.assertFails(
            replace(fx.SplitSpec(), plan_hash="plan-h2"),
            "plan-drift",
        )

    def test_second_pending_selection_is_refused(self) -> None:
        self.assertFails(
            replace(
                fx.SplitSpec(),
                ledger=(
                    fx.SplitRecord(
                        parent=fx.PARENT,
                        state="frozen",
                        plan_hash="plan-h1",
                    ),
                ),
            ),
            "second-pending-selection",
        )

    def test_resume_is_idempotent_after_binding(self) -> None:
        decision = fx.validate_split(
            replace(
                fx.SplitSpec(),
                ledger=(
                    fx.SplitRecord(
                        parent=fx.PARENT,
                        state="bound",
                        plan_hash="plan-h1",
                    ),
                ),
            )
        )
        self.assertTrue(decision.ok, decision.reasons)
        self.assertTrue(decision.idempotent)
        self.assertEqual(decision.state, "bound")

    def test_resume_reconciles_a_partial_record(self) -> None:
        decision = fx.validate_split(
            replace(
                fx.SplitSpec(),
                ledger=(
                    fx.SplitRecord(
                        parent=fx.PARENT,
                        state="materialized",
                        plan_hash="plan-h1",
                    ),
                ),
                new_selection=False,
            )
        )
        self.assertTrue(decision.ok, decision.reasons)
        self.assertEqual(decision.resume_from, "materialized")

    def test_authorization_hash_mismatch_is_refused(self) -> None:
        for state in ("frozen", "materialized", "bound"):
            with self.subTest(state=state):
                self.assertFails(
                    replace(
                        fx.SplitSpec(),
                        authorization_hash="auth-2",
                        ledger=(
                            fx.SplitRecord(
                                parent=fx.PARENT,
                                state=state,
                                plan_hash="plan-h1",
                                authorization_hash="auth-1",
                            ),
                        ),
                        new_selection=False,
                    ),
                    "authorization-hash-mismatch",
                )

    def test_resume_accepts_a_matching_authorization_hash(self) -> None:
        decision = fx.validate_split(
            replace(
                fx.SplitSpec(),
                authorization_hash="auth-7",
                ledger=(
                    fx.SplitRecord(
                        parent=fx.PARENT,
                        state="frozen",
                        plan_hash="plan-h1",
                        authorization_hash="auth-7",
                    ),
                ),
                new_selection=False,
            )
        )
        self.assertTrue(decision.ok, decision.reasons)
        self.assertEqual(decision.resume_from, "frozen")

    def test_invalid_ledger_state_is_refused(self) -> None:
        self.assertFails(
            replace(
                fx.SplitSpec(),
                ledger=(
                    fx.SplitRecord(
                        parent=fx.PARENT,
                        state="archived",
                        plan_hash="plan-h1",
                    ),
                ),
                new_selection=False,
            ),
            "invalid-ledger-state",
        )


class ConvergenceDecisionTests(unittest.TestCase):
    """The split-or-continue rule never becomes a raw-count stop."""

    def test_before_two_rounds_continues(self) -> None:
        decision = fx.decide_convergence(fx.ConvergenceFacts(corrective_rounds=1))
        self.assertEqual(decision.kind, "continue")

    def test_reopened_non_shrinking_and_orthogonal_prefer_split(self) -> None:
        cases = (
            fx.ConvergenceFacts(
                corrective_rounds=2,
                reopened_blocker_class=True,
                authorization_available=True,
            ),
            fx.ConvergenceFacts(
                corrective_rounds=2,
                blocker_counts=(3, 3),
                authorization_available=True,
            ),
            fx.ConvergenceFacts(
                corrective_rounds=2,
                orthogonal_work=True,
                authorization_available=True,
            ),
        )
        for facts in cases:
            with self.subTest(facts=facts):
                self.assertEqual(fx.decide_convergence(facts).kind, "split")

    def test_unauthorized_split_returns_a_proposed_decision(self) -> None:
        decision = fx.decide_convergence(
            fx.ConvergenceFacts(
                corrective_rounds=2,
                reopened_blocker_class=True,
                authorization_available=False,
            )
        )
        self.assertEqual(decision.kind, "proposed-redecision")
        self.assertIn("no-exact-authorization", decision.reasons)
        self.assertTrue(decision.independent_work_may_continue)

    def test_continue_is_one_bounded_correction_with_a_closure_test(self) -> None:
        decision = fx.decide_convergence(
            fx.ConvergenceFacts(
                corrective_rounds=2,
                blocker_counts=(3, 1),
                closure_test="pytest tests/cache/test_policy.py",
            )
        )
        self.assertEqual(decision.kind, "bounded-continue")
        self.assertEqual(
            decision.closure_test,
            "pytest tests/cache/test_policy.py",
        )

    def test_no_closure_test_or_prior_override_returns_proposed(self) -> None:
        for facts in (
            fx.ConvergenceFacts(corrective_rounds=2, blocker_counts=(3, 1)),
            fx.ConvergenceFacts(
                corrective_rounds=2,
                blocker_counts=(3, 1),
                closure_test="pytest tests/cache/test_policy.py",
                override_already_used=True,
            ),
        ):
            with self.subTest(facts=facts):
                self.assertEqual(
                    fx.decide_convergence(facts).kind,
                    "proposed-redecision",
                )


if __name__ == "__main__":
    unittest.main()
