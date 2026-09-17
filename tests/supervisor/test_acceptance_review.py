"""Acceptance-stage contract tests.

Covers the acceptance artifact review set, the revision identity and staleness
rule, the three outcomes (``accept``/``fix``/``escalate``) and the transitions
they drive, the created-change check at the reviewed revision, the
fixer -> verifier -> fresh-acceptance route, escalation to the primary, and the
gate separation that keeps acceptance a review outcome rather than an approval
authority.

The loop-level tests drive the real supervised run loop through a registered
job's gate, so the acceptance stage is exercised on the same
``gated_dispatch`` boundary as the other supervised stages.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from lib.supervisor import acceptance
from lib.supervisor import agent_contracts
from lib.supervisor import budgets
from lib.supervisor import ledger
from lib.supervisor import lock as lock_mod
from lib.supervisor import model_policy

SCRIPT = Path(__file__).resolve().parents[2] / "orchestrator" / "opsx-plan.py"


def load_opsx_plan():
    spec = importlib.util.spec_from_file_location("opsx_plan", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["opsx_plan"] = module
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _selection() -> dict:
    return {
        "version": model_policy.MODEL_POLICY_VERSION,
        "roles": {
            "supervisor": "openai/gpt-4o",
            "supervised_author": "openai/gpt-4o",
            "implementer": "openai/gpt-4o",
            "reviewer": "openai/gpt-4o",
            "archiver": "openai/gpt-4o",
            "acceptance_reviewer": "openai/gpt-4o",
            "fixer": "openai/gpt-4o",
            "verifier": "openai/gpt-4o",
            "implementer_escalation": "openai/gpt-4o",
        },
        "stages": {
            "create": "supervised_author",
            "implement": "implementer",
            "review": "reviewer",
            "archive": "archiver",
            "acceptance": "acceptance_reviewer",
            "fix": "fixer",
            "verify": "verifier",
            "escalate": "implementer_escalation",
        },
    }


def _policy(**overrides: object) -> dict:
    base = {
        "authority_config": {"mode": "policy-bound"},
        "model_selection": _selection(),
        "inexpensive_allowlist": {
            "version": model_policy.MODEL_POLICY_VERSION,
            "models": ["openai/gpt-4o"],
            "source": "test fixture",
        },
        "manifest_snapshot_hash": "deadbeef",
        "budgets": {
            "version": budgets.BUDGET_SCHEMA_VERSION,
            "total_cost_usd": 1000.0,
            "per_action_cost_usd": None,
            "total_elapsed_minutes": None,
            "per_action_elapsed_minutes": None,
            "max_incident_attempts": None,
        },
        "deadlines": {
            "version": budgets.BUDGET_SCHEMA_VERSION,
            "execution_deadline_minutes": None,
        },
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Accept ance core module
# ---------------------------------------------------------------------------


class AcceptanceRevisionTests(unittest.TestCase):
    """1.1 / 7.1 — artifact review set, revision identity, staleness."""

    def _set(self, **overrides):
        params = {
            "manifest_snapshot_hash": "manifest-hash",
            "depends_on": ["dep-b", "dep-a"],
            "authored_artifacts": {
                "openspec/changes/c/proposal.md": "proposal",
                "openspec/changes/c/tasks.md": "tasks",
                "openspec/changes/c/design.md": None,
            },
            "spec_deltas": {
                "openspec/changes/c/specs/cap/spec.md": (
                    "## ADDED Requirements\n\n### Requirement: R1\n"
                )
            },
            "canonical_specs": {"openspec/specs/cap/spec.md": "canonical"},
            "tracked_change_files": {"lib/x.py": "impl"},
        }
        params.update(overrides)
        return acceptance.build_review_set(**params)

    def test_revision_is_order_independent(self) -> None:
        first = self._set()
        second = acceptance.build_review_set(
            manifest_snapshot_hash="manifest-hash",
            depends_on=["dep-a", "dep-b"],
            authored_artifacts={
                "openspec/changes/c/tasks.md": "tasks",
                "openspec/changes/c/design.md": None,
                "openspec/changes/c/proposal.md": "proposal",
            },
            spec_deltas={
                "openspec/changes/c/specs/cap/spec.md": (
                    "## ADDED Requirements\n\n### Requirement: R1\n"
                )
            },
            canonical_specs={"openspec/specs/cap/spec.md": "canonical"},
            tracked_change_files={"lib/x.py": "impl"},
        )
        self.assertEqual(
            acceptance.artifact_revision(first), acceptance.artifact_revision(second)
        )

    def test_crlf_does_not_change_the_revision(self) -> None:
        lf = self._set(authored_artifacts={
            "openspec/changes/c/proposal.md": "line one\nline two\n",
            "openspec/changes/c/tasks.md": "tasks",
            "openspec/changes/c/design.md": None,
        })
        crlf = self._set(authored_artifacts={
            "openspec/changes/c/proposal.md": "line one\r\nline two\r\n",
            "openspec/changes/c/tasks.md": "tasks",
            "openspec/changes/c/design.md": None,
        })
        self.assertEqual(
            acceptance.artifact_revision(lf), acceptance.artifact_revision(crlf)
        )

    def test_review_set_contains_the_real_artifacts(self) -> None:
        review_set = self._set()
        paths = {
            entry["path"] for entry in review_set["authored_artifacts"]
        } | {
            entry["path"] for entry in review_set["spec_deltas"]
        } | {
            entry["path"] for entry in review_set["canonical_specs"]
        }
        self.assertEqual(
            paths,
            {
                "openspec/changes/c/proposal.md",
                "openspec/changes/c/tasks.md",
                "openspec/changes/c/design.md",
                "openspec/changes/c/specs/cap/spec.md",
                "openspec/specs/cap/spec.md",
            },
        )
        self.assertEqual(review_set["manifest_snapshot_hash"], "manifest-hash")
        self.assertEqual(review_set["depends_on"], ["dep-a", "dep-b"])

    def test_delta_identity_distinguishes_operation_and_requirement(self) -> None:
        added = acceptance.build_review_set(
            spec_deltas={
                "openspec/changes/c/specs/cap/spec.md": (
                    "## ADDED Requirements\n\n### Requirement: Renamed\n"
                )
            }
        )
        modified = acceptance.build_review_set(
            spec_deltas={
                "openspec/changes/c/specs/cap/spec.md": (
                    "## MODIFIED Requirements\n\n### Requirement: Renamed\n"
                )
            }
        )
        self.assertNotEqual(
            acceptance.artifact_revision(added), acceptance.artifact_revision(modified)
        )
        identity = added["spec_deltas"][0]
        self.assertEqual(identity["operation"], "ADDED")
        self.assertEqual(identity["requirement"], "Renamed")

    def test_manifest_and_dependency_changes_invalidate_a_stale_acceptance(self) -> None:
        base = acceptance.artifact_revision(self._set())
        manifest_changed = acceptance.artifact_revision(
            self._set(manifest_snapshot_hash="other")
        )
        dep_changed = acceptance.artifact_revision(self._set(depends_on=["dep-a"]))
        self.assertNotEqual(base, manifest_changed)
        self.assertNotEqual(base, dep_changed)
        self.assertTrue(acceptance.revision_is_stale(base, manifest_changed))

    def test_missing_artifact_is_part_of_the_revision(self) -> None:
        present = acceptance.artifact_revision(self._set())
        absent = acceptance.artifact_revision(
            self._set(
                authored_artifacts={
                    "openspec/changes/c/proposal.md": "proposal",
                    "openspec/changes/c/tasks.md": "tasks",
                    "openspec/changes/c/design.md": "design",
                }
            )
        )
        self.assertNotEqual(present, absent)

    def test_revision_is_not_the_broker_material_hash(self) -> None:
        """1.3 — acceptance binding is disjoint from approval checkpoint binding."""
        revision = acceptance.artifact_revision(self._set())
        self.assertTrue(revision.startswith(acceptance.ARTIFACT_REVISION_PREFIX))
        # The broker's material hash covers gate fields + snapshot + policy
        # revision; it can never equal an acceptance artifact revision.
        import hashlib

        broker_hash = hashlib.sha256(b"gate-fields").hexdigest()
        self.assertNotEqual(revision, broker_hash)

    def test_normalize_verdict_rejects_unknown_and_empty_accept(self) -> None:
        with self.assertRaises(acceptance.AcceptanceContractError):
            acceptance.normalize_verdict({"outcome": "maybe"})
        with self.assertRaises(acceptance.AcceptanceContractError):
            acceptance.normalize_verdict({"outcome": "accept", "artifacts_reviewed": []})

    def test_authoritative_identities_include_manifest_and_dependency_ground_truth(
        self,
    ) -> None:
        identities = acceptance.authoritative_artifact_identities(self._set())
        self.assertIn("manifest:snapshot:manifest-hash", identities)
        self.assertIn("manifest:depends_on:dep-a", identities)
        self.assertIn("manifest:depends_on:dep-b", identities)
        for path in (
            "openspec/changes/c/proposal.md",
            "openspec/changes/c/tasks.md",
            "openspec/changes/c/design.md",
            "openspec/changes/c/specs/cap/spec.md",
            "openspec/specs/cap/spec.md",
            "lib/x.py",
        ):
            self.assertIn(path, identities)
        self.assertEqual(len(identities), len(set(identities)))
        self.assertEqual(identities, sorted(identities))
        # An empty snapshot hash is still an acknowledged identity.
        empty = acceptance.authoritative_artifact_identities(
            acceptance.build_review_set()
        )
        self.assertEqual(empty, ["manifest:snapshot:absent"])

    def test_accept_must_acknowledge_exactly_the_authoritative_set(self) -> None:
        required = acceptance.authoritative_artifact_identities(self._set())
        # The complete set — in any order — satisfies the coverage rule.
        verdict = acceptance.normalize_verdict(
            {
                "outcome": "accept",
                "artifacts_reviewed": list(reversed(required)),
            },
            required_artifacts=required,
        )
        self.assertEqual(verdict["outcome"], "accept")
        self.assertEqual(set(verdict["artifacts_reviewed"]), set(required))
        # A partial accept naming only proposal/tasks is rejected.
        with self.assertRaises(acceptance.AcceptanceContractError):
            acceptance.normalize_verdict(
                {
                    "outcome": "accept",
                    "artifacts_reviewed": [
                        "openspec/changes/c/proposal.md",
                        "openspec/changes/c/tasks.md",
                    ],
                },
                required_artifacts=required,
            )
        # An accept omitting only the manifest/dependency ground truth is
        # rejected even when every file artifact is named.
        files_only = [
            identity for identity in required if not identity.startswith("manifest:")
        ]
        with self.assertRaises(acceptance.AcceptanceContractError) as ctx:
            acceptance.normalize_verdict(
                {"outcome": "accept", "artifacts_reviewed": files_only},
                required_artifacts=required,
            )
        self.assertIn("manifest:snapshot:manifest-hash", str(ctx.exception))
        # An arbitrary-path accept is rejected as unexpected.
        with self.assertRaises(acceptance.AcceptanceContractError):
            acceptance.normalize_verdict(
                {"outcome": "accept", "artifacts_reviewed": ["etc/passwd"]},
                required_artifacts=required,
            )
        # An accept adding extras beyond the authoritative set is rejected.
        with self.assertRaises(acceptance.AcceptanceContractError):
            acceptance.normalize_verdict(
                {
                    "outcome": "accept",
                    "artifacts_reviewed": required + ["lib/unrelated.py"],
                },
                required_artifacts=required,
            )

    def test_fix_and_escalate_are_not_bound_to_the_authoritative_set(self) -> None:
        required = acceptance.authoritative_artifact_identities(self._set())
        fix = acceptance.normalize_verdict(
            {"outcome": "fix", "fix_prompt": "rename the delta"},
            required_artifacts=required,
        )
        self.assertEqual(fix["outcome"], "fix")
        escalate = acceptance.normalize_verdict(
            {"outcome": "escalate", "reason": "needs judgment"},
            required_artifacts=required,
        )
        self.assertEqual(escalate["outcome"], "escalate")

    def test_fix_without_defect_text_is_rejected(self) -> None:
        with self.assertRaises(acceptance.AcceptanceContractError):
            acceptance.normalize_verdict({"outcome": "fix", "reason": "", "fix_prompt": ""})

    def test_verdict_satisfies_stage_only_for_a_non_stale_accept(self) -> None:
        self.assertTrue(acceptance.verdict_satisfies_stage("accept", "r1", "r1"))
        self.assertFalse(acceptance.verdict_satisfies_stage("accept", "r1", "r2"))
        self.assertFalse(acceptance.verdict_satisfies_stage("fix", "r1", "r1"))
        self.assertFalse(acceptance.verdict_satisfies_stage("escalate", "r1", "r1"))


# ---------------------------------------------------------------------------
# Ledger migration and verdict storage
# ---------------------------------------------------------------------------


class AcceptanceLedgerTests(unittest.TestCase):
    """1.2 / 7.4 — forward-only v6 -> v7 migration and verdict methods."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.storage = self.root / "storage"
        self.storage.mkdir()
        self.db_path = self.storage / "supervisor.sqlite3"

    def _open(self) -> ledger.Ledger:
        handle = ledger.open_ledger(self.db_path, repository_root=self.repo)
        self.addCleanup(handle.close)
        return handle

    def _register(self, handle) -> int:
        return handle.register_job(
            run_id="run-1",
            worktree=self.repo / "worktree",
            owner="service",
            policy=_policy(),
            operator="operator",
            manifest_content="[[changes]]\nid = \"c\"\n",
        )

    def _downgrade_to_v6(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP TABLE acceptance_reviews")
        conn.execute("PRAGMA user_version = 6")
        conn.commit()
        conn.close()

    def test_v6_ledger_migrates_forward_preserving_records(self) -> None:
        handle = self._open()
        job_id = self._register(handle)
        policy_hash = handle.current_policy(job_id)["manifest_snapshot_hash"]
        receipt = handle.record_stop_request(
            job_id, kind="pause", authority="operator", detail="test"
        )
        wait = handle.record_wait(
            job_id, kind="human", checkpoint="cp", material_hash="mh"
        )
        snapshot_hash = handle.record_manifest_snapshot(job_id, content="manifest")
        handle.close()
        self._downgrade_to_v6()

        migrated = self._open()
        self.assertEqual(migrated.pragma("user_version"), ledger.CURRENT_SCHEMA_VERSION)
        self.assertEqual(migrated.get_job(job_id)["id"], job_id)
        self.assertEqual(
            migrated.current_policy(job_id)["manifest_snapshot_hash"], policy_hash
        )
        self.assertEqual(migrated.get_wait(wait)["checkpoint"], "cp")
        self.assertEqual(
            [row["id"] for row in migrated.stop_receipts(job_id)], [receipt]
        )
        self.assertEqual(
            migrated.manifest_snapshot(job_id, snapshot_hash), "manifest"
        )
        # The acceptance table is usable immediately after migration.
        review_id = migrated.record_acceptance_review(
            job_id,
            change_id="c",
            outcome="accept",
            artifact_revision="acceptance-v1:abc",
            reviewed_artifacts=["openspec/changes/c/tasks.md"],
            reason="ok",
            created_check_evidence="passed",
        )
        self.assertEqual(
            migrated.latest_acceptance_review(job_id, "c")["id"], review_id
        )

    def test_newer_than_code_ledger_is_refused(self) -> None:
        handle = self._open()
        handle.close()
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA user_version = 8")
        conn.commit()
        conn.close()
        with self.assertRaises(ledger.LedgerVersionError):
            ledger.open_ledger(self.db_path, repository_root=self.repo)

    def test_record_requires_a_known_outcome_and_revision(self) -> None:
        handle = self._open()
        job_id = self._register(handle)
        with self.assertRaises(ledger.LedgerError):
            handle.record_acceptance_review(
                job_id,
                change_id="c",
                outcome="approve",
                artifact_revision="acceptance-v1:abc",
            )
        with self.assertRaises(ledger.LedgerError):
            handle.record_acceptance_review(
                job_id, change_id="c", outcome="accept", artifact_revision=""
            )

    def test_latest_verdict_is_append_only_and_reads_back(self) -> None:
        handle = self._open()
        job_id = self._register(handle)
        handle.record_acceptance_review(
            job_id,
            change_id="c",
            outcome="fix",
            artifact_revision="acceptance-v1:1",
            reviewed_artifacts=["a"],
            fix_prompt="rename the delta",
        )
        handle.record_acceptance_review(
            job_id,
            change_id="c",
            outcome="accept",
            artifact_revision="acceptance-v1:2",
            reviewed_artifacts=["a", "b"],
            reason="clean",
        )
        rows = handle.list_acceptance_reviews(job_id, "c")
        self.assertEqual([row["outcome"] for row in rows], ["fix", "accept"])
        self.assertEqual(
            json.loads(handle.latest_acceptance_review(job_id, "c")["reviewed_artifacts"]),
            ["a", "b"],
        )
        # Implementation-review state is untouched: acceptance has no column on
        # the job/action/receipt tables.
        self.assertEqual(handle.stop_receipts(job_id), [])


# ---------------------------------------------------------------------------
# Loop-level acceptance stage
# ---------------------------------------------------------------------------


class AcceptanceStageHarness(unittest.TestCase):
    """Drive the real supervised run loop through the acceptance stage."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text(
            "openspec/changes/archive/\n", encoding="utf-8"
        )
        git(self.repo, "add", "tracked.txt", ".gitignore")
        git(
            self.repo,
            "-c", "user.email=test@example.invalid",
            "-c", "user.name=Test User",
            "commit", "-m", "init",
        )

        self.storage = self.root / "service-storage"
        self.storage.mkdir()
        self.db_path = self.storage / "supervisor.sqlite3"
        self.ledger = ledger.open_ledger(self.db_path, repository_root=self.repo)
        self.addCleanup(self.ledger.close)

        self.cid = "add-acceptance-test"
        self.plan_name = f"run-{self.cid}"
        self.cfg = {
            "name": self.plan_name,
            "adapter": "opencode",
            "implement_invoke": "opencode run --agent opsx-implementer --model $OPSX_IMPLEMENTER_MODEL",
            "review_invoke": "opencode run --agent opsx-reviewer --model $OPSX_REVIEWER_MODEL",
            "archive_invoke": "opencode run --agent opsx-archiver --model $OPSX_ARCHIVER_MODEL",
            "acceptance_invoke": "opencode run --agent opsx-acceptance-reviewer --model $OPSX_ACCEPTANCE_REVIEWER_MODEL",
            "fix_invoke": "opencode run --agent opsx-fixer --model $OPSX_FIXER_MODEL",
            "verify_invoke": "opencode run --agent opsx-verifier --model $OPSX_VERIFIER_MODEL",
            "state_file": ".opencode/opsx-controller/{change}.json",
            "timeout_minutes": 1,
            "max_rounds": 3,
            "no_progress_limit": 2,
            "fast_checks": [],
            "check_timeout_minutes": 1,
            "require_clean_tracked": False,
            "review_created": False,
            "changes": {
                self.cid: {
                    "id": self.cid,
                    "depends_on": [],
                    "enabled": True,
                    "pause_before": False,
                    "timeout_minutes": 1,
                    "create_invoke": "",
                    "create_max_attempts": 1,
                }
            },
            "order": [self.cid],
            "created_check": "",
            "plan_doc": "",
            "create_timeout_minutes": 1,
        }
        self.manifest_path = self.repo / "registered-plan.toml"
        self.manifest_path.write_text(self._manifest_content(), encoding="utf-8")
        self.cfg["_manifest_path"] = str(self.manifest_path)
        model_env = {
            "OPSX_IMPLEMENTER_MODEL": "openai/gpt-4o",
            "OPSX_REVIEWER_MODEL": "openai/gpt-4o",
            "OPSX_ARCHIVER_MODEL": "openai/gpt-4o",
            "OPSX_ACCEPTANCE_REVIEWER_MODEL": "openai/gpt-4o",
            "OPSX_FIXER_MODEL": "openai/gpt-4o",
            "OPSX_VERIFIER_MODEL": "openai/gpt-4o",
            "OPSX_IMPLEMENTER_ESCALATION_MODEL": "openai/gpt-4o",
        }
        patcher = mock.patch.dict(os.environ, model_env)
        patcher.start()
        self.addCleanup(patcher.stop)
        integration = sys.modules.get("lib.orchestrator.journal_dispatch")
        if integration is not None:
            integration.end_active_dispatch()
            self.addCleanup(integration.end_active_dispatch)

        self.state = {"plan": self.plan_name, "approvals": [], "changes": {}}
        self._saved_invoke = self.opsx_plan.invoke_direct_stage
        self.write_authored_change()

    def tearDown(self) -> None:
        self.opsx_plan.invoke_direct_stage = self._saved_invoke

    def write_authored_change(self) -> None:
        cdir = self.repo / "openspec" / "changes" / self.cid
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text(
            "## 1. Tasks\n\n- [x] 1.1 Example task\n", encoding="utf-8"
        )

    def _manifest_content(self) -> str:
        return (
            "[[changes]]\n"
            f'id = "{self.cid}"\n'
            "pause_before = false\n"
            "depends_on = []\n"
        )

    def register_job(self) -> int:
        self.job_id = self.ledger.register_job(
            run_id="run-1",
            worktree=self.repo,
            owner="service",
            policy=_policy(),
            operator="operator",
            manifest_content=self._manifest_content(),
        )
        patcher = mock.patch.dict(
            os.environ, {"OPSX_SUPERVISOR_STATE_FILE": str(self.db_path)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.job_id

    @contextmanager
    def supervised_execution(self):
        job = self.ledger.find_job_by_worktree(self.repo)
        if job is None:
            yield
            return
        job_id = int(job["id"])
        identity = lock_mod.current_identity()
        self.ledger.record_fencing(
            job_id, event="acquired", owner="test-service",
            pid=identity["pid"], process_start=identity["process_start"],
            boot_id=identity["boot_id"], host=identity["host"],
        )
        try:
            yield
        finally:
            self.ledger.record_fencing(
                job_id, event="released", owner="test-service",
                pid=identity["pid"], process_start=identity["process_start"],
                boot_id=identity["boot_id"], host=identity["host"],
            )

    def stage_runner(self, payloads: list[dict]) -> list[dict]:
        records: list[dict] = []

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            integration = self.opsx_plan.journal_dispatch
            if integration is not None and integration.active_dispatch() is not None:
                context = integration.active_dispatch()
                integration.record_session_binding(
                    context["ledger"], context["action_id"],
                    f"fake-{stage}-{round_num}",
                )
            if payloads:
                payload = payloads.pop(0)
                self.assertEqual(stage, payload["stage"], "stage order mismatch")
                mutate = payload.get("mutate")
                if mutate is not None:
                    mutate(self)
                body = json.dumps(payload["result"]) + "\n"
            else:
                body = "not a json envelope\n"
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(body, encoding="utf-8")
            records.append({"stage": stage, "round": round_num})
            return "exited", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke
        return records

    def run_change(self) -> str:
        with self.supervised_execution():
            return self.opsx_plan.run_direct_change(
                self.repo, self.cfg, self.state, self.cid, budget_usd=0.0
            )

    def record(self) -> dict:
        return self.opsx_plan.state_mod.rec(self.state, self.cid)

    # -- payload builders --

    def implement_payload(self) -> dict:
        return {
            "stage": "implement",
            "result": {
                "status": "implemented",
                "change": self.cid,
                "round": 1,
                "progress_made": True,
                "completed_tasks": ["1.1"],
                "remaining_tasks": [],
                "task_counts": {"complete": 1, "total": 1},
                "files_touched": [],
                "known_change_files": [],
                "summary": "done",
            },
        }

    def review_payload(self, verdict: str = "pass") -> dict:
        return {
            "stage": "review",
            "result": {
                "status": "reviewed",
                "change": self.cid,
                "round": 1,
                "verdict": verdict,
                "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                "findings": [],
                "summary": "review clean",
                "fix_prompt": "",
            },
        }

    def authoritative_artifacts(self) -> list[str]:
        """The engine-derived authoritative artifact set for the change now."""
        policy = self.ledger.current_policy(self.job_id)
        gate = {"manifest_snapshot_hash": policy["manifest_snapshot_hash"]}
        _revision, _review_set, identities = (
            self.opsx_plan.compute_acceptance_revision(
                self.repo, self.cfg, self.state, self.cid, gate=gate
            )
        )
        return identities

    def acceptance_payload(self, outcome: str, **extra) -> dict:
        result = {
            "role": "acceptance_reviewer",
            "outcome": outcome,
            "artifacts_reviewed": extra.pop(
                "artifacts_reviewed", self.authoritative_artifacts()
            ),
            "reason": extra.pop("reason", f"acceptance {outcome}"),
            "fix_prompt": extra.pop("fix_prompt", ""),
        }
        result.update(extra)
        return {"stage": "acceptance", "result": result}

    def fixer_payload(self, **extra) -> dict:
        result = {
            "role": "fixer",
            "repair": "renamed the delta requirement",
            "files": [f"openspec/changes/{self.cid}/specs/cap/spec.md"],
            "checks": [{"command": "openspec validate", "result": "pass", "detail": ""}],
            "self_certified": False,
        }
        result.update(extra)
        return {"stage": "fix", "result": result}

    def verifier_payload(self, verdict: str = "pass", **extra) -> dict:
        result = {
            "role": "verifier",
            "verdict": verdict,
            "repair_verified": verdict == "pass",
            "diff_reviewed": True,
            "evidence": [],
            "reason": f"verifier {verdict}",
        }
        result.update(extra)
        return {"stage": "verify", "result": result}


class AcceptanceStageDispatchTests(AcceptanceStageHarness):
    """2.2 / 2.3 / 7.1 — dispatch position, outcomes, distinctness."""

    def test_acceptance_runs_between_review_and_archive(self) -> None:
        self.register_job()
        records = self.stage_runner(
            [self.implement_payload(), self.review_payload(), self.acceptance_payload("accept")]
        )
        self.run_change()
        self.assertEqual(
            [entry["stage"] for entry in records[:3]],
            ["implement", "review", "acceptance"],
        )
        record = self.record()
        self.assertEqual(record["acceptance"]["outcome"], "accept")
        self.assertTrue(record["acceptance"]["artifact_revision"].startswith("acceptance-v1:"))
        # The verdict was recorded in the ledger against the reviewed revision.
        row = self.ledger.latest_acceptance_review(self.job_id, self.cid)
        self.assertIsNotNone(row)
        self.assertEqual(row["outcome"], "accept")

    def test_acceptance_verdict_is_distinct_from_implementation_review(self) -> None:
        self.register_job()
        self.stage_runner(
            [self.implement_payload(), self.review_payload(), self.acceptance_payload("accept")]
        )
        self.run_change()
        record = self.record()
        # The implementation review verdict/findings are untouched.
        self.assertEqual(record["last_review"]["verdict"], "pass")
        self.assertEqual(record["last_review"]["finding_counts"]["critical"], 0)
        # Acceptance is a separate stage result.
        self.assertEqual(record["acceptance"]["outcome"], "accept")
        statuses = [
            (entry.get("phase"), entry.get("status")) for entry in record["history"]
        ]
        self.assertIn(("acceptance", "accept"), statuses)
        self.assertIn(("review", "pass"), statuses)

    def test_unconfigured_acceptance_invoke_fails_closed(self) -> None:
        self.cfg["acceptance_invoke"] = ""
        self.register_job()
        records = self.stage_runner([self.implement_payload(), self.review_payload()])
        self.run_change()
        self.assertEqual([entry["stage"] for entry in records], ["implement", "review"])
        record = self.record()
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertIn("acceptance stage invoke is not configured", record["reason"])
        self.assertEqual(
            [
                row["kind"]
                for row in self.ledger.list_actions(self.job_id)
                if row["kind"] == "acceptance"
            ],
            [],
        )

    def test_stale_accept_is_rejected_and_reruns_over_the_new_revision(self) -> None:
        self.register_job()

        def edit_artifact(harness) -> None:
            path = (
                harness.repo / "openspec" / "changes" / harness.cid / "proposal.md"
            )
            path.write_text("## Why\n\nchanged mid-review\n", encoding="utf-8")

        stale_accept = self.acceptance_payload("accept", reason="looks good")
        stale_accept["mutate"] = edit_artifact
        records = self.stage_runner(
            [
                self.implement_payload(),
                self.review_payload(),
                stale_accept,
                self.acceptance_payload("accept", reason="fresh acceptance"),
            ]
        )
        self.run_change()
        self.assertEqual(
            [entry["stage"] for entry in records[:4]][:3],
            ["implement", "review", "acceptance"],
        )
        record = self.record()
        # The first (stale) accept was rejected and a fresh acceptance ran.
        outcomes = [
            entry.get("status")
            for entry in record["history"]
            if entry.get("phase") == "acceptance"
        ]
        self.assertEqual(outcomes[0], "accept")
        self.assertTrue(
            any(entry.get("stale") for entry in record["history"] if entry.get("phase") == "acceptance")
        )
        self.assertGreaterEqual(len(records), 4)

    def test_created_check_failure_blocks_without_recording_an_accept(self) -> None:
        self.cfg["created_check"] = "false"
        self.register_job()
        records = self.stage_runner([self.implement_payload(), self.review_payload()])
        self.run_change()
        self.assertEqual([entry["stage"] for entry in records], ["implement", "review"])
        record = self.record()
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertIn("created-change check", record["reason"])
        self.assertEqual(record["acceptance"]["created_check"] != "passed", True)
        self.assertIsNone(self.ledger.latest_acceptance_review(self.job_id, self.cid))

    def test_created_check_runs_and_revision_is_captured_before_dispatch(self) -> None:
        self.register_job()
        captured: dict = {}
        original = self.opsx_plan.prepare_acceptance_attempt

        def spy(repo, cfg, state, cid, r, *, gate=None):
            result = original(repo, cfg, state, cid, r, gate=gate)
            captured["revision"] = r["acceptance"]["artifact_revision"]
            captured["created_check"] = r["acceptance"]["created_check"]
            return result

        self.opsx_plan.prepare_acceptance_attempt = spy
        try:
            self.stage_runner(
                [self.implement_payload(), self.review_payload(), self.acceptance_payload("accept")]
            )
            self.run_change()
        finally:
            self.opsx_plan.prepare_acceptance_attempt = original
        self.assertEqual(captured["created_check"], "passed")
        self.assertTrue(captured["revision"].startswith("acceptance-v1:"))

    def test_acceptance_ledger_write_failure_blocks_archive(self) -> None:
        """7.1: a lost acceptance verdict fails closed and never advances."""
        self.register_job()
        records = self.stage_runner(
            [self.implement_payload(), self.review_payload(), self.acceptance_payload("accept")]
        )
        with mock.patch.object(
            ledger.Ledger,
            "record_acceptance_review",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            self.run_change()
        # The accept never satisfied the stage and archive was never dispatched.
        self.assertEqual(
            [entry["stage"] for entry in records],
            ["implement", "review", "acceptance"],
        )
        record = self.record()
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["last_result"], "acceptance_persistence_error")
        self.assertIn("acceptance verdict persistence failed", record["reason"])
        self.assertNotEqual(record["phase"], "archive")
        # The projection surfaces the persistence error instead of a satisfied
        # accept, and no durable verdict row exists for a later reader.
        self.assertEqual(record["acceptance"]["outcome"], "")
        self.assertIn(
            "acceptance verdict persistence failed",
            record["acceptance"]["persistence_error"],
        )
        self.assertIsNone(self.ledger.latest_acceptance_review(self.job_id, self.cid))

    def _assert_incomplete_accept_fails_closed(self, reviewed: list[str]) -> None:
        records = self.stage_runner(
            [
                self.implement_payload(),
                self.review_payload(),
                self.acceptance_payload("accept", artifacts_reviewed=reviewed),
            ]
        )
        self.run_change()
        # The incomplete accept was a contract violation: no archive dispatch,
        # no ledger verdict row, and a named invalid-acceptance failure.
        self.assertEqual(
            [entry["stage"] for entry in records],
            ["implement", "review", "acceptance"],
        )
        record = self.record()
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["last_result"], "acceptance_invalid")
        self.assertNotEqual(record["phase"], "archive")
        self.assertEqual(record["acceptance"]["outcome"], "")
        self.assertIn(
            "must acknowledge exactly the authoritative artifact set",
            record["reason"],
        )
        self.assertIsNone(self.ledger.latest_acceptance_review(self.job_id, self.cid))

    def test_partial_accept_payload_never_dispatches_archive(self) -> None:
        """An accept naming only proposal/tasks cannot advance to archive."""
        self.register_job()
        self._assert_incomplete_accept_fails_closed(
            [
                f"openspec/changes/{self.cid}/proposal.md",
                f"openspec/changes/{self.cid}/tasks.md",
            ]
        )

    def test_arbitrary_accept_payload_never_dispatches_archive(self) -> None:
        """An accept naming arbitrary paths cannot advance to archive."""
        self.register_job()
        self._assert_incomplete_accept_fails_closed(["etc/passwd", "lib/x.py"])

    def test_manifest_omitting_accept_payload_never_dispatches_archive(self) -> None:
        """An accept naming every file but not the manifest/dependency ground
        truth cannot advance to archive."""
        self.register_job()
        files_only = [
            identity
            for identity in self.authoritative_artifacts()
            if not identity.startswith("manifest:")
        ]
        self.assertTrue(files_only)
        self._assert_incomplete_accept_fails_closed(files_only)

    def test_complete_accept_set_dispatches_archive(self) -> None:
        """An accept acknowledging exactly the authoritative set advances."""
        self.register_job()
        records = self.stage_runner(
            [self.implement_payload(), self.review_payload(), self.acceptance_payload("accept")]
        )
        self.run_change()
        record = self.record()
        self.assertEqual(record["acceptance"]["outcome"], "accept")
        self.assertEqual(record["phase"], "archive")
        row = self.ledger.latest_acceptance_review(self.job_id, self.cid)
        self.assertIsNotNone(row)
        self.assertEqual(row["outcome"], "accept")
        self.assertEqual(
            sorted(json.loads(row["reviewed_artifacts"])),
            self.authoritative_artifacts(),
        )


class AcceptanceFixRouteTests(AcceptanceStageHarness):
    """4.1 / 4.2 / 7.2 — fixer -> verifier -> fresh acceptance."""

    def test_fix_dispatches_the_pinned_fixer_and_is_verified(self) -> None:
        self.register_job()
        records = self.stage_runner(
            [
                self.implement_payload(),
                self.review_payload(),
                self.acceptance_payload("fix", fix_prompt="rename the delta"),
                self.fixer_payload(),
                self.verifier_payload("pass"),
                self.acceptance_payload("accept", reason="fresh acceptance after repair"),
            ]
        )
        self.run_change()
        stages = [entry["stage"] for entry in records]
        self.assertEqual(stages[:6], ["implement", "review", "acceptance", "fix", "verify", "acceptance"])
        kinds = [row["kind"] for row in self.ledger.list_actions(self.job_id)]
        self.assertIn("fix", kinds)
        self.assertIn("verify", kinds)
        # The verified repair was recorded so the next gated dispatch saw it.
        fix_action = [
            row for row in self.ledger.list_actions(self.job_id) if row["kind"] == "fix"
        ][0]
        evidence = [
            row
            for row in self.ledger.list_evidence(int(fix_action["id"]))
            if row["kind"] == agent_contracts.EVIDENCE_REPAIR
        ]
        self.assertTrue(evidence)
        payload = json.loads(evidence[-1]["payload"])
        self.assertTrue(payload["consumable"])
        self.assertIsNotNone(payload["fixer_report"])
        self.assertEqual(payload["verifier_verdict"]["verdict"], "pass")
        self.assertNotEqual(
            payload["fixer_report"]["session_id"],
            payload["verifier_verdict"]["session_id"],
        )

    def test_fixer_report_alone_never_advances_to_acceptance(self) -> None:
        record = self.record()
        record["phase"] = "fix"
        record["acceptance"]["fix"] = {
            "repair": "claimed done",
            "self_certified": False,
        }
        action = self.opsx_plan.apply_verify_result(
            self.repo, self.cfg, self.state, self.cid,
            {"role": "fixer", "repair": "claimed done", "self_certified": True},
        )
        self.assertEqual(action, "stop")
        self.assertEqual(record["phase"], "fix")
        self.assertIn("unrepaired acceptance defect", record["reason"])

    def test_failed_verification_fails_naming_the_unrepaired_defect(self) -> None:
        self.register_job()
        records = self.stage_runner(
            [
                self.implement_payload(),
                self.review_payload(),
                self.acceptance_payload("fix", fix_prompt="rename the delta"),
                self.fixer_payload(),
                self.verifier_payload("fail", reason="diff still wrong"),
            ]
        )
        self.run_change()
        self.assertEqual(
            [entry["stage"] for entry in records],
            ["implement", "review", "acceptance", "fix", "verify"],
        )
        record = self.record()
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertIn("unrepaired acceptance defect", record["reason"])
        self.assertIn("rename the delta", record["reason"])

    def test_fix_route_is_bounded_by_the_round_budget(self) -> None:
        self.cfg["max_rounds"] = 2
        record = self.record()
        record["max_rounds"] = 2
        record["round"] = 2
        record["phase"] = "acceptance"
        action = self.opsx_plan.apply_acceptance_result(
            self.repo, self.cfg, self.state, self.cid,
            {"outcome": "fix", "fix_prompt": "still broken", "artifacts_reviewed": []},
        )
        self.assertEqual(action, "stop")
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertIn("acceptance fix budget exhausted", record["reason"])
        self.assertIn("still broken", record["reason"])

    def test_repair_evidence_write_failure_blocks_fresh_acceptance(self) -> None:
        """7.2: a verified repair is not consumed without durable evidence."""
        self.register_job()
        records = self.stage_runner(
            [
                self.implement_payload(),
                self.review_payload(),
                self.acceptance_payload("fix", fix_prompt="rename the delta"),
                self.fixer_payload(),
                self.verifier_payload("pass"),
                self.acceptance_payload("accept", reason="must never run"),
            ]
        )
        with mock.patch.object(
            agent_contracts,
            "record_repair_evidence",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            self.run_change()
        # The verified repair was not consumed and no fresh acceptance ran.
        self.assertEqual(
            [entry["stage"] for entry in records],
            ["implement", "review", "acceptance", "fix", "verify"],
        )
        record = self.record()
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["last_result"], "repair_evidence_persistence_error")
        self.assertIn("repair evidence persistence failed", record["reason"])
        self.assertNotEqual(record["phase"], "acceptance")
        self.assertIn(
            "repair evidence persistence failed",
            record["acceptance"]["persistence_error"],
        )
        # No durable repair evidence exists for the fixer action, so the repair
        # gate can never read a consumed repair.
        fix_action = [
            row for row in self.ledger.list_actions(self.job_id) if row["kind"] == "fix"
        ][0]
        evidence = [
            row
            for row in self.ledger.list_evidence(int(fix_action["id"]))
            if row["kind"] == agent_contracts.EVIDENCE_REPAIR
        ]
        self.assertEqual(evidence, [])


class AcceptanceEscalationAndGateTests(AcceptanceStageHarness):
    """4.3 / 5.1 / 5.2 / 7.3 — escalation and gate separation."""

    def test_escalate_returns_to_the_primary_and_blocks_archive(self) -> None:
        self.register_job()
        records = self.stage_runner(
            [self.implement_payload(), self.review_payload(), self.acceptance_payload("escalate")]
        )
        self.run_change()
        self.assertEqual(
            [entry["stage"] for entry in records], ["implement", "review", "acceptance"]
        )
        record = self.record()
        self.assertNotEqual(record["phase"], "archive")
        self.assertTrue(record["acceptance"]["blocking"])
        self.assertTrue(record["acceptance"]["escalated"])
        self.assertEqual(record["last_result"], "acceptance_escalated")

    def test_escalation_is_never_silently_defaulted(self) -> None:
        record = self.record()
        record["round"] = 1
        record["phase"] = "acceptance"
        action = self.opsx_plan.apply_acceptance_result(
            self.repo, self.cfg, self.state, self.cid,
            {"outcome": "escalate", "reason": "needs a judgment", "artifacts_reviewed": []},
        )
        self.assertEqual(action, "stop")
        self.assertEqual(record["phase"], "acceptance")
        self.assertNotIn(record["phase"], {"archive", "fix"})
        self.assertTrue(record["acceptance"]["blocking"])

    def test_unresolved_escalation_blocks_the_stage_until_resolved(self) -> None:
        record = self.record()
        record["round"] = 1
        record["acceptance"].update({"outcome": "escalate", "blocking": True, "escalated": True})
        blocked = self.opsx_plan.prepare_acceptance_attempt(
            self.repo, self.cfg, self.state, self.cid, record, gate=None
        )
        self.assertIn("escalation is unresolved", blocked["blocked"])
        self.assertTrue(blocked["retryable"])
        self.opsx_plan.resolve_acceptance_escalation(self.state, self.cid, note="primary says fix it")
        self.assertFalse(record["acceptance"]["blocking"])
        self.assertEqual(record["phase"], "acceptance")
        fresh = self.opsx_plan.prepare_acceptance_attempt(
            self.repo, self.cfg, self.state, self.cid, record, gate=None
        )
        self.assertNotIn("blocked", fresh)

    def test_accept_does_not_release_a_human_gate_or_the_operator_receipt(self) -> None:
        self.register_job()
        self.stage_runner(
            [self.implement_payload(), self.review_payload(), self.acceptance_payload("accept")]
        )
        self.run_change()
        record = self.record()
        self.assertEqual(record["acceptance"]["outcome"], "accept")
        # Acceptance never satisfies the operator acceptance receipt for a
        # created change, and it never records a broker receipt.
        self.assertFalse(record["accepted"])
        self.assertEqual(self.ledger.stop_receipts(self.job_id), [])
        receipt_rows = self.ledger.receipts_for_change(self.job_id, self.cid)
        self.assertEqual(
            [row for row in receipt_rows if row["kind"] == "acceptance"], []
        )

    def test_accept_does_not_check_a_task_or_waive_completeness(self) -> None:
        self.register_job()
        self.stage_runner(
            [self.implement_payload(), self.review_payload(), self.acceptance_payload("accept")]
        )
        self.run_change()
        tasks_path = (
            self.repo / "openspec" / "changes" / self.cid / "tasks.md"
        )
        text = tasks_path.read_text(encoding="utf-8")
        # Acceptance marks nothing complete and leaves the tasks file as the
        # implement/review/archive gates see it.
        self.assertIn("- [x] 1.1 Example task", text)
        record = self.record()
        self.assertEqual(record["acceptance"]["outcome"], "accept")
        # An unchecked automatable task still blocks regardless of the verdict.
        tasks_path.write_text(
            "## 1. Tasks\n\n- [ ] 1.1 Example task\n- [ ] 1.2 Still open\n",
            encoding="utf-8",
        )
        remaining = self.opsx_plan.state_mod.remaining_automatable_tasks(
            self.repo, self.cid
        )
        self.assertEqual(remaining, ["1.1 Example task", "1.2 Still open"])

    def test_review_pass_routes_to_acceptance_only_for_supervised_runs(self) -> None:
        payload = {
            "status": "reviewed",
            "verdict": "pass",
            "summary": "clean",
            "finding_counts": {"critical": 0, "warning": 0, "note": 0},
            "findings": [],
            "fix_prompt": "",
        }
        record = self.record()
        with mock.patch.object(self.opsx_plan, "_try_notify"):
            action = self.opsx_plan.apply_review_result(
                self.repo, self.cfg, self.state, self.cid, payload, supervised=False
            )
        self.assertEqual(action, "continue")
        self.assertEqual(record["phase"], "archive")

        record["phase"] = "review"
        with mock.patch.object(self.opsx_plan, "_try_notify"):
            action = self.opsx_plan.apply_review_result(
                self.repo, self.cfg, self.state, self.cid, payload, supervised=True
            )
        self.assertEqual(action, "continue")
        self.assertEqual(record["phase"], "acceptance")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
