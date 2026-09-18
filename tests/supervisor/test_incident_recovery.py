"""Tests for the durable incident lifecycle and the bounded recovery surface.

Covers task group 1 of ``add-bounded-incident-recovery``: the forward-only
v7 -> v8 signature migration, incident-to-signature linking, and the guarded
incident state transitions. Later task groups (recovery classification,
standing grants, the recovery orchestration, and each bounded path) extend this
file with their own assertions.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.supervisor import agent_contracts, budgets, endpoints, ledger, model_policy, recovery


def _grant(**effects: object) -> dict:
    return {
        "version": recovery.STANDING_GRANT_SCHEMA_VERSION,
        "effects": {effect: {"max": bound} for effect, bound in effects.items()},
    }


def _granted_policy(**effects: object) -> dict:
    return _policy(authority_config={"mode": "policy-bound", "standing_grants": _grant(**effects)})


def _selection() -> dict:
    return {
        "version": model_policy.MODEL_POLICY_VERSION,
        "roles": {"implementer": "cheap/model-a"},
        "stages": {"implement": "implementer"},
    }


def _policy(**overrides: object) -> dict:
    base = {
        "authority_config": {"mode": "policy-bound", "approval": "supervisor"},
        "model_selection": _selection(),
        "inexpensive_allowlist": {
            "version": model_policy.MODEL_POLICY_VERSION,
            "models": ["cheap/model-a"],
            "source": "test",
        },
        "manifest_snapshot_hash": "deadbeef",
        "budgets": {
            "version": budgets.BUDGET_SCHEMA_VERSION,
            "total_cost_usd": 100.0,
            "per_action_cost_usd": None,
            "total_elapsed_minutes": None,
            "per_action_elapsed_minutes": None,
            "max_incident_attempts": 3,
        },
        "deadlines": {
            "version": budgets.BUDGET_SCHEMA_VERSION,
            "execution_deadline_minutes": None,
        },
    }
    base.update(overrides)
    return base


class IncidentRecoveryTestCase(unittest.TestCase):
    """Shared temp layout: a repository, a worktree, and trusted storage."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.worktree = self.repo / "worktree"
        self.worktree.mkdir()
        self.storage = self.root / "service-storage"
        self.storage.mkdir()
        self.db_path = self.storage / "supervisor.sqlite3"

    def open(self, **kwargs: object) -> ledger.Ledger:
        kwargs.setdefault("repository_root", self.repo)
        handle = ledger.open_ledger(self.db_path, **kwargs)
        self.addCleanup(handle.close)
        return handle

    def register(self, handle: ledger.Ledger, **overrides: object) -> int:
        params = {
            "run_id": "run-1",
            "worktree": self.worktree,
            "owner": "service",
            "policy": _policy(),
            "operator": "operator",
            "manifest_content": "[[changes]]\nid = \"change-a\"\n",
        }
        params.update(overrides)
        return handle.register_job(**params)


class IncidentVocabularyTests(IncidentRecoveryTestCase):
    """Task 1.1: incident state constants, legal transitions, terminal guards."""

    def test_incident_states_and_terminal_states_are_closed(self) -> None:
        self.assertEqual(
            ledger.INCIDENT_STATES, ("open", "recovering", "resolved", "escalated")
        )
        self.assertEqual(
            ledger.TERMINAL_INCIDENT_STATES, ("resolved", "escalated")
        )

    def test_legal_transitions_match_the_documented_lifecycle(self) -> None:
        self.assertEqual(
            ledger.legal_incident_transitions("open"), ("recovering", "escalated")
        )
        self.assertEqual(
            ledger.legal_incident_transitions("recovering"),
            ("resolved", "escalated"),
        )
        self.assertEqual(ledger.legal_incident_transitions("resolved"), ())
        self.assertEqual(ledger.legal_incident_transitions("escalated"), ())
        self.assertEqual(ledger.legal_incident_transitions("unknown"), ())

        self.assertTrue(ledger.incident_transition_allowed("open", "recovering"))
        self.assertTrue(ledger.incident_transition_allowed("open", "escalated"))
        self.assertFalse(ledger.incident_transition_allowed("open", "resolved"))
        self.assertFalse(ledger.incident_transition_allowed("resolved", "recovering"))


