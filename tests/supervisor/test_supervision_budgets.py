"""Tests for the supervision budget contract and reservation engine.

Covers task groups 1-6 and 10 of ``add-supervision-budgets``:

- versioned budgets/deadlines payload validation and legacy classification;
- reservation durable before dispatch, reconciliation without double billing,
  deduplication of duplicate results;
- retention of unknown/interrupted reservations;
- per-action and total limit enforcement with named errors;
- consumption and attempt-signature survival across a simulated reset;
- bounded identical-incident refusal;
- human-wait duration excluded from execution elapsed;
- operator-only increases (worker/reset paths refused, increases forward-only);
- bounded backoff with recorded retries;
- supervisor role budget-counted; legacy unregistered runs untouched.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from lib.supervisor import budgets, ledger, model_policy


def _budget_payload(**overrides: object) -> dict:
    base = {
        "version": budgets.BUDGET_SCHEMA_VERSION,
        "total_cost_usd": None,
        "per_action_cost_usd": None,
        "total_elapsed_minutes": None,
        "per_action_elapsed_minutes": None,
        "max_incident_attempts": None,
    }
    base.update(overrides)
    return base


def _deadline_payload(**overrides: object) -> dict:
    base = {
        "version": budgets.BUDGET_SCHEMA_VERSION,
        "execution_deadline_minutes": None,
    }
    base.update(overrides)
    return base


def _selection() -> dict:
    roles = {
        "supervisor": "frontier/supervisor",
        "supervised_author": "cheap/author",
        "implementer": "cheap/implementer",
        "reviewer": "cheap/reviewer",
        "archiver": "cheap/archiver",
        "acceptance_reviewer": "cheap/acceptance",
        "fixer": "cheap/fixer",
        "verifier": "cheap/verifier",
        "implementer_escalation": "cheap/escalation",
    }
    stages = {
        "create": "supervised_author",
        "implement": "implementer",
        "review": "reviewer",
        "archive": "archiver",
        "acceptance": "acceptance_reviewer",
        "fix": "fixer",
        "verify": "verifier",
        "escalate": "implementer_escalation",
    }
    return {"version": model_policy.MODEL_POLICY_VERSION, "roles": roles, "stages": stages}


def _policy(*, budgets_payload: dict | None = None,
            deadlines_payload: dict | None = None) -> dict:
    return {
        "authority_config": {"mode": "policy-bound"},
        "model_selection": _selection(),
        "inexpensive_allowlist": {
            "version": model_policy.MODEL_POLICY_VERSION,
            "models": [
                "cheap/author", "cheap/implementer", "cheap/reviewer",
                "cheap/archiver", "cheap/acceptance", "cheap/fixer",
                "cheap/verifier", "cheap/escalation",
            ],
            "source": "test fixture",
        },
        "manifest_snapshot_hash": "deadbeef",
        "budgets": budgets_payload
        if budgets_payload is not None
        else _budget_payload(total_cost_usd=10.0),
        "deadlines": deadlines_payload
        if deadlines_payload is not None
        else _deadline_payload(execution_deadline_minutes=60.0),
    }


class PayloadValidationTests(unittest.TestCase):
    def test_budgets_round_trip(self) -> None:
        payload = _budget_payload(total_cost_usd=5.0, max_incident_attempts=2)
        self.assertEqual(budgets.validate_budgets(payload), payload)
        self.assertEqual(budgets.encode_budgets(payload), payload)

    def test_budgets_rejects_missing_version(self) -> None:
        with self.assertRaises(budgets.BudgetShapeError):
            budgets.validate_budgets({"total_cost_usd": 1.0})

    def test_budgets_rejects_missing_declared_keys(self) -> None:
        full = _budget_payload(total_cost_usd=1.0)
        for field in budgets.BUDGET_FIELDS:
            partial = {key: value for key, value in full.items() if key != field}
            with self.assertRaises(budgets.BudgetShapeError) as ctx:
                budgets.validate_budgets(partial)
            self.assertIn(field, str(ctx.exception))

    def test_budgets_accepts_attempt_only_policy(self) -> None:
        payload = _budget_payload(max_incident_attempts=2)
        self.assertEqual(budgets.validate_budgets(payload), payload)

    def test_deadlines_rejects_missing_declared_keys(self) -> None:
        with self.assertRaises(budgets.BudgetShapeError) as ctx:
            budgets.validate_deadlines({"version": budgets.BUDGET_SCHEMA_VERSION})
        self.assertIn("execution_deadline_minutes", str(ctx.exception))

    def test_budgets_rejects_newer_version(self) -> None:
        with self.assertRaises(budgets.BudgetVersionError):
            budgets.validate_budgets(_budget_payload(version=2))

    def test_budgets_rejects_all_null_limits(self) -> None:
        with self.assertRaises(budgets.BudgetShapeError):
            budgets.validate_budgets(_budget_payload())

    def test_budgets_rejects_non_object(self) -> None:
        for bad in ([], "x", 3, None):
            with self.assertRaises(budgets.BudgetShapeError):
                budgets.validate_budgets(bad)  # type: ignore[arg-type]

    def test_budgets_rejects_wrong_types(self) -> None:
        with self.assertRaises(budgets.BudgetShapeError):
            budgets.validate_budgets(_budget_payload(total_cost_usd="lots"))
        with self.assertRaises(budgets.BudgetShapeError):
            budgets.validate_budgets(_budget_payload(max_incident_attempts=1.5))
        with self.assertRaises(budgets.BudgetShapeError):
            budgets.validate_budgets(_budget_payload(max_incident_attempts=True))

    def test_budgets_rejects_unknown_key(self) -> None:
        payload = _budget_payload(total_cost_usd=1.0)
        payload["tokens"] = 100
        with self.assertRaises(budgets.BudgetShapeError):
            budgets.validate_budgets(payload)

    def test_deadlines_round_trip(self) -> None:
        payload = _deadline_payload(execution_deadline_minutes=30.0)
        self.assertEqual(budgets.validate_deadlines(payload), payload)

    def test_deadlines_rejects_wait_budget_key(self) -> None:
        payload = _deadline_payload()
        payload["wall_seconds"] = 3600
        with self.assertRaises(budgets.BudgetShapeError):
            budgets.validate_deadlines(payload)

    def test_decoders_classify_legacy_unversioned(self) -> None:
        legacy = {"tokens": 100, "incidents": 3}
        decoded = budgets.decode_budgets(legacy)
        self.assertEqual(decoded["state"], "legacy_unversioned")
        self.assertEqual(decoded["payload"], legacy)
        decoded_dl = budgets.decode_deadlines({"wall_seconds": 60})
        self.assertEqual(decoded_dl["state"], "legacy_unversioned")

    def test_decoders_versioned_and_newer(self) -> None:
        decoded = budgets.decode_budgets(_budget_payload(total_cost_usd=1.0))
        self.assertEqual(decoded["state"], "versioned")
        with self.assertRaises(budgets.BudgetVersionError):
            budgets.decode_budgets(_budget_payload(version=9))

    def test_budget_policy_state_mapping(self) -> None:
        state = budgets.budget_policy_state(_policy())
        self.assertEqual(state, {"budgets": "versioned", "deadlines": "versioned"})

    def test_legacy_policy_fails_closed(self) -> None:
        policy = _policy()
        policy["budgets"] = {"tokens": 1000}
        reason = budgets.policy_block_reason(policy)
        self.assertIsNotNone(reason)
        self.assertIn("legacy_unversioned", reason)
        with self.assertRaises(budgets.BudgetShapeError):
            budgets.enforce_policy(policy)

    def test_constants_are_conservative_defaults(self) -> None:
        self.assertEqual(budgets.BUDGET_SCHEMA_VERSION, 1)
        self.assertGreater(budgets.DEFAULT_TOKEN_CAP_ENVELOPE, 0)
        self.assertGreaterEqual(budgets.HEADROOM_MULTIPLIER, 1.0)
        self.assertEqual(
            budgets.RETAINING_OBSERVATION_STATES, ("unknown", "interrupted")
        )
        self.assertEqual(
            budgets.RESERVATION_STATES, model_policy.RESERVATION_STATES
        )
        self.assertEqual(
            budgets.OBSERVATION_STATES, model_policy.OBSERVATION_STATES
        )

    def test_no_runtime_package_imports(self) -> None:
        import ast

        source = Path(budgets.__file__).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("lib."), alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("lib.supervisor"):
                    continue
                self.assertFalse(node.module.startswith("lib."), node.module)


class RetentionAndEstimateTests(unittest.TestCase):
    def test_retention_classification(self) -> None:
        self.assertTrue(budgets.observation_retains("unknown"))
        self.assertTrue(budgets.observation_retains("interrupted"))
        self.assertFalse(budgets.observation_retains("observed"))
        self.assertEqual(budgets.classify_reservation_state("unknown"), "retained")
        self.assertEqual(budgets.classify_reservation_state("interrupted"), "retained")
        self.assertEqual(budgets.classify_reservation_state("observed"), "reconciled")
        with self.assertRaises(budgets.BudgetShapeError):
            budgets.classify_reservation_state("bogus")

    def test_sum_reservations_counts_retained_and_reserved(self) -> None:
        records = [
            {"state": "reconciled", "observed_cost_usd": 1.5,
             "observed_elapsed_minutes": 2.0, "reserved_cost_usd": 9.0,
             "reserved_elapsed_minutes": 9.0},
            {"state": "retained", "reserved_cost_usd": 3.0,
             "reserved_elapsed_minutes": 4.0},
            {"state": "reserved", "reserved_cost_usd": 0.5,
             "reserved_elapsed_minutes": 1.0},
        ]
        totals = budgets.sum_reservations(records)
        self.assertAlmostEqual(totals["cost_usd"], 5.0)
        self.assertAlmostEqual(totals["elapsed_minutes"], 7.0)
        self.assertEqual(totals["reservation_count"], 3)
        # Unknown consumption is retained at its reserved estimate, not free.
        self.assertAlmostEqual(totals["retained_cost_usd"], 3.0)

    def test_reservation_estimate_applies_headroom(self) -> None:
        estimate = budgets.reservation_estimate(2.0)
        self.assertAlmostEqual(
            estimate, 2.0 * (budgets.DEFAULT_TOKEN_CAP_ENVELOPE / 1e6)
            * budgets.HEADROOM_MULTIPLIER
        )

    def test_backoff_schedule_is_capped(self) -> None:
        self.assertEqual(budgets.backoff_delay(1), budgets.BACKOFF_BASE_SECONDS)
        self.assertEqual(budgets.backoff_delay(2), budgets.BACKOFF_BASE_SECONDS * 2)
        self.assertEqual(budgets.backoff_delay(10), budgets.BACKOFF_CAP_SECONDS)
        self.assertEqual(len(budgets.BACKOFF_SCHEDULE), budgets.BACKOFF_MAX_ATTEMPTS)

    def test_incident_signature_is_stable_and_volatile_free(self) -> None:
        a = budgets.incident_signature(kind="review_fail", change_id="c", stage="review")
        b = budgets.incident_signature(kind="review_fail", change_id="c", stage="review")
        c = budgets.incident_signature(kind="review_fail", change_id="c", stage="review",
                                       discriminator="other")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_bounded_backoff_retries_then_raises(self) -> None:
        calls: list[int] = []
        delays: list[float] = []
        attempts = {"n": 0}

        def operation() -> str:
            attempts["n"] += 1
            raise RuntimeError("transient")

        def on_retry(attempt: int, delay: float, exc: BaseException) -> None:
            calls.append(attempt)
            delays.append(delay)

        with self.assertRaises(RuntimeError):
            budgets.run_with_bounded_backoff(
                operation, sleep=lambda _d: None, on_retry=on_retry, max_attempts=3
            )
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(delays, [budgets.backoff_delay(1), budgets.backoff_delay(2)])

    def test_bounded_backoff_succeeds_without_retry(self) -> None:
        self.assertEqual(
            budgets.run_with_bounded_backoff(lambda: "ok", sleep=lambda _d: None),
            "ok",
        )

    def test_blocker_state_is_terminal(self) -> None:
        record = budgets.blocker_state("exhausted", operator_action="raise the limit")
        self.assertEqual(record["state"], "blocked")
        self.assertFalse(record["retryable"])
        self.assertIn("raise", record["operator_action"])


class LedgerTestCase(unittest.TestCase):
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
        }
        params.update(overrides)
        return handle.register_job(**params)

    def action(self, handle: ledger.Ledger, job_id: int, *, kind: str = "implement") -> int:
        action_id = handle.begin_action(job_id, kind=kind, run_id="run-1")
        handle.dispatch_action(action_id)
        return action_id


class ReservationEngineTests(LedgerTestCase):
    def test_reservation_is_durable_before_dispatch(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        action_id = self.action(handle, job_id)
        reservation_id = budgets.reserve(
            handle, job_id=job_id, action_id=action_id, role="implementer",
            requested_model="cheap/implementer", reserved_cost_usd=1.0,
            reserved_elapsed_minutes=5.0, policy=_policy(),
            pricing_catalog_version="test-1",
        )
        row = handle.get_reservation(reservation_id)
        self.assertEqual(row["state"], "reserved")
        self.assertEqual(row["role"], "implementer")
        self.assertAlmostEqual(row["reserved_cost_usd"], 1.0)
        handle.close()
        reopened = self.open()
        self.assertEqual(reopened.get_reservation(reservation_id)["state"], "reserved")

    def test_reservation_write_failure_blocks(self) -> None:
        from unittest import mock

        handle = self.open()
        job_id = self.register(handle)
        action_id = self.action(handle, job_id)
        with mock.patch.object(
            handle, "insert_reservation", side_effect=sqlite3.OperationalError("locked")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                budgets.reserve(
                    handle, job_id=job_id, action_id=action_id, role="implementer",
                    requested_model="cheap/implementer", reserved_cost_usd=1.0,
                    reserved_elapsed_minutes=5.0, policy=_policy(),
                )

    def test_per_action_limit_blocks_oversized_dispatch(self) -> None:
        policy = _policy(budgets_payload=_budget_payload(per_action_cost_usd=0.5))
        handle = self.open()
        job_id = self.register(handle, policy=policy)
        action_id = self.action(handle, job_id)
        with self.assertRaises(budgets.BudgetExhaustedError) as ctx:
            budgets.reserve(
                handle, job_id=job_id, action_id=action_id, role="implementer",
                requested_model="cheap/implementer", reserved_cost_usd=1.0,
                reserved_elapsed_minutes=1.0, policy=policy,
            )
        self.assertIn("per_action_cost_usd", str(ctx.exception))
        # No reservation row was created for the blocked dispatch.
        self.assertEqual(handle.reservations_for_job(job_id), [])

    def test_total_limit_blocks_further_dispatch(self) -> None:
        policy = _policy(budgets_payload=_budget_payload(total_cost_usd=2.0))
        handle = self.open()
        job_id = self.register(handle, policy=policy)
        first = self.action(handle, job_id, kind="implement")
        budgets.reserve(
            handle, job_id=job_id, action_id=first, role="implementer",
            requested_model="cheap/implementer", reserved_cost_usd=1.8,
            reserved_elapsed_minutes=1.0, policy=policy,
        )
        second = self.action(handle, job_id, kind="review")
        with self.assertRaises(budgets.BudgetExhaustedError) as ctx:
            budgets.reserve(
                handle, job_id=job_id, action_id=second, role="reviewer",
                requested_model="cheap/reviewer", reserved_cost_usd=0.5,
                reserved_elapsed_minutes=1.0, policy=policy,
            )
        self.assertIn("total_cost_usd", str(ctx.exception))

    def test_reconcile_replaces_estimate_without_double_charge(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        action_id = self.action(handle, job_id)
        reservation_id = budgets.reserve(
            handle, job_id=job_id, action_id=action_id, role="implementer",
            requested_model="cheap/implementer", reserved_cost_usd=1.0,
            reserved_elapsed_minutes=5.0, policy=_policy(),
        )
        state = budgets.reconcile(
            handle, reservation_id=reservation_id, observation_state="observed",
            observed_input_tokens=100, observed_output_tokens=50,
            observed_cost_usd=0.25, observed_elapsed_minutes=2.0,
        )
        self.assertEqual(state, "reconciled")
        consumption = handle.consumption_for_job(job_id)
        self.assertAlmostEqual(consumption["cost_usd"], 0.25)
        self.assertAlmostEqual(consumption["elapsed_minutes"], 2.0)
        # A duplicate delivery is deduplicated: consumption is unchanged.
        budgets.reconcile(
            handle, reservation_id=reservation_id, observation_state="observed",
            observed_cost_usd=99.0, observed_elapsed_minutes=99.0,
        )
        after = handle.consumption_for_job(job_id)
        self.assertAlmostEqual(after["cost_usd"], 0.25)
        self.assertAlmostEqual(after["elapsed_minutes"], 2.0)

    def test_unknown_and_interrupted_reservations_are_retained(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        first = self.action(handle, job_id, kind="implement")
        res_unknown = budgets.reserve(
            handle, job_id=job_id, action_id=first, role="implementer",
            requested_model="cheap/implementer", reserved_cost_usd=1.0,
            reserved_elapsed_minutes=5.0, policy=_policy(),
        )
        self.assertEqual(
            budgets.reconcile(handle, reservation_id=res_unknown,
                              observation_state="unknown"),
            "retained",
        )
        second = self.action(handle, job_id, kind="review")
        res_interrupted = budgets.reserve(
            handle, job_id=job_id, action_id=second, role="reviewer",
            requested_model="cheap/reviewer", reserved_cost_usd=2.0,
            reserved_elapsed_minutes=3.0, policy=_policy(),
        )
        self.assertEqual(
            budgets.reconcile(handle, reservation_id=res_interrupted,
                              observation_state="interrupted"),
            "retained",
        )
        # Retained reservations still count at their reserved estimate.
        consumption = handle.consumption_for_job(job_id)
        self.assertAlmostEqual(consumption["cost_usd"], 3.0)
        self.assertAlmostEqual(consumption["elapsed_minutes"], 8.0)
        self.assertEqual(consumption["retained_cost_usd"], 3.0)

    def test_retention_is_idempotent(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        action_id = self.action(handle, job_id)
        reservation_id = budgets.reserve(
            handle, job_id=job_id, action_id=action_id, role="implementer",
            requested_model="cheap/implementer", reserved_cost_usd=1.0,
            reserved_elapsed_minutes=5.0, policy=_policy(),
        )
        budgets.reconcile(handle, reservation_id=reservation_id,
                          observation_state="unknown")
        budgets.reconcile(handle, reservation_id=reservation_id,
                          observation_state="unknown")
        self.assertEqual(handle.get_reservation(reservation_id)["state"], "retained")
        self.assertAlmostEqual(handle.consumption_for_job(job_id)["cost_usd"], 1.0)

    def test_one_reservation_per_action(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        action_id = self.action(handle, job_id)
        budgets.reserve(
            handle, job_id=job_id, action_id=action_id, role="implementer",
            requested_model="cheap/implementer", reserved_cost_usd=1.0,
            reserved_elapsed_minutes=5.0, policy=_policy(),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            budgets.reserve(
                handle, job_id=job_id, action_id=action_id, role="implementer",
                requested_model="cheap/implementer", reserved_cost_usd=1.0,
                reserved_elapsed_minutes=5.0, policy=_policy(),
            )

    def test_legacy_policy_blocks_reservation(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        action_id = self.action(handle, job_id)
        legacy = _policy()
        legacy["budgets"] = {"tokens": 1000}
        with self.assertRaises(budgets.BudgetShapeError):
            budgets.reserve(
                handle, job_id=job_id, action_id=action_id, role="implementer",
                requested_model="cheap/implementer", reserved_cost_usd=1.0,
                reserved_elapsed_minutes=5.0, policy=legacy,
            )

    def test_supervisor_role_is_budget_counted(self) -> None:
        # The allowlist-exempt supervisor is not exempt from budget counting.
        handle = self.open()
        job_id = self.register(handle)
        action_id = self.action(handle, job_id, kind="supervisor")
        reservation_id = budgets.reserve(
            handle, job_id=job_id, action_id=action_id, role="supervisor",
            requested_model="frontier/supervisor", reserved_cost_usd=4.0,
            reserved_elapsed_minutes=2.0, policy=_policy(),
        )
        self.assertEqual(handle.get_reservation(reservation_id)["role"], "supervisor")
        self.assertAlmostEqual(handle.consumption_for_job(job_id)["cost_usd"], 4.0)

    def test_bounded_attempts_refusal_survives_reopen(self) -> None:
        policy = _policy(budgets_payload=_budget_payload(
            total_cost_usd=10.0, max_incident_attempts=2
        ))
        handle = self.open()
        job_id = self.register(handle, policy=policy)
        signature = budgets.incident_signature(
            kind="review_fail", change_id="c1", stage="review"
        )
        self.assertEqual(
            budgets.record_incident_attempt(handle, job_id=job_id,
                                            signature=signature, policy=policy),
            1,
        )
        self.assertEqual(
            budgets.record_incident_attempt(handle, job_id=job_id,
                                            signature=signature, policy=policy),
            2,
        )
        with self.assertRaises(budgets.BoundedAttemptsExceededError):
            budgets.record_incident_attempt(handle, job_id=job_id,
                                            signature=signature, policy=policy)
        handle.close()
        # A reset does not touch the external ledger; the bound is durable.
        reopened = self.open()
        self.assertEqual(reopened.incident_attempt_count(job_id, signature), 2)
        with self.assertRaises(budgets.BoundedAttemptsExceededError):
            budgets.record_incident_attempt(reopened, job_id=job_id,
                                            signature=signature, policy=policy)


class ResetSurvivalTests(LedgerTestCase):
    def test_consumption_and_attempts_survive_reset(self) -> None:
        """A simulated ``opsx-plan reset`` rewrites worktree JSON only."""
        handle = self.open()
        job_id = self.register(handle)
        action_id = self.action(handle, job_id)
        reservation_id = budgets.reserve(
            handle, job_id=job_id, action_id=action_id, role="implementer",
            requested_model="cheap/implementer", reserved_cost_usd=1.0,
            reserved_elapsed_minutes=5.0, policy=_policy(),
        )
        budgets.reconcile(
            handle, reservation_id=reservation_id, observation_state="observed",
            observed_cost_usd=0.4, observed_elapsed_minutes=1.5,
        )
        signature = budgets.incident_signature(
            kind="review_fail", change_id="c1", stage="review"
        )
        budgets.record_incident_attempt(handle, job_id=job_id,
                                        signature=signature, policy=_policy())

        # Simulate reset: rewrite only worktree JSON state, close and reopen.
        state_file = self.worktree / ".opsx-plan" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"changes": {}}), encoding="utf-8")
        handle.close()

        reopened = self.open()
        consumption = reopened.consumption_for_job(job_id)
        self.assertAlmostEqual(consumption["cost_usd"], 0.4)
        self.assertAlmostEqual(consumption["elapsed_minutes"], 1.5)
        self.assertEqual(reopened.incident_attempt_count(job_id, signature), 1)
        self.assertEqual(
            len(reopened.reservations_for_job(job_id)), 1,
        )


class OperatorOnlyIncreaseTests(LedgerTestCase):
    def test_operator_increase_applies_forward_only(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        first = self.action(handle, job_id, kind="implement")
        budgets.reserve(
            handle, job_id=job_id, action_id=first, role="implementer",
            requested_model="cheap/implementer", reserved_cost_usd=1.0,
            reserved_elapsed_minutes=1.0, policy=_policy(),
        )
        new_policy = _policy(budgets_payload=_budget_payload(total_cost_usd=50.0))
        budgets.operator_budget_increase(
            handle, job_id=job_id, revision=2, policy=new_policy, operator="operator"
        )
        # Previous consumption is unchanged and the new limit applies forward.
        self.assertEqual(handle.get_reservation(first)["reserved_cost_usd"], 1.0)
        current = handle.current_policy(job_id)
        self.assertEqual(current["budgets"]["total_cost_usd"], 50.0)
        self.assertEqual(handle.policy_revision(job_id, 1)["budgets"]["total_cost_usd"], 10.0)

    def test_worker_path_cannot_revise_without_operator(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        with self.assertRaises(budgets.BudgetShapeError):
            budgets.operator_budget_increase(
                handle, job_id=job_id, revision=2, policy=_policy(), operator=""
            )
        self.assertEqual(len(handle.list_policy_revisions(job_id)), 1)


class ExecutionElapsedTests(LedgerTestCase):
    def test_dispatch_intervals_close_on_completion(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        action_id = self.action(handle, job_id)
        intervals = handle.dispatch_intervals(job_id)
        self.assertEqual(len(intervals), 1)
        self.assertTrue(intervals[0]["open"])
        handle.complete_action(action_id)
        closed = handle.dispatch_intervals(job_id)
        self.assertFalse(closed[0]["open"])
        self.assertIsNotNone(closed[0]["ended_at"])

    def test_execution_deadline_uses_execution_elapsed_only(self) -> None:
        deadlines = budgets.validate_deadlines(
            _deadline_payload(execution_deadline_minutes=10.0)
        )
        # Below the deadline: no block.
        budgets.check_execution_deadline(
            deadlines, execution_elapsed_minutes=5.0
        )
        with self.assertRaises(budgets.BudgetExhaustedError) as ctx:
            budgets.check_execution_deadline(
                deadlines, execution_elapsed_minutes=10.0
            )
        self.assertIn("execution_deadline_minutes", str(ctx.exception))

    def test_human_wait_is_not_a_dispatch_interval(self) -> None:
        """A paused human-wait job accrues no new execution interval."""
        handle = self.open()
        job_id = self.register(handle)
        action_id = self.action(handle, job_id)
        handle.complete_action(action_id)
        # A human wait is durable job state, not a dispatch: no interval is
        # created for it, so it cannot consume the execution deadline.
        handle.set_job_state(job_id, "paused")
        handle.set_job_state(job_id, "active")
        self.assertEqual(len(handle.dispatch_intervals(job_id)), 1)


class MigrationChainTests(LedgerTestCase):
    def test_v1_ledger_migrates_to_head_preserving_rows(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        action_id = handle.begin_action(job_id, kind="implement", run_id="run-1")
        handle.close()

        # Downgrade to a genuine v1 ledger: drop the v2/v3 tables and stamp
        # user_version = 1.
        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP TABLE IF EXISTS incident_attempts")
        conn.execute("DROP TABLE IF EXISTS reservations")
        conn.execute("DROP TABLE IF EXISTS fencing_records")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        migrated = self.open()
        self.assertEqual(migrated.schema_version(), ledger.CURRENT_SCHEMA_VERSION)
        self.assertEqual(migrated.get_job(job_id)["id"], job_id)
        self.assertEqual(migrated.get_action(action_id)["id"], action_id)
        # The new tables exist and are usable after the chain applies.
        self.assertEqual(migrated.reservations_for_job(job_id), [])
        self.assertEqual(migrated.list_incident_attempts(job_id), [])

    def test_policy_swallows_legacy_budgets_and_reports_state(self) -> None:
        handle = self.open()
        job_id = self.register(handle)
        handle.connection.execute("DROP TRIGGER job_policies_guard_update")
        handle.connection.execute(
            "UPDATE job_policies SET budgets = ?, deadlines = ? "
            "WHERE job_id = ? AND is_current = 1",
            (json.dumps({"tokens": 1}), json.dumps({"wall_seconds": 1}), job_id),
        )
        policy = handle.current_policy(job_id)
        self.assertEqual(policy["budget_policy_state"]["budgets"], "legacy_unversioned")
        self.assertEqual(
            policy["budget_policy_state"]["deadlines"], "legacy_unversioned"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
