from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.models import resolver
from lib.orchestrator import groundtruth as groundtruth_mod
from lib.orchestrator import state as state_mod
from lib.orchestrator import base as base_mod

SCRIPT = Path(__file__).resolve().parents[2] / "orchestrator" / "opsx-plan.py"

# Pre-compiled regex for extracting the fenced TOML block emitted by
# build_schema_guidance.
_TOM_BLOCK = re.compile(r"```toml\s*\n(.*?)```", re.DOTALL)

_MODEL_HOME: tempfile.TemporaryDirectory | None = None
_MODEL_CONFIG_PATCH = None
_MODEL_ENV_PATCH = None


def setUpModule() -> None:
    """Pin model resolution so the suite does not read ambient configuration.

    Resolution consults ~/.config/opsx-controller/models.toml and then the
    OPSX_*_MODEL environment variables. Left unpinned, this suite passes on a
    machine that happens to have models configured and fails on a clean
    checkout or a CI runner, where stages cannot resolve a model and report
    `cannot activate models for adapter ...: unresolved role(s)`.
    """
    global _MODEL_HOME, _MODEL_CONFIG_PATCH, _MODEL_ENV_PATCH
    _MODEL_HOME = tempfile.TemporaryDirectory()
    _MODEL_CONFIG_PATCH = mock.patch.object(
        resolver, "USER_CONFIG_PATH", Path(_MODEL_HOME.name) / "models.toml"
    )
    _MODEL_CONFIG_PATCH.start()
    _MODEL_ENV_PATCH = mock.patch.dict(
        os.environ,
        {
            "OPSX_CONTROLLER_MODEL": "test-provider/test-controller",
            "OPSX_IMPLEMENTER_MODEL": "test-provider/test-implementer",
            "OPSX_REVIEWER_MODEL": "test-provider/test-reviewer",
            "OPSX_ARCHIVER_MODEL": "test-provider/test-archiver",
        },
    )
    _MODEL_ENV_PATCH.start()


def tearDownModule() -> None:
    assert _MODEL_ENV_PATCH is not None
    assert _MODEL_CONFIG_PATCH is not None
    assert _MODEL_HOME is not None
    _MODEL_ENV_PATCH.stop()
    _MODEL_CONFIG_PATCH.stop()
    _MODEL_HOME.cleanup()


