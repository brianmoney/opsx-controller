"""Contract tests for the durable supervisor ledger (``lib/supervisor``).

These tests pin the storage contract later supervision changes build on:
schema versioning and forward-only migration, protected explicit policy
revisions, durable identities with ``run_id`` linkage, the trusted-location
rule, transactional crash recovery, the journal-before-side-effects ordering,
and the single-supervised-job-per-worktree invariant.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from lib.supervisor import clock, ledger


def _policy(**overrides: object) -> dict:
    base = {
        "authority_config": {"mode": "policy-bound", "approval": "supervisor"},
        "model_selection": {
            "version": 1,
            "roles": {"implementer": "cheap/model-a"},
            "stages": {"implement": "implementer"},
        },
        "inexpensive_allowlist": {
            "version": 1,
            "models": ["cheap/model-a", "cheap/model-b"],
            "source": "repo-local config (/repo/.opsx-plan/models.toml, [allowlist])",
        },
        "manifest_snapshot_hash": "deadbeef",
        "budgets": {
            "version": 1,
            "total_cost_usd": 100.0,
            "per_action_cost_usd": None,
            "total_elapsed_minutes": None,
            "per_action_elapsed_minutes": None,
            "max_incident_attempts": 3,
        },
        "deadlines": {"version": 1, "execution_deadline_minutes": None},
    }
    base.update(overrides)
    return base


class LedgerTestCase(unittest.TestCase):
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


class PersistenceTests(LedgerTestCase):
    def test_open_write_close_reopen_preserves_records(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        action_id = handle.begin_action(job_id, kind="implement", run_id="run-1")
        incident_id = handle.record_incident(job_id, kind="crash", summary="restart")
        policy_before = handle.current_policy(job_id)
        handle.close()

        reopened = self.open()
        job = reopened.get_job(job_id)
        action = reopened.get_action(action_id)
        incident = reopened.get_incident(incident_id)
        policy_after = reopened.current_policy(job_id)

        self.assertEqual(job["id"], job_id)
        self.assertEqual(action["id"], action_id)
        self.assertEqual(incident["id"], incident_id)
        self.assertIsInstance(job_id, int)
        self.assertIsInstance(action_id, int)
        self.assertIsInstance(incident_id, int)

        # Intact job_id references.
        self.assertEqual(action["job_id"], job_id)
        self.assertEqual(incident["job_id"], job_id)
        self.assertEqual(policy_after["job_id"], job_id)

        # Policy records survive reopen unchanged.
        self.assertEqual(policy_after["revision"], policy_before["revision"])
        self.assertEqual(
            policy_after["manifest_snapshot_hash"],
            policy_before["manifest_snapshot_hash"],
        )

    def test_schema_version_recorded_and_matches_runtime(self) -> None:
        handle = self.open()
        self.assertEqual(handle.schema_version(), ledger.CURRENT_SCHEMA_VERSION)
        self.assertEqual(
            handle.pragma("journal_mode").lower(), "wal"
        )
        self.assertEqual(handle.pragma("foreign_keys"), 1)


class MigrationTests(LedgerTestCase):
    def test_older_ledger_migrates_forward_preserving_records(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        action_id = handle.begin_action(job_id, kind="review", run_id="run-1")
        handle.close()

        # Simulate a ledger written by older code at schema version 0.
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        conn.close()

        migrated = self.open()
        self.assertEqual(migrated.schema_version(), ledger.CURRENT_SCHEMA_VERSION)
        self.assertEqual(migrated.get_job(job_id)["id"], job_id)
        self.assertEqual(migrated.get_action(action_id)["id"], action_id)
        self.assertEqual(migrated.current_policy(job_id)["policy_version"],
                         ledger.CURRENT_POLICY_VERSION)

    def test_newer_ledger_raises_without_modifying_file(self) -> None:
        handle = self.open()
        self.register(handle)
        handle.close()

        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()

        before = hashlib.sha256(self.db_path.read_bytes()).digest()
        with self.assertRaises(ledger.LedgerVersionError):
            ledger.open_ledger(self.db_path, repository_root=self.repo)
        after = hashlib.sha256(self.db_path.read_bytes()).digest()
        self.assertEqual(before, after)

    def test_persisted_policy_carries_policy_schema_version(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        policy = handle.current_policy(job_id)
        self.assertEqual(policy["policy_version"], ledger.CURRENT_POLICY_VERSION)
        self.assertEqual(policy["job_id"], job_id)


class ReceiptAndSnapshotSchemaTests(LedgerTestCase):
    """Task 1.3: the v5 receipts/manifest_snapshots migration contract."""

    def test_v4_ledger_migrates_forward_preserving_existing_records(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        action_id = handle.begin_action(job_id, kind="implement", run_id="run-1")
        incident_id = handle.record_incident(job_id, kind="crash", summary="restart")
        handle.close()

        # Simulate a genuine v4 ledger: drop the v5 tables and stamp the prior
        # schema version.
        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP TABLE IF EXISTS manifest_snapshots")
        conn.execute("DROP TABLE IF EXISTS receipts")
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        conn.close()

        migrated = self.open()
        self.assertEqual(migrated.schema_version(), ledger.CURRENT_SCHEMA_VERSION)
        # Every pre-existing record survives the migration intact.
        self.assertEqual(migrated.get_job(job_id)["id"], job_id)
        self.assertEqual(migrated.get_action(action_id)["id"], action_id)
        self.assertEqual(migrated.get_incident(incident_id)["id"], incident_id)
        self.assertEqual(migrated.current_policy(job_id)["job_id"], job_id)
        # The new tables exist and are usable immediately after migration.
        self.assertEqual(migrated.receipts_for_change(job_id, "change-a"), [])
        self.assertEqual(migrated.receipt_high_water(job_id), 0)
        # A job registered before the table existed carries no stored content
        # (it cannot be recovered), but the table is writable now.
        digest = migrated.record_manifest_snapshot(job_id, content="[[changes]]\n")
        self.assertEqual(migrated.manifest_snapshot(job_id, digest), "[[changes]]\n")

    def test_v5_ledger_opened_by_older_code_raises(self) -> None:
        handle = self.open()
        self.register(handle)
        handle.close()

        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
        conn.close()

        with mock.patch.object(ledger, "CURRENT_SCHEMA_VERSION", 4):
            with self.assertRaises(ledger.LedgerVersionError):
                ledger.open_ledger(self.db_path, repository_root=self.repo)

    def test_registration_requires_protected_snapshot_content(self) -> None:
        handle = self.open()
        with self.assertRaises(ledger.LedgerError):
            self.register(handle, manifest_content="")
        with self.assertRaises(ledger.LedgerError):
            self.register(handle, manifest_content=None)
        self.assertEqual(handle.list_jobs(), [])

    def test_registration_records_hash_bound_to_content(self) -> None:
        handle = self.open()
        content = "[[changes]]\nid = \"change-a\"\n"
        job_id = self.register(handle, manifest_content=content)
        policy = handle.current_policy(job_id)
        self.assertEqual(
            policy["manifest_snapshot_hash"], ledger.snapshot_digest(content)
        )
        self.assertEqual(handle.current_manifest_snapshot(job_id), content)

    def test_receipts_are_append_only_and_queryable(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        first = handle.record_receipt(
            job_id, change_id="change-a", kind="approval",
            checkpoint="approval:change-a", material_hash="deadbeef",
            authority="operator", actor_principal="alice",
        )
        second = handle.record_receipt(
            job_id, change_id="change-a", kind="approval",
            checkpoint="approval:change-a", material_hash="cafef00d",
            authority="operator",
        )
        approvals = handle.receipts_for_change(job_id, "change-a", kind="approval")
        self.assertEqual([row["id"] for row in approvals], [first, second])
        self.assertEqual(handle.receipt_high_water(job_id), second)
        self.assertEqual(
            [row["id"] for row in handle.receipts_after(job_id, first)], [second]
        )

    def test_unknown_receipt_kind_or_authority_is_refused(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        for kind in ("bogus", ""):
            with self.assertRaises(ledger.LedgerError):
                handle.record_receipt(
                    job_id, change_id="change-a", kind=kind,
                    checkpoint="c", material_hash="h", authority="operator",
                )
        with self.assertRaises(ledger.LedgerError):
            handle.record_receipt(
                job_id, change_id="change-a", kind="approval",
                checkpoint="c", material_hash="h", authority="root",
            )
        self.assertEqual(handle.receipt_high_water(job_id), 0)

    def test_snapshot_store_is_idempotent_and_hash_addressed(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        content = "[[changes]]\nid = \"change-b\"\n"
        digest = handle.record_manifest_snapshot(job_id, content=content)
        again = handle.record_manifest_snapshot(job_id, content=content)
        self.assertEqual(digest, again)
        self.assertEqual(handle.manifest_snapshot(job_id, digest), content)
        self.assertIsNone(handle.manifest_snapshot(job_id, "missing"))


class CrashRecoveryTests(LedgerTestCase):
    def test_interrupted_multi_record_write_leaves_no_partial_state(self) -> None:
        handle = self.open()

        def interrupt(conn: sqlite3.Connection, job_id: int) -> None:
            raise RuntimeError("simulated interruption")

        with mock.patch.object(ledger, "_after_job_insert", interrupt):
            with self.assertRaises(RuntimeError):
                self.register(handle)

        # Nothing from the interrupted transaction is visible on the live
        # connection, nor on reopen.
        self.assertEqual(handle.list_jobs(), [])
        self.assertEqual(
            handle.connection.execute("SELECT COUNT(*) FROM job_policies").fetchone()[0],
            0,
        )
        handle.close()

        reopened = self.open()
        self.assertEqual(reopened.list_jobs(), [])
        # The ledger is otherwise consistent and usable.
        job_id = self.register(reopened)
        self.assertEqual(reopened.get_job(job_id)["id"], job_id)

    def test_interrupted_write_leaves_no_partial_record(self) -> None:
        """A mid-transaction failure is rolled back even for writes that span
        the action/intent journal and its evidence."""
        handle = self.open()
        job_id = self.register(handle)
        action_id = handle.begin_action(job_id, kind="implement", run_id="run-1")

        with mock.patch.object(
            handle, "_transaction", side_effect=RuntimeError("interrupted")
        ):
            with self.assertRaises(RuntimeError):
                handle.mark_uncertain(action_id)

        self.assertEqual(handle.get_action(action_id)["state"], "intent")


class TrustedLocationTests(LedgerTestCase):
    def test_repo_internal_path_rejected_and_nothing_created(self) -> None:
        internal = self.repo / "ledger.sqlite3"
        with self.assertRaises(ledger.TrustedLocationError):
            ledger.open_ledger(internal, repository_root=self.repo)
        self.assertFalse(internal.exists())
        self.assertFalse((self.repo / "ledger.sqlite3-wal").exists())

    def test_symlinked_repo_internal_path_rejected(self) -> None:
        internal = self.repo / "ledger.sqlite3"
        link = self.root / "link.sqlite3"
        os.symlink(internal, link)
        with self.assertRaises(ledger.TrustedLocationError):
            ledger.open_ledger(link, repository_root=self.repo)
        self.assertFalse(internal.exists())

    def test_opsx_plan_path_rejected_and_nothing_created(self) -> None:
        opsx_dir = self.repo / ".opsx-plan"
        opsx_dir.mkdir()
        target = opsx_dir / "ledger.sqlite3"
        with self.assertRaises(ledger.TrustedLocationError):
            ledger.open_ledger(target, repository_root=self.repo)
        self.assertFalse(target.exists())

    def test_repository_references_are_stored_relatively(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        job = handle.get_job(job_id)

        self.assertTrue(os.path.isabs(job["repo_root"]))
        self.assertEqual(job["repo_root"], str(self.repo.resolve()))
        self.assertFalse(os.path.isabs(job["worktree_path"]))
        self.assertEqual(job["worktree_path"], "worktree")

        relative = ledger.repository_relative(
            self.worktree / "src" / "main.py", self.repo
        )
        self.assertEqual(relative, os.path.join("worktree", "src", "main.py"))


class ModelPolicyLedgerTests(LedgerTestCase):
    """The ledger routes policy model fields through the model-policy module."""

    def test_versioned_policy_round_trips_and_reports_state(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        policy = handle.current_policy(job_id)
        self.assertEqual(policy["model_policy_state"]["model_selection"], "versioned")
        self.assertEqual(
            policy["model_policy_state"]["inexpensive_allowlist"], "versioned"
        )
        self.assertEqual(policy["model_selection"]["version"], 1)
        self.assertEqual(policy["inexpensive_allowlist"]["version"], 1)

    def test_malformed_model_selection_rejected_on_write(self) -> None:
        handle = self.open()
        bad = _policy(model_selection={"implementer": "cheap/model-a"})
        with self.assertRaises(ledger.model_policy.ModelPolicyError):
            self.register(handle, policy=bad)
        self.assertEqual(handle.list_jobs(), [])

    def test_newer_nested_version_rejected_on_write(self) -> None:
        handle = self.open()
        bad = _policy(
            model_selection={
                "version": 99,
                "roles": {"implementer": "cheap/model-a"},
                "stages": {"implement": "implementer"},
            }
        )
        with self.assertRaises(ledger.model_policy.ModelPolicyVersionError):
            self.register(handle, policy=bad)

    def test_newer_nested_version_rejected_on_read(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        # Corrupt the stored nested version via raw SQL. The insert-only guard
        # trigger is dropped for this raw fixture write (it is recreated on
        # reopen), so the version mismatch is what gets detected at decode.
        handle.connection.execute("DROP TRIGGER job_policies_guard_update")
        handle.connection.execute(
            "UPDATE job_policies SET model_selection = ? WHERE job_id = ? "
            "AND is_current = 1",
            (
                json_dumps(
                    {
                        "version": 42,
                        "roles": {"implementer": "x"},
                        "stages": {"implement": "implementer"},
                    }
                ),
                job_id,
            ),
        )
        with self.assertRaises(ledger.model_policy.ModelPolicyVersionError):
            handle.current_policy(job_id)

    def test_raw_legacy_row_reads_as_legacy_unversioned(self) -> None:
        """A pre-schema row (no nested version key) is readable as-is and
        tagged legacy_unversioned for both old payload shapes."""
        handle = self.open()
        job_id = self.register(handle)
        # Overwrite with pre-policy shapes: a bare dict model_selection and the
        # old list-shaped allowlist, both without a version key.
        handle.connection.execute("DROP TRIGGER job_policies_guard_update")
        handle.connection.execute(
            "UPDATE job_policies SET model_selection = ?, inexpensive_allowlist = ? "
            "WHERE job_id = ? AND is_current = 1",
            (
                json_dumps({"implementer": "cheap/model-a"}),
                json_dumps(["cheap/model-a", "cheap/model-b"]),
                job_id,
            ),
        )
        policy = handle.current_policy(job_id)
        self.assertEqual(
            policy["model_policy_state"]["model_selection"], "legacy_unversioned"
        )
        self.assertEqual(
            policy["model_policy_state"]["inexpensive_allowlist"],
            "legacy_unversioned",
        )
        # The original values are preserved unmodified.
        self.assertEqual(policy["model_selection"], {"implementer": "cheap/model-a"})
        self.assertEqual(
            policy["inexpensive_allowlist"], ["cheap/model-a", "cheap/model-b"]
        )

    def test_nested_version_is_distinct_from_outer_policy_version(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        policy = handle.current_policy(job_id)
        outer = policy["policy_version"]
        nested = policy["model_selection"]["version"]
        self.assertEqual(outer, ledger.CURRENT_POLICY_VERSION)
        self.assertEqual(nested, ledger.model_policy.MODEL_POLICY_VERSION)
        # Both coincide today but are validated independently.
        self.assertEqual(outer, nested)

    def test_revision_preserves_versioned_policy(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        handle.revise_policy(
            job_id,
            revision=2,
            policy=_policy(
                model_selection={
                    "version": 1,
                    "roles": {"reviewer": "cheap/model-c"},
                    "stages": {"review": "reviewer"},
                },
                inexpensive_allowlist={
                    "version": 1,
                    "models": ["cheap/model-c"],
                    "source": "user-global config (/home/u/models.toml, [allowlist])",
                },
            ),
            operator="operator",
        )
        current = handle.current_policy(job_id)
        self.assertEqual(
            current["model_selection"]["roles"]["reviewer"], "cheap/model-c"
        )
        self.assertEqual(current["model_policy_state"]["model_selection"], "versioned")


class PolicyRevisionTests(LedgerTestCase):
    def test_initial_revision_has_all_required_fields(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        policy = handle.current_policy(job_id)
        self.assertEqual(policy["revision"], 1)
        self.assertEqual(policy["policy_version"], ledger.CURRENT_POLICY_VERSION)
        for field in ledger.REQUIRED_POLICY_FIELDS:
            self.assertIn(field, policy)
        self.assertEqual(policy["authority_config"]["mode"], "policy-bound")

    def test_explicit_revision_supersedes_and_prior_retrievable(self) -> None:
        handle = self.open()
        job_id = self.register(handle)

        handle.revise_policy(
            job_id,
            revision=2,
            policy=_policy(budgets={
                "version": 1,
                "total_cost_usd": 5.0,
                "per_action_cost_usd": None,
                "total_elapsed_minutes": None,
                "per_action_elapsed_minutes": None,
                "max_incident_attempts": 1,
            }),
            operator="operator",
        )

        current = handle.current_policy(job_id)
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["budgets"]["total_cost_usd"], 5.0)

        prior = handle.policy_revision(job_id, 1)
        self.assertEqual(prior["revision"], 1)
        self.assertEqual(prior["budgets"]["max_incident_attempts"], 3)
        self.assertEqual(len(handle.list_policy_revisions(job_id)), 2)

    def test_non_incrementing_revision_rejected_and_unchanged(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        before = handle.current_policy(job_id)

        for bad_revision in (1, 3, 0):
            with self.assertRaises(ledger.PolicyRevisionError):
                handle.revise_policy(
                    job_id,
                    revision=bad_revision,
                    policy=_policy(budgets={
                        "version": 1,
                        "total_cost_usd": 1.0,
                        "per_action_cost_usd": None,
                        "total_elapsed_minutes": None,
                        "per_action_elapsed_minutes": None,
                        "max_incident_attempts": None,
                    }),
                    operator="operator",
                )

        self.assertEqual(handle.current_policy(job_id), before)
        self.assertEqual(len(handle.list_policy_revisions(job_id)), 1)

    def test_in_place_policy_mutation_is_rejected_by_guard(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        with self.assertRaises(sqlite3.IntegrityError):
            handle.connection.execute(
                "UPDATE job_policies SET budgets = ? WHERE job_id = ? AND is_current = 1",
                (json_dumps({"version": 1}), job_id),
            )


class JobInvariantTests(LedgerTestCase):
    def test_second_job_for_same_worktree_refused_and_existing_unchanged(self) -> None:
        handle = self.open()
        first = self.register(handle)
        before = dict(handle.get_job(first))

        with self.assertRaises(ledger.DuplicateJobError):
            self.register(handle, run_id="run-2")

        after = dict(handle.get_job(first))
        self.assertEqual(before, after)
        self.assertEqual(len(handle.list_jobs()), 1)

    def test_second_job_allowed_after_first_is_terminal(self) -> None:
        handle = self.open()
        first = self.register(handle)
        handle.set_job_state(first, "completed")
        second = self.register(handle, run_id="run-2")
        self.assertNotEqual(first, second)


class IdentityLinkageTests(LedgerTestCase):
    def test_run_id_stored_as_data_distinct_from_record_ids(self) -> None:
        handle = self.open()
        job_id = self.register(handle, run_id="run-xyz")
        action_id = handle.begin_action(job_id, kind="implement", run_id="run-xyz")

        job = handle.get_job(job_id)
        action = handle.get_action(action_id)

        self.assertEqual(job["run_id"], "run-xyz")
        self.assertEqual(action["run_id"], "run-xyz")
        self.assertNotEqual(job["run_id"], str(job_id))
        self.assertNotEqual(action["run_id"], str(action_id))
        self.assertEqual(action["job_id"], job_id)
        self.assertEqual(
            [row["id"] for row in handle.query_actions_by_run("run-xyz")],
            [action_id],
        )


class JournalTests(LedgerTestCase):
    def test_intent_is_durable_after_interruption_before_side_effect(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        action_id = handle.begin_action(job_id, kind="implement", run_id="run-1")
        # Simulate interruption between the committed intent and the side
        # effect by closing without dispatching.
        handle.close()

        reopened = self.open()
        action = reopened.get_action(action_id)
        self.assertEqual(action["state"], "intent")
        self.assertEqual(action["job_id"], job_id)
        self.assertEqual(action["kind"], "implement")

    def test_uncertain_action_blocks_completion_and_replay_until_evidence(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        action_id = handle.begin_action(job_id, kind="implement", run_id="run-1")
        handle.dispatch_action(action_id, session_id="session-1", process_id="pid-1")
        handle.mark_uncertain(action_id, detail="no confirmation")

        self.assertEqual(handle.get_action(action_id)["state"], "uncertain")
        with self.assertRaises(ledger.JournalStateError):
            handle.complete_action(action_id)
        with self.assertRaises(ledger.JournalStateError):
            handle.replay_action(action_id)

        handle.record_evidence(action_id, kind="probe", payload={"effect": False})
        self.assertEqual(handle.get_action(action_id)["state"], "reconciled")
        # Only after reconciling evidence may it be completed or replayed.
        handle.complete_action(action_id)
        self.assertEqual(handle.get_action(action_id)["state"], "completed")

    def test_replay_after_reconciliation_records_new_dispatch(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        action_id = handle.begin_action(job_id, kind="implement", run_id="run-1")
        handle.dispatch_action(action_id)
        handle.mark_uncertain(action_id)
        handle.record_evidence(action_id, kind="probe", payload={"observed": True})

        handle.replay_action(action_id, session_id="session-2")
        rows = handle.connection.execute(
            "SELECT COUNT(*) FROM dispatches WHERE action_id = ?", (action_id,)
        ).fetchone()[0]
        # No exactly-once claim: replay is a fresh, observable dispatch.
        self.assertEqual(rows, 2)

    def test_dispatched_action_cannot_be_dispatched_before_intent(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        action_id = handle.begin_action(job_id, kind="implement", run_id="run-1")
        handle.dispatch_action(action_id)
        handle.complete_action(action_id)
        with self.assertRaises(ledger.JournalStateError):
            handle.dispatch_action(action_id)
        with self.assertRaises(ledger.JournalStateError):
            handle.complete_action(action_id)

    def test_uncertain_cannot_fail_without_evidence_then_replay(self) -> None:
        """Regression: uncertainty cannot be laundered into a failed terminal
        state and then replayed without a reconciling evidence row."""
        handle = self.open()
        job_id = self.register(handle)
        action_id = handle.begin_action(job_id, kind="implement", run_id="run-1")
        handle.dispatch_action(action_id)
        handle.mark_uncertain(action_id, detail="no confirmation")

        with self.assertRaises(ledger.JournalStateError):
            handle.fail_action(action_id)
        # The rejected transition changed nothing: still uncertain, not failed.
        self.assertEqual(handle.get_action(action_id)["state"], "uncertain")

        with self.assertRaises(ledger.JournalStateError):
            handle.replay_action(action_id)
        with self.assertRaises(ledger.JournalStateError):
            handle.dispatch_action(action_id)

        rows = handle.connection.execute(
            "SELECT COUNT(*) FROM dispatches WHERE action_id = ?", (action_id,)
        ).fetchone()[0]
        self.assertEqual(rows, 1)

        # Reconciling evidence is the only path out of uncertainty. Once
        # reconciled, the observed failure may be recorded as terminal.
        handle.record_evidence(action_id, kind="probe", payload={"effect": True})
        self.assertEqual(handle.get_action(action_id)["state"], "reconciled")
        handle.fail_action(action_id, detail="observed failure")
        self.assertEqual(handle.get_action(action_id)["state"], "failed")

    def test_failed_action_is_terminal_for_dispatch_and_replay(self) -> None:
        """Regression: a failed action is terminal; dispatch and replay refuse
        it and record no new dispatch row."""
        handle = self.open()
        job_id = self.register(handle)
        action_id = handle.begin_action(job_id, kind="implement", run_id="run-1")
        handle.dispatch_action(action_id)
        handle.fail_action(action_id)

        with self.assertRaises(ledger.JournalStateError):
            handle.replay_action(action_id)
        with self.assertRaises(ledger.JournalStateError):
            handle.dispatch_action(action_id)
        with self.assertRaises(ledger.JournalStateError):
            handle.fail_action(action_id)
        # Failed cannot be flipped to completed either.
        with self.assertRaises(ledger.JournalStateError):
            handle.complete_action(action_id)

        self.assertEqual(handle.get_action(action_id)["state"], "failed")
        rows = handle.connection.execute(
            "SELECT COUNT(*) FROM dispatches WHERE action_id = ?", (action_id,)
        ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_uncertain_reconciliation_is_required_for_every_exit(self) -> None:
        """An uncertain action cannot be failed, completed, replayed, or
        dispatched; all four transitions are atomic and leave no trace."""
        handle = self.open()
        job_id = self.register(handle)
        action_id = handle.begin_action(job_id, kind="implement", run_id="run-1")
        handle.dispatch_action(action_id)
        handle.mark_uncertain(action_id)
        evidence_before = len(handle.list_evidence(action_id))
        dispatches_before = handle.connection.execute(
            "SELECT COUNT(*) FROM dispatches WHERE action_id = ?", (action_id,)
        ).fetchone()[0]

        for attempt in (
            handle.fail_action,
            handle.complete_action,
            handle.replay_action,
            handle.dispatch_action,
        ):
            with self.assertRaises(ledger.JournalStateError):
                attempt(action_id)

        self.assertEqual(handle.get_action(action_id)["state"], "uncertain")
        self.assertEqual(len(handle.list_evidence(action_id)), evidence_before)
        self.assertEqual(
            handle.connection.execute(
                "SELECT COUNT(*) FROM dispatches WHERE action_id = ?", (action_id,)
            ).fetchone()[0],
            dispatches_before,
        )


class _FakeClock:
    """Deterministic ledger clock: each stamp advances by a fixed step.

    ``advance`` moves the clock without stamping, so a test can simulate wall
    time passing between ledger writes (a dispatch that runs for minutes, or a
    human wait that must not be charged as execution time).
    """

    def __init__(self, start: datetime, step_seconds: float = 1.0) -> None:
        self.now = start
        self.step = timedelta(seconds=step_seconds)

    def __call__(self) -> str:
        stamp = self.now
        self.now += self.step
        return stamp.isoformat(timespec="seconds")

    def advance(self, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


def _elapsed_minutes(started_at: str, ended_at: str) -> float:
    return (
        datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)
    ).total_seconds() / 60.0


class RetainedDispatchIntervalTests(LedgerTestCase):
    """An interrupted or unknown dispatch contributes the time it really ran.

    Regression for the review finding: a retained reservation recorded no
    outcome timestamp, so its interval closed at the action's ``updated_at``
    (still the dispatch time) and the dispatch counted as zero elapsed against
    the execution deadline.
    """

    def _clock(self, step_seconds: float = 1.0) -> _FakeClock:
        fake = _FakeClock(
            datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc),
            step_seconds=step_seconds,
        )
        patcher = mock.patch.object(clock, "utcnow", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def _dispatch(self, handle: ledger.Ledger, job_id: int) -> tuple[int, int]:
        """Reserve before dispatch, mirroring the gate's ordering."""
        action_id = handle.begin_action(job_id, kind="implement", run_id="run-1")
        reservation_id = handle.insert_reservation(
            job_id,
            action_id=action_id,
            role="implementer",
            requested_model="cheap/model-a",
            reserved_cost_usd=1.5,
            reserved_elapsed_minutes=2.0,
        )
        handle.dispatch_action(action_id)
        return action_id, reservation_id

    def test_retained_dispatch_spans_dispatch_through_reconciliation(self) -> None:
        fake = self._clock()
        handle = self.open()
        job_id = self.register(handle)
        action_id, reservation_id = self._dispatch(handle, job_id)
        dispatched_at = handle.get_action(action_id)["dispatched_at"]
        # The dispatch runs for seven minutes and then times out.
        fake.advance(7.0)
        handle.retain_reservation(reservation_id)

        reservation = handle.get_reservation(reservation_id)
        self.assertEqual(reservation["state"], "retained")
        self.assertIsNotNone(reservation["retained_at"])
        intervals = handle.dispatch_intervals(job_id)
        self.assertEqual(len(intervals), 1)
        self.assertFalse(intervals[0]["open"])
        self.assertEqual(intervals[0]["started_at"], dispatched_at)
        self.assertEqual(intervals[0]["ended_at"], reservation["retained_at"])
        self.assertGreaterEqual(
            _elapsed_minutes(intervals[0]["started_at"], intervals[0]["ended_at"]),
            7.0,
        )
        # The action itself never moved, so closing at its ``updated_at`` would
        # have yielded zero elapsed: that is the defect being pinned here.
        self.assertEqual(handle.get_action(action_id)["updated_at"], dispatched_at)

    def test_human_wait_after_reconciliation_is_not_execution_time(self) -> None:
        fake = self._clock()
        handle = self.open()
        job_id = self.register(handle)
        _, reservation_id = self._dispatch(handle, job_id)
        fake.advance(3.0)
        handle.retain_reservation(reservation_id)
        closed = handle.dispatch_intervals(job_id)
        charged = _elapsed_minutes(closed[0]["started_at"], closed[0]["ended_at"])

        # A long human wait follows the retained outcome. It creates no new
        # dispatch, so it is excluded by construction and the already-closed
        # interval does not grow.
        fake.advance(240.0)
        handle.set_job_state(job_id, "paused")
        handle.set_job_state(job_id, "active")
        after = handle.dispatch_intervals(job_id)
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["ended_at"], closed[0]["ended_at"])
        self.assertEqual(
            _elapsed_minutes(after[0]["started_at"], after[0]["ended_at"]), charged
        )

    def test_retain_is_idempotent_and_keeps_the_first_outcome_time(self) -> None:
        fake = self._clock()
        handle = self.open()
        job_id = self.register(handle)
        _, reservation_id = self._dispatch(handle, job_id)
        fake.advance(4.0)
        handle.retain_reservation(reservation_id)
        first = handle.get_reservation(reservation_id)["retained_at"]
        fake.advance(60.0)
        handle.retain_reservation(reservation_id)
        self.assertEqual(handle.get_reservation(reservation_id)["retained_at"], first)
        self.assertEqual(handle.dispatch_intervals(job_id)[0]["ended_at"], first)

    def test_reconciled_reservation_records_no_retained_at(self) -> None:
        fake = self._clock()
        handle = self.open()
        job_id = self.register(handle)
        _, reservation_id = self._dispatch(handle, job_id)
        fake.advance(2.0)
        handle.reconcile_reservation(
            reservation_id, observed_cost_usd=0.5, observed_elapsed_minutes=2.0
        )
        reservation = handle.get_reservation(reservation_id)
        self.assertEqual(reservation["state"], "reconciled")
        self.assertIsNone(reservation["retained_at"])
        self.assertEqual(
            handle.dispatch_intervals(job_id)[0]["ended_at"],
            reservation["reconciled_at"],
        )

    def test_retained_row_without_outcome_time_keeps_prior_behavior(self) -> None:
        """A row written before the column existed still closes as it used to."""
        fake = self._clock()
        handle = self.open()
        job_id = self.register(handle)
        action_id, reservation_id = self._dispatch(handle, job_id)
        fake.advance(5.0)
        handle.retain_reservation(reservation_id)
        handle.connection.execute(
            "UPDATE reservations SET retained_at = NULL WHERE id = ?",
            (reservation_id,),
        )
        handle.connection.commit()
        interval = handle.dispatch_intervals(job_id)[0]
        self.assertFalse(interval["open"])
        self.assertEqual(
            interval["ended_at"], handle.get_action(action_id)["updated_at"]
        )

    def test_retained_estimate_still_counts_against_the_budget(self) -> None:
        fake = self._clock()
        handle = self.open()
        job_id = self.register(handle)
        _, reservation_id = self._dispatch(handle, job_id)
        before = handle.consumption_for_job(job_id)
        fake.advance(6.0)
        handle.retain_reservation(reservation_id)
        after = handle.consumption_for_job(job_id)
        # Stamping the outcome time changes accounting not at all: the
        # retained reservation still carries its reserved estimate.
        self.assertEqual(after["cost_usd"], before["cost_usd"])
        self.assertEqual(after["retained_cost_usd"], 1.5)
        self.assertEqual(after["retained_elapsed_minutes"], 2.0)
        self.assertEqual(after["reserved_cost_usd"], 0.0)
        self.assertEqual(after["reservation_count"], 1)

    @unittest.skipUnless(
        sqlite3.sqlite_version_info >= (3, 35, 0),
        "ALTER TABLE ... DROP COLUMN requires SQLite 3.35+",
    )
    def test_v3_ledger_migrates_forward_and_gains_the_outcome_column(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        action_id, reservation_id = self._dispatch(handle, job_id)
        handle.close()

        # Simulate a genuine v3 ledger: the reservations table without the
        # additive column, stamped at the previous schema version.
        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE reservations DROP COLUMN retained_at")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.close()

        migrated = self.open()
        self.assertEqual(migrated.schema_version(), ledger.CURRENT_SCHEMA_VERSION)
        reservation = migrated.get_reservation(reservation_id)
        self.assertEqual(reservation["action_id"], action_id)
        self.assertEqual(reservation["state"], "reserved")
        self.assertIsNone(reservation["retained_at"])
        self.assertEqual(migrated.get_job(job_id)["id"], job_id)
        migrated.retain_reservation(reservation_id)
        self.assertIsNotNone(migrated.get_reservation(reservation_id)["retained_at"])


def json_dumps(value: object) -> str:
    import json

    return json.dumps(value)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