class IncidentSignatureTests(IncidentRecoveryTestCase):
    """Task 1.3: record_incident links the stable attempt signature."""

    def test_signature_is_recorded_and_survives_reopen(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        signature = budgets.incident_signature(
            kind="transient_provider", change_id="change-a", stage="implement"
        )
        incident_id = handle.record_incident(
            job_id,
            kind="transient_provider",
            signature=signature,
            summary="provider 503",
        )
        handle.record_incident_attempt(job_id, signature=signature)
        handle.record_incident_attempt(job_id, signature=signature)
        handle.close()

        reopened = self.open()
        incident = reopened.get_incident(incident_id)
        self.assertEqual(incident["state"], "open")
        self.assertEqual(incident["signature"], signature)
        self.assertEqual(
            reopened.incident_attempt_count(job_id, signature), 2
        )
        listed = {row["id"]: row for row in reopened.list_incidents(job_id)}
        self.assertEqual(listed[incident_id]["signature"], signature)

    def test_incident_without_a_signature_is_unlinked(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        incident_id = handle.record_incident(job_id, kind="crash", summary="legacy")
        self.assertIsNone(handle.get_incident(incident_id)["signature"])

    def test_record_incident_rejects_an_unknown_state(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        with self.assertRaises(ledger.LedgerError):
            handle.record_incident(job_id, kind="crash", state="triaging")


class IncidentTransitionTests(IncidentRecoveryTestCase):
    """Task 1.3: transition_incident is guarded, durable, and named."""

    def test_incident_resolves_through_the_lifecycle(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        incident_id = handle.record_incident(
            job_id, kind="transient_provider", signature="sig-1"
        )
        recovering = handle.transition_incident(incident_id, target_state="recovering")
        self.assertEqual(recovering["state"], "recovering")
        resolved = handle.transition_incident(
            incident_id, target_state="resolved", summary="repair verified"
        )
        self.assertEqual(resolved["state"], "resolved")
        self.assertEqual(resolved["summary"], "repair verified")
        resolved_at = resolved["updated_at"]
        handle.close()

        reopened = self.open()
        persisted = reopened.get_incident(incident_id)
        self.assertEqual(persisted["state"], "resolved")
        self.assertEqual(persisted["summary"], "repair verified")
        self.assertEqual(persisted["updated_at"], resolved_at)

    def test_an_open_incident_can_escalate_directly(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        incident_id = handle.record_incident(job_id, kind="runtime_defect")
        escalated = handle.transition_incident(
            incident_id, target_state="escalated", summary="operator blocker"
        )
        self.assertEqual(escalated["state"], "escalated")

    def test_illegal_transition_is_refused_and_state_is_unchanged(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        incident_id = handle.record_incident(job_id, kind="transient_provider")
        with self.assertRaises(ledger.IncidentTransitionError):
            handle.transition_incident(incident_id, target_state="resolved")
        # The refusal changed nothing: the legal next transition still works.
        self.assertEqual(
            handle.get_incident(incident_id)["state"], "open"
        )
        recovered = handle.transition_incident(incident_id, target_state="recovering")
        self.assertEqual(recovered["state"], "recovering")

    def test_terminal_incident_refuses_all_further_transitions(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        incident_id = handle.record_incident(job_id, kind="transient_provider")
        handle.transition_incident(incident_id, target_state="recovering")
        handle.transition_incident(incident_id, target_state="escalated")
        for target in ("resolved", "recovering", "open"):
            with self.assertRaises(ledger.TerminalIncidentError):
                handle.transition_incident(incident_id, target_state=target)
        self.assertEqual(handle.get_incident(incident_id)["state"], "escalated")

    def test_unknown_incident_raises_unknown_record(self) -> None:
        handle = self.open()
        self.register(handle)
        with self.assertRaises(ledger.UnknownRecordError):
            handle.transition_incident(99999, target_state="recovering")

    def test_unknown_target_state_is_refused(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        incident_id = handle.record_incident(job_id, kind="transient_provider")
        with self.assertRaises(ledger.LedgerError):
            handle.transition_incident(incident_id, target_state="triaging")


class IncidentMigrationTests(IncidentRecoveryTestCase):
    """Task 1.2: the forward-only v7 -> v8 signature migration."""

    def _simulate_v7_database(self, handle: ledger.Ledger) -> None:
        handle.close()
        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP INDEX IF EXISTS idx_incidents_signature")
        conn.execute("ALTER TABLE incidents DROP COLUMN signature")
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
        conn.close()

    def test_v7_ledger_migrates_forward_and_gains_the_signature_link(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        incident_id = handle.record_incident(
            job_id, kind="crash", signature="sig-before", summary="restart"
        )
        self._simulate_v7_database(handle)

        migrated = self.open()
        self.assertEqual(migrated.schema_version(), ledger.CURRENT_SCHEMA_VERSION)
        self.assertGreaterEqual(ledger.CURRENT_SCHEMA_VERSION, 8)
        # Existing rows survive with their legacy (unlinked) signature.
        incident = migrated.get_incident(incident_id)
        self.assertEqual(incident["id"], incident_id)
        self.assertEqual(incident["kind"], "crash")
        self.assertIsNone(incident["signature"])
        # The migration adds the link and its index; new rows can use it.
        new_id = migrated.record_incident(
            job_id, kind="crash", signature="sig-after"
        )
        self.assertEqual(migrated.get_incident(new_id)["signature"], "sig-after")
        indexes = {
            row[1]
            for row in migrated.connection.execute("PRAGMA index_list(incidents)")
        }
        self.assertIn("idx_incidents_signature", indexes)

    def test_migration_runs_inside_one_transaction(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        incident_id = handle.record_incident(job_id, kind="crash")
        self._simulate_v7_database(handle)

        broken = mock.MagicMock(side_effect=RuntimeError("migration failed"))
        with mock.patch.object(
            ledger, "MIGRATIONS", {**ledger.MIGRATIONS, 8: broken}
        ):
            with self.assertRaises(RuntimeError):
                ledger.open_ledger(self.db_path, repository_root=self.repo)
        # A refused migration rolls back the version bump and the column add;
        # the real migration then runs cleanly on reopen with rows preserved.
        migrated = self.open()
        self.assertEqual(migrated.schema_version(), ledger.CURRENT_SCHEMA_VERSION)
        self.assertEqual(migrated.get_incident(incident_id)["kind"], "crash")


class FailureClassificationTests(unittest.TestCase):
    """Task 2.2 / 7.1: evidence-based classification and class -> path mapping."""

    def test_every_known_class_maps_to_a_bounded_path(self) -> None:
        self.assertEqual(len(recovery.INCIDENT_CLASSES), len(recovery.CLASS_PATHS))
        for failure_class in recovery.INCIDENT_CLASSES:
            with self.subTest(failure_class=failure_class):
                plan = recovery.recovery_plan(failure_class)
                self.assertIn(plan["path"], recovery.PATHS)
                self.assertTrue(plan["permitted_remedies"])

    def test_transient_5xx_and_permanent_errors_diverge(self) -> None:
        self.assertEqual(
            recovery.classify_failure({"status_code": 503}), "transient_provider"
        )
        self.assertEqual(
            recovery.classify_failure({"failure_class": "timeout"}), "transient_provider"
        )
        self.assertEqual(
            recovery.classify_failure({"error_type": "ConnectionResetError"}),
            "transient_provider",
        )
        self.assertEqual(
            recovery.classify_failure({"status_code": 401}), "permanent_provider"
        )
        self.assertEqual(
            recovery.classify_failure({"status_code": 429, "message": "hard quota exceeded"}),
            "permanent_provider",
        )
        self.assertTrue(recovery.is_retryable("transient_provider"))
        self.assertFalse(recovery.is_retryable("permanent_provider"))

    def test_undetermined_is_treated_as_permanent(self) -> None:
        failure_class = recovery.classify_failure({"message": "something odd happened"})
        self.assertEqual(failure_class, "undetermined")
        self.assertFalse(recovery.is_retryable(failure_class))
        self.assertEqual(recovery.CLASS_PATHS[failure_class], recovery.PATH_ESCALATE)

    def test_runtime_defect_is_classified_from_markers(self) -> None:
        self.assertEqual(
            recovery.classify_failure({"message": "no module named lib.supervisor"}),
            "runtime_defect",
        )
        self.assertEqual(recovery.CLASS_PATHS["runtime_defect"], recovery.PATH_RUNTIME_DEFECT_BLOCKER)


class RemedyVocabularyTests(unittest.TestCase):
    """Task 2.3 / 2.6: the closed remedy vocabulary refuses out-of-set choices."""

    def test_out_of_set_remedy_is_a_policy_violation(self) -> None:
        decision = recovery.remedy_decision("permanent_provider", "retry_transient")
        self.assertFalse(decision["allowed"])
        with self.assertRaises(recovery.RemedyPolicyViolation):
            recovery.validate_remedy("permanent_provider", "retry_transient")

    def test_destructive_remedy_is_refused_even_for_a_dirty_worktree(self) -> None:
        decision = recovery.remedy_decision("dirty_worktree", "reset_hard")
        self.assertFalse(decision["allowed"])
        self.assertIn("destructive", decision["reason"])

    def test_permitted_remedy_is_accepted(self) -> None:
        self.assertEqual(
            recovery.validate_remedy("dirty_worktree", "repair_worktree_preserving"),
            "repair_worktree_preserving",
        )


class StandingGrantTests(IncidentRecoveryTestCase):
    """Task 2.4 / 2.5 / 2.6: versioned grant schema and fail-closed evaluation."""

    def test_unversioned_grant_is_rejected_on_write(self) -> None:
        with self.assertRaises(recovery.StandingGrantShapeError):
            recovery.validate_standing_grants({"effects": {"commit": {"max": 1}}})

    def test_unknown_effect_and_missing_bound_are_rejected(self) -> None:
        with self.assertRaises(recovery.StandingGrantShapeError):
            recovery.validate_standing_grants({"version": 1, "effects": {"push": {"max": 1}}})
        with self.assertRaises(recovery.StandingGrantShapeError):
            recovery.validate_standing_grants({"version": 1, "effects": {"commit": {}}})

    def test_absent_grant_authorizes_nothing(self) -> None:
        decision = recovery.evaluate_standing_grant(
            {}, effect="commit", verdict={"consumable": True}
        )
        self.assertFalse(decision["authorized"])
        self.assertEqual(decision["state"], "legacy_unversioned")

    def test_grant_requires_a_passing_verdict(self) -> None:
        policy = _granted_policy(commit=2)
        decision = recovery.evaluate_standing_grant(
            policy, effect="commit", verdict={"consumable": False}
        )
        self.assertFalse(decision["authorized"])

    def test_uncovered_effect_and_reached_bound_are_refused(self) -> None:
        policy = _granted_policy(commit=1)
        self.assertFalse(
            recovery.evaluate_standing_grant(
                policy, effect="resume", verdict={"consumable": True}
            )["authorized"]
        )
        self.assertFalse(
            recovery.evaluate_standing_grant(
                policy, effect="commit", verdict={"consumable": True}, consumed=1
            )["authorized"]
        )
        self.assertTrue(
            recovery.evaluate_standing_grant(
                policy, effect="commit", verdict={"consumable": True}, consumed=0
            )["authorized"]
        )

    def test_worker_cannot_create_or_widen_a_grant(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        current = handle.current_policy(job_id)
        self.assertEqual(current["standing_grant_state"], "legacy_unversioned")
        with self.assertRaises(recovery.StandingGrantShapeError):
            recovery.operator_standing_grant_revision(
                handle,
                job_id=job_id,
                revision=current["revision"] + 1,
                policy=current,
                operator="implementer",
                standing_grants=_grant(commit=1),
            )
        # The stored policy is unchanged: still fail-closed, no grant.
        self.assertEqual(
            handle.current_policy(job_id)["standing_grant_state"], "legacy_unversioned"
        )

    def test_operator_grant_revision_is_versioned_and_persists(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        current = handle.current_policy(job_id)
        recovery.operator_standing_grant_revision(
            handle,
            job_id=job_id,
            revision=current["revision"] + 1,
            policy=current,
            operator="operator",
            standing_grants=_grant(commit=2, reset=None),
        )
        handle.close()
        reopened = self.open()
        policy = reopened.current_policy(job_id)
        self.assertEqual(policy["standing_grant_state"], "versioned")
        self.assertEqual(
            recovery.standing_grants_value(policy)["effects"]["commit"]["max"], 2
        )


class DeltaIdentityRepairTests(unittest.TestCase):
    """Task 3.5 / 7.2: the repair derives identity from the canonical spec."""

    CANONICAL = (
        "### Requirement: Widget Rotation\n\n"
        "The system SHALL rotate widgets once per interval.\n"
    )
    DELTA = (
        "## MODIFIED Requirements\n\n"
        "### Requirement: Widget Spin\n\n"
        "The system SHALL rotate widgets once per interval.\n"
    )

    def test_corrected_identity_matches_the_canonical_requirement(self) -> None:
        result = recovery.repair_delta_modified_identity(self.DELTA, self.CANONICAL)
        self.assertTrue(result["changed"])
        self.assertEqual(result["original_identity"], "Widget Spin")
        self.assertEqual(result["corrected_identity"], "Widget Rotation")
        self.assertIn("### Requirement: Widget Rotation", result["delta"])
        # Canonical intent is preserved and the canonical text is never rewritten.
        self.assertTrue(result["canonical_intent_preserved"])
        self.assertTrue(result["canonical_spec_unchanged"])
        self.assertNotIn("Widget Spin", result["delta"])


class WorktreePreservationTests(unittest.TestCase):
    """Task 3.6 / 7.2: unrelated tracked, staged, and untracked work is intact."""

    def _state(self) -> dict:
        return {
            "available": True,
            "tracked": {"src/app.py": "M", "notes.md": "M"},
            "staged": {"src/other.py": "M"},
            "untracked": ["scratch.md"],
        }

    def test_unrelated_work_is_preserved_when_only_authorized_paths_change(self) -> None:
        before = self._state()
        after = {
            "available": True,
            "tracked": {"src/app.py": "M", "notes.md": "M", "src/fix.py": "M"},
            "staged": {"src/other.py": "M"},
            "untracked": ["scratch.md"],
        }
        decision = recovery.verify_worktree_preserved(
            before, after, authorized_paths=["src/fix.py"]
        )
        self.assertTrue(decision["preserved"])

    def test_lost_unrelated_work_is_detected(self) -> None:
        before = self._state()
        after = {
            "available": True,
            "tracked": {},
            "staged": {},
            "untracked": [],
        }
        decision = recovery.verify_worktree_preserved(
            before, after, authorized_paths=["src/fix.py"]
        )
        self.assertFalse(decision["preserved"])
        self.assertTrue(decision["violations"])


class RecoveryOrchestrationTests(IncidentRecoveryTestCase):
    """Task 3.1 / 3.2 / 7.3: chosen remedy, fixer/verifier, independent gate."""

    def _recovering_incident(self, handle: ledger.Ledger, job_id: int) -> int:
        incident_id = handle.record_incident(
            job_id, kind="dirty_worktree", signature="sig-1"
        )
        handle.transition_incident(incident_id, target_state="recovering")
        return incident_id

    def test_begin_recovery_journals_the_primary_chosen_remedy(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        action_id = handle.begin_action(job_id, kind="implement", run_id="run-1")
        result = recovery.begin_recovery(
            handle,
            job_id=job_id,
            change_id="change-a",
            stage="implement",
            failure={"failure_class": "dirty_worktree", "message": "dirty"},
            policy=_policy(),
            remedy="repair_worktree_preserving",
            action_id=action_id,
        )
        self.assertEqual(result["status"], "recovering")
        evidence = handle.list_evidence(action_id)
        self.assertEqual([entry["kind"] for entry in evidence], ["remedy_choice"])
        self.assertEqual(handle.get_incident(result["incident_id"])["state"], "recovering")

    def test_out_of_set_remedy_is_a_recorded_policy_violation(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        with self.assertRaises(recovery.RemedyPolicyViolation):
            recovery.begin_recovery(
                handle,
                job_id=job_id,
                change_id="change-a",
                stage="implement",
                failure={"failure_class": "permanent_provider"},
                policy=_policy(),
                remedy="retry_transient",
            )
        kinds = [row["kind"] for row in handle.list_incidents(job_id)]
        self.assertIn("policy_violation", kinds)

    def test_dispatch_repair_dispatches_the_fixer_then_the_verifier(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        calls: list[tuple[str, dict]] = []

        def dispatch(role, payload):
            calls.append((role, payload))
            return {"role": role, "session_id": f"{role}-session"}

        result = recovery.dispatch_repair(
            handle,
            job_id=job_id,
            change_id="change-a",
            stage="implement",
            incident_id=self._recovering_incident(handle, job_id),
            dispatch=dispatch,
        )
        self.assertEqual([role for role, _ in calls], ["fixer", "verifier"])
        self.assertEqual(result["fixer_session_id"], "fixer-session")
        self.assertEqual(result["verifier_session_id"], "verifier-session")

    def test_fixer_claim_alone_does_not_consume_a_repair(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        incident_id = self._recovering_incident(handle, job_id)
        with self.assertRaises(agent_contracts.RepairConsumptionError):
            recovery.consume_recovery_effect(
                handle,
                job_id=job_id,
                incident_id=incident_id,
                change_id="change-a",
                effect="commit",
                policy=_granted_policy(commit=1),
                verdict=None,
                fixer_report={"repair": "repaired", "session_id": "fixer-1"},
                fixer_session_id="fixer-1",
            )

    def test_same_session_verdict_is_not_independent(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        incident_id = self._recovering_incident(handle, job_id)
        with self.assertRaises(agent_contracts.RepairConsumptionError):
            recovery.consume_recovery_effect(
                handle,
                job_id=job_id,
                incident_id=incident_id,
                change_id="change-a",
                effect="commit",
                policy=_granted_policy(commit=1),
                verdict={
                    "verdict": "pass",
                    "repair_verified": True,
                    "diff_reviewed": True,
                    "session_id": "fixer-1",
                },
                fixer_report={"repair": "repaired", "session_id": "fixer-1"},
                fixer_session_id="fixer-1",
            )

    def test_verified_diff_unlocks_a_covered_effect(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        incident_id = self._recovering_incident(handle, job_id)
        result = recovery.consume_recovery_effect(
            handle,
            job_id=job_id,
            incident_id=incident_id,
            change_id="change-a",
            effect="commit",
            policy=_granted_policy(commit=1),
            verdict={
                "verdict": "pass",
                "repair_verified": True,
                "diff_reviewed": True,
                "session_id": "verifier-1",
            },
            fixer_report={"repair": "repaired", "session_id": "fixer-1"},
            fixer_session_id="fixer-1",
        )
        self.assertTrue(result["authorized"])
        self.assertEqual(recovery.grant_effect_consumption(handle, job_id, "commit"), 1)

    def test_absent_grant_escalates_a_verified_effect(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        incident_id = self._recovering_incident(handle, job_id)
        with self.assertRaises(recovery.RecoveryBlocked):
            recovery.consume_recovery_effect(
                handle,
                job_id=job_id,
                incident_id=incident_id,
                change_id="change-a",
                effect="commit",
                policy=_policy(),
                verdict={
                    "verdict": "pass",
                    "repair_verified": True,
                    "diff_reviewed": True,
                    "session_id": "verifier-1",
                },
                fixer_report={"repair": "repaired", "session_id": "fixer-1"},
                fixer_session_id="fixer-1",
            )
        self.assertEqual(handle.get_incident(incident_id)["state"], "escalated")

    def test_partial_archive_routes_to_a_fresh_review_without_completion(self) -> None:
        plan = recovery.partial_archive_recovery(
            change_id="change-a", archive_complete=False, fast_check_ok=True
        )
        self.assertEqual(plan["path"], recovery.PATH_FRESH_REVIEW)
        self.assertTrue(plan["fresh_review_required"])
        self.assertFalse(plan["completion_asserted"])
        self.assertFalse(plan["archive_treated_as_done"])


class BoundedRecoveryTests(IncidentRecoveryTestCase):
    """Task 3.4 / 3.9 / 7.4: identical loops are bounded and runtime defects block."""

    def test_identical_loops_are_bounded_and_survive_reopen(self) -> None:
        handle = self.open()
        job_id = self.register(
            handle,
            policy=_policy(
                budgets={**_policy()["budgets"], "max_incident_attempts": 2}
            ),
        )
        policy = handle.current_policy(job_id)
        signature = budgets.incident_signature(
            kind="transient_provider", change_id="change-a", stage="implement"
        )
        incident_id = handle.record_incident(
            job_id, kind="transient_provider", signature=signature
        )
        handle.transition_incident(incident_id, target_state="recovering")
        recovery.bounded_recovery_attempt(
            handle, job_id=job_id, signature=signature, policy=policy
        )
        recovery.bounded_recovery_attempt(
            handle, job_id=job_id, signature=signature, policy=policy
        )
        handle.close()

        reopened = self.open()
        self.assertEqual(reopened.incident_attempt_count(job_id, signature), 2)
        with self.assertRaises(recovery.BoundedRecoveryExceeded):
            recovery.bounded_recovery_attempt(
                reopened, job_id=job_id, signature=signature, policy=policy
            )

    def test_transient_retry_only_retries_transient_failures(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        attempts = {"transient": 0, "permanent": 0}

        def transient_op() -> str:
            attempts["transient"] += 1
            if attempts["transient"] < 2:
                raise TimeoutError("upstream timeout")
            return "ok"

        result = recovery.bounded_transient_retry(
            handle, job_id=job_id, signature="sig", operation=transient_op, sleep=lambda _s: None
        )
        self.assertEqual(result, "ok")
        self.assertEqual(attempts["transient"], 2)
        self.assertGreaterEqual(handle.incident_attempt_count(job_id, "sig"), 1)

        def permanent_op() -> str:
            attempts["permanent"] += 1
            raise PermissionError("authentication failed")

        with self.assertRaises(PermissionError):
            recovery.bounded_transient_retry(
                handle,
                job_id=job_id,
                signature="sig-2",
                operation=permanent_op,
                sleep=lambda _s: None,
            )
        self.assertEqual(attempts["permanent"], 1)

    def test_runtime_defect_becomes_an_operator_blocker_with_no_self_repair(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        result = recovery.begin_recovery(
            handle,
            job_id=job_id,
            change_id="change-a",
            stage="implement",
            failure={"failure_class": "runtime_defect", "message": "no module named x"},
            policy=_policy(),
        )
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["self_repaired"])
        self.assertTrue(result["escalated"])
        self.assertEqual(handle.get_incident(result["incident_id"])["state"], "escalated")
        # Structural guarantee: no API edits, reloads, or deploys the service.
        self.assertEqual(recovery.self_repair_surface(), ())
        forbidden = ("reload", "redeploy", "deploy", "install", "edit_installed")
        self.assertEqual(
            [
                name
                for name in dir(recovery)
                if any(marker in name.lower() for marker in forbidden)
            ],
            [],
        )


class ChooseRemedyEndpointTests(IncidentRecoveryTestCase):
    """Task 4.1 / 4.2 / 4.3: the scoped endpoint verb and its journaled refusals."""

    def setUp(self) -> None:
        super().setUp()
        self.handle = self.open()
        self.principal = "prin"
        self.job_id = self.register(self.handle, owner_principal=self.principal)
        self.handle.set_job_state(self.job_id, "active")
        self.credentials = endpoints.PeerCredentials(
            pid=1, uid=1000, gid=1000
        )
        self.action_id = self.handle.begin_action(
            self.job_id, kind="implement", run_id="run-1"
        )

    def _request(self, **overrides: object) -> dict:
        request = {
            "ledger": self.handle,
            "job_id": self.job_id,
            "role": "implementer",
            "observed_agent": "opsx-implementer",
            "service_identity": self.principal,
            "action_id": self.action_id,
        }
        request.update(overrides)
        return request

    def _incident(self, kind: str = "transient_provider") -> int:
        return self.handle.record_incident(
            self.job_id, kind=kind, signature=f"{kind}-sig"
        )

    def test_allowed_choice_is_journaled_before_the_repair(self) -> None:
        incident_id = self._incident()
        result = endpoints._worker_choose_remedy(
            self._request(incident_id=incident_id, remedy="retry_transient"),
            self.credentials,
        )
        self.assertTrue(result["journaled"])
        evidence = self.handle.list_evidence(self.action_id)
        self.assertEqual([entry["kind"] for entry in evidence], ["remedy_choice"])

    def test_out_of_set_choice_is_refused_with_a_recorded_violation(self) -> None:
        incident_id = self._incident("permanent_provider")
        with self.assertRaises(Exception) as caught:
            endpoints._worker_choose_remedy(
                self._request(incident_id=incident_id, remedy="retry_transient"),
                self.credentials,
            )
        self.assertEqual(type(caught.exception).__name__, "BrokerMediationError")
        kinds = [row["kind"] for row in self.handle.list_incidents(self.job_id)]
        self.assertIn("policy_violation", kinds)
        self.assertEqual(self.handle.list_evidence(self.action_id), [])

    def test_incident_of_another_job_is_refused(self) -> None:
        other = self.repo / "other"
        other.mkdir()
        (other / ".git").mkdir()
        other_worktree = other / "worktree"
        other_worktree.mkdir()
        job2 = self.handle.register_job(
            run_id="run-2",
            worktree=other_worktree,
            owner="service",
            policy=_policy(),
            operator="operator",
            manifest_content="[[changes]]\nid = \"change-a\"\n",
        )
        foreign_incident = self.handle.record_incident(job2, kind="transient_provider")
        with self.assertRaises(Exception):
            endpoints._worker_choose_remedy(
                self._request(incident_id=foreign_incident, remedy="retry_transient"),
                self.credentials,
            )
        self.assertEqual(self.handle.list_evidence(self.action_id), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
