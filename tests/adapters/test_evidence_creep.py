"""Tests for the evidence-creep doctor advisory.

The check exists because a plan whose success parameters are *evidence
obligations* can cost more than the feature it validates, and can be impossible
to discharge. These tests pin two properties:

* it fires on the parameter shapes that caused that failure; and
* it never fails a doctor run -- it is an advisory, not a gate.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from lib.orchestrator import evidence_creep as ec

_REPO = Path(__file__).resolve().parents[2]

_CLEAN_PLAN = """\
### Change: `add-a-thing`

**Success parameters:** (1) `GET /` returns 200 with five panels rendered;
(2) an unknown selector returns 400 naming the parameter; (3) `pytest tests/`
passes.
"""

_CREEPY_PLAN = """\
### Change: `qualify-a-thing`

**Success parameters:** (1) 16 screenshots exist (eight pages x 1280/390 px);
(2) all authored/lifecycle inputs are frozen before measurement, or an explicit
reconciled runtime-source binding plus a lifecycle manifest is recorded;
(3) `GET /api/v1/overview` sustains p95 <= 200 ms for 1,000 requests.
"""


class EvidenceCreepScanTests(unittest.TestCase):
    def test_a_behavior_only_plan_produces_no_hits(self) -> None:
        self.assertEqual(ec.scan_plan_text(_CLEAN_PLAN), [])

    def test_screenshot_count_is_flagged(self) -> None:
        hits = ec.scan_plan_text(_CREEPY_PLAN)
        markers = {m for _, m, _ in hits}
        self.assertIn("screenshot count", markers)

    def test_runtime_binding_clause_is_flagged(self) -> None:
        # The clause may match several markers; the property is that it fires.
        hits = ec.scan_plan_text(_CREEPY_PLAN)
        binding = [h for h in hits if "frozen" in h[2] or "manifest" in h[2]]
        self.assertTrue(binding, hits)

    def test_p95_measurement_campaign_is_flagged(self) -> None:
        markers = {m for _, m, _ in ec.scan_plan_text(_CREEPY_PLAN)}
        self.assertIn("p95/p99 measurement", markers)

    def test_hits_are_attributed_to_their_change(self) -> None:
        changes = {c for c, _, _ in ec.scan_plan_text(_CREEPY_PLAN)}
        self.assertEqual(changes, {"qualify-a-thing"})


class EvidenceCreepAdvisoryTests(unittest.TestCase):
    def test_a_clean_plan_reports_no_advisory(self) -> None:
        tmp = _REPO / "tests" / "adapters" / ".tmp-clean-plan.md"
        try:
            tmp.write_text(_CLEAN_PLAN, encoding="utf-8")
            passed, label, remediation = ec.check_evidence_creep(_REPO, str(tmp))
            self.assertTrue(passed)
            self.assertEqual(remediation, "")
            self.assertNotIn("advisory)", label)
        finally:
            tmp.unlink(missing_ok=True)

    def test_a_creepy_plan_is_advisory_but_never_failing(self) -> None:
        tmp = _REPO / "tests" / "adapters" / ".tmp-creepy-plan.md"
        try:
            tmp.write_text(_CREEPY_PLAN, encoding="utf-8")
            passed, label, remediation = ec.check_evidence_creep(_REPO, str(tmp))
            # Advisory only: a hit must never fail the doctor run.
            self.assertTrue(passed)
            self.assertIn("advisory", label)
            self.assertIn("ADVISORY", remediation)
            self.assertIn("plan-authoring.md", remediation)
        finally:
            tmp.unlink(missing_ok=True)

    def test_a_missing_plan_is_not_an_error(self) -> None:
        passed, _label, remediation = ec.check_evidence_creep(
            _REPO, str(_REPO / "does-not-exist.md")
        )
        self.assertTrue(passed)
        self.assertEqual(remediation, "")

    def test_no_plan_is_not_an_error(self) -> None:
        passed, _label, remediation = ec.check_evidence_creep(_REPO, None)
        self.assertTrue(passed)
        self.assertEqual(remediation, "")


if __name__ == "__main__":
    unittest.main()