def load_opsx_plan():
    spec = importlib.util.spec_from_file_location("opsx_plan", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


class VerifyChangeCreatedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        git(
            self.repo,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test User",
            "commit",
            "-m",
            "init",
        )
        self.cfg = {"created_check": "", "check_timeout_minutes": 1}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_authored_change(self, cid: str) -> None:
        cdir = self.repo / "openspec" / "changes" / cid
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text("## 1. Tasks\n", encoding="utf-8")

    def test_create_allows_preexisting_tracked_changes(self) -> None:
        (self.repo / "tracked.txt").write_text("dirty before create\n", encoding="utf-8")
        before = groundtruth_mod.tracked_worktree_snapshot(self.repo)

        self.write_authored_change("add-example")

        ok, why = groundtruth_mod.verify_change_created(
            self.repo, self.cfg, "add-example", before
        )
        self.assertTrue(ok, why)

    def test_create_rejects_new_tracked_changes(self) -> None:
        before = groundtruth_mod.tracked_worktree_snapshot(self.repo)

        self.write_authored_change("add-example")
        (self.repo / "tracked.txt").write_text("dirty during create\n", encoding="utf-8")

        ok, why = groundtruth_mod.verify_change_created(
            self.repo, self.cfg, "add-example", before
        )
        self.assertFalse(ok)
        self.assertIn("creation modified tracked files", why)

    def test_accept_verification_ignores_unrelated_dirty_tree(self) -> None:
        (self.repo / "tracked.txt").write_text("dirty before accept\n", encoding="utf-8")
        self.write_authored_change("add-example")

        ok, why = groundtruth_mod.verify_change_created(self.repo, self.cfg, "add-example")
        self.assertTrue(ok, why)


class ArchiveCommitEvidenceGateTests(unittest.TestCase):
    """Whether an `archive(<id>):` commit is required evidence depends on
    whether openspec/changes/archive/ is gitignored.

    When the directory is tracked (the default OpenSpec layout) a missing
    commit means the archive was never durably recorded and must fail the
    change. When it is gitignored the archiver has nothing to stage, so the
    commit legitimately does not exist and must not veto completion.
    """

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        git(
            self.repo,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test User",
            "commit",
            "-m",
            "init",
        )
        self.cid = "add-gate-example"
        self.archive_rel = f"openspec/changes/archive/2026-07-26-{self.cid}"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def ignore_archive_dir(self) -> None:
        (self.repo / ".gitignore").write_text(
            "openspec/changes/archive/\n", encoding="utf-8"
        )

    def archive_on_disk(self) -> None:
        dst = self.repo / self.archive_rel
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "proposal.md").write_text("# archived\n", encoding="utf-8")

    def record_without_commit(self) -> dict:
        return {
            "archive": {
                "status": "passed",
                "path": self.archive_rel,
                "commit": "",
                "reason": "",
            }
        }

    def test_archive_dir_ignored_reports_ignore_rules(self) -> None:
        self.assertFalse(groundtruth_mod.archive_dir_ignored(self.repo))
        self.ignore_archive_dir()
        self.assertTrue(groundtruth_mod.archive_dir_ignored(self.repo))

    def test_archive_dir_ignored_ignores_index_state(self) -> None:
        """A force-added legacy archive must not flip the gate: newly
        archived files still stage nothing under the same ignore rule."""
        self.ignore_archive_dir()
        self.archive_on_disk()
        git(self.repo, "add", "-f", f"{self.archive_rel}/proposal.md")
        git(
            self.repo,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test User",
            "commit",
            "-m",
            f"archive({self.cid}): force-added legacy archive",
        )
        self.assertTrue(groundtruth_mod.archive_dir_ignored(self.repo))

    def test_tracked_archive_dir_requires_archive_commit(self) -> None:
        self.archive_on_disk()
        ok, why = self.opsx_plan.verify_direct_archive_done(
            self.repo, self.cid, self.record_without_commit()
        )
        self.assertFalse(ok)
        self.assertIn("did not record archive commit", why)

    def test_ignored_archive_dir_allows_missing_archive_commit(self) -> None:
        self.ignore_archive_dir()
        self.archive_on_disk()
        ok, why = self.opsx_plan.verify_direct_archive_done(
            self.repo, self.cid, self.record_without_commit()
        )
        self.assertTrue(ok, why)

    def test_tracked_archive_dir_requires_reachable_commit(self) -> None:
        self.archive_on_disk()
        record = self.record_without_commit()
        record["archive"]["commit"] = "0" * 40
        ok, why = self.opsx_plan.verify_direct_archive_done(
            self.repo, self.cid, record
        )
        self.assertFalse(ok)
        self.assertIn("not reachable from HEAD", why)

    def test_ignored_archive_dir_tolerates_unreachable_commit(self) -> None:
        self.ignore_archive_dir()
        self.archive_on_disk()
        record = self.record_without_commit()
        record["archive"]["commit"] = "0" * 40
        ok, why = self.opsx_plan.verify_direct_archive_done(
            self.repo, self.cid, record
        )
        self.assertTrue(ok, why)

    def test_record_archive_evidence_requires_commit_when_tracked(self) -> None:
        self.archive_on_disk()
        record = {"archive": {}}
        self.assertFalse(
            self.opsx_plan.record_archive_evidence(self.repo, record, self.cid)
        )

    def test_record_archive_evidence_accepts_no_commit_when_ignored(self) -> None:
        self.ignore_archive_dir()
        self.archive_on_disk()
        record = {"archive": {}}
        self.assertTrue(
            self.opsx_plan.record_archive_evidence(self.repo, record, self.cid)
        )
        self.assertEqual(record["archive"]["status"], "passed")
        self.assertEqual(record["archive"]["commit"], "")
