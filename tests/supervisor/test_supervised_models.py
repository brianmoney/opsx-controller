"""Tests for the supervised model-policy contract and pure decisions.

Covers:

- a configuration with no supervised roles resolves and dispatches exactly as
  before (legacy behavior);
- the pure pre-dispatch policy check blocks a missing, unallowlisted, or
  identifier-syntax-invalid supervised role with a named reason;
- the descriptive frontier ``supervisor`` is allowlist-exempt and classified
  budget-counted;
- no fallback or inheritance occurs;
- the ``mismatch`` predicate fires only on a differing non-null observed
  identity;
- a resolved identifier different from its policy pin blocks;
- an unknown/interrupted observation is classified retained;
- the explicit stage mapping includes create-to-``supervised_author`` and the
  ordinary implement/review/archive roles.
"""

from __future__ import annotations

import unittest

from lib.supervisor import model_policy as mp


def _selection(**role_overrides: str) -> dict:
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
    roles.update(role_overrides)
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
    return {"version": mp.MODEL_POLICY_VERSION, "roles": roles, "stages": stages}


def _allowlist(models: list[str] | None = None) -> dict:
    return {
        "version": mp.MODEL_POLICY_VERSION,
        "models": models if models is not None else [
            "cheap/author",
            "cheap/implementer",
            "cheap/reviewer",
            "cheap/archiver",
            "cheap/acceptance",
            "cheap/fixer",
            "cheap/verifier",
            "cheap/escalation",
        ],
        "source": "repo-local config (/repo/.opsx-plan/models.toml, [allowlist])",
    }


def _policy(**overrides: object) -> dict:
    base = {"model_selection": _selection(), "inexpensive_allowlist": _allowlist()}
    base.update(overrides)
    return base


class LegacyCompatibilityTests(unittest.TestCase):
    def test_standard_stage_mapping_is_explicit(self) -> None:
        self.assertEqual(mp.STANDARD_STAGE_MAPPING["create"], "supervised_author")
        self.assertEqual(mp.STANDARD_STAGE_MAPPING["implement"], "implementer")
        self.assertEqual(mp.STANDARD_STAGE_MAPPING["review"], "reviewer")
        self.assertEqual(mp.STANDARD_STAGE_MAPPING["archive"], "archiver")
        self.assertEqual(mp.STANDARD_STAGE_MAPPING["acceptance"], "acceptance_reviewer")
        self.assertEqual(mp.STANDARD_STAGE_MAPPING["fix"], "fixer")
        self.assertEqual(mp.STANDARD_STAGE_MAPPING["verify"], "verifier")
        self.assertEqual(mp.STANDARD_STAGE_MAPPING["escalate"], "implementer_escalation")

    def test_controller_is_not_a_supervised_dispatch_role(self) -> None:
        self.assertNotIn("controller", mp.SUPERVISED_DISPATCH_ROLES)
        self.assertIn("supervisor", mp.POLICY_ROLES)

    def test_no_policy_module_dependency_on_models_runtime(self) -> None:
        """The module must stay stdlib-only (checked structurally elsewhere;
        this asserts it has no runtime-package imports)."""
        import ast
        from pathlib import Path

        source = Path(mp.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("lib."), alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertFalse(node.module.startswith("lib."), node.module)


class EncodeDecodeTests(unittest.TestCase):
    def test_model_selection_round_trip(self) -> None:
        payload = _selection()
        encoded = mp.encode_model_selection(payload)
        self.assertEqual(encoded, payload)

    def test_allowlist_round_trip(self) -> None:
        payload = _allowlist()
        encoded = mp.encode_allowlist(payload)
        self.assertEqual(encoded, payload)

    def test_encode_rejects_missing_version(self) -> None:
        with self.assertRaises(mp.ModelPolicyError):
            mp.encode_model_selection({"roles": {"implementer": "cheap/x"}, "stages": {"implement": "implementer"}})
        with self.assertRaises(mp.ModelPolicyError):
            mp.encode_allowlist({"models": ["cheap/x"], "source": "s"})

    def test_encode_rejects_newer_version(self) -> None:
        with self.assertRaises(mp.ModelPolicyVersionError):
            mp.encode_model_selection({"version": 99, "roles": {"implementer": "x"}, "stages": {"implement": "implementer"}})
        with self.assertRaises(mp.ModelPolicyVersionError):
            mp.encode_allowlist({"version": 2, "models": [], "source": "s"})

    def test_encode_rejects_stage_without_pin(self) -> None:
        with self.assertRaises(mp.ModelPolicyError):
            mp.encode_model_selection(
                {"version": 1, "roles": {"implementer": "x"}, "stages": {"fix": "fixer"}}
            )

    def test_encode_rejects_create_mapped_to_another_role(self) -> None:
        # A supervised create dispatch must never bypass supervised_author by
        # recording it against the implementer (or any other supported role).
        with self.assertRaises(mp.ModelPolicyError) as ctx:
            mp.encode_model_selection(
                {
                    "version": 1,
                    "roles": {"implementer": "x", "supervised_author": "y"},
                    "stages": {"create": "implementer"},
                }
            )
        self.assertIn("supervised_author", str(ctx.exception))

    def test_encode_rejects_unsupported_stage(self) -> None:
        with self.assertRaises(mp.ModelPolicyError):
            mp.encode_model_selection(
                {
                    "version": 1,
                    "roles": {"implementer": "x"},
                    "stages": {"review": "implementer"},
                }
            )

    def test_encode_rejects_role_outside_policy_roles(self) -> None:
        # A pin for an unsupported role (e.g. the legacy controller) is rejected
        # so a stage cannot silently map to a non-policy role.
        with self.assertRaises(mp.ModelPolicyError):
            mp.encode_model_selection(
                {
                    "version": 1,
                    "roles": {"controller": "x"},
                    "stages": {"implement": "controller"},
                }
            )

    def test_encode_rejects_whitespace_model_identifier(self) -> None:
        with self.assertRaises(mp.ModelPolicyError):
            mp.encode_model_selection(
                {"version": 1, "roles": {"implementer": "  "}, "stages": {"implement": "implementer"}}
            )

    def test_decode_legacy_unversioned_preserves_original(self) -> None:
        old_list = ["cheap/model-a", "cheap/model-b"]
        result = mp.decode_allowlist(old_list)
        self.assertEqual(result["state"], "legacy_unversioned")
        self.assertEqual(result["payload"], old_list)

    def test_decode_legacy_dict_without_version(self) -> None:
        result = mp.decode_model_selection({"implementer": "cheap/a"})
        self.assertEqual(result["state"], "legacy_unversioned")
        self.assertEqual(result["payload"], {"implementer": "cheap/a"})

    def test_decode_versioned_returns_normalized(self) -> None:
        result = mp.decode_model_selection(_selection())
        self.assertEqual(result["state"], "versioned")
        self.assertEqual(result["payload"]["stages"]["create"], "supervised_author")

    def test_decode_newer_version_rejected(self) -> None:
        with self.assertRaises(mp.ModelPolicyVersionError):
            mp.decode_model_selection({"version": 5, "roles": {}, "stages": {}})


class DispatchIdentityTests(unittest.TestCase):
    def _record(self, **overrides: object) -> dict:
        base = {
            "action_id": 42,
            "role": "supervised_author",
            "requested_model": "cheap/author",
            "observed_model": None,
            "observation_state": "requested",
            "reservation_state": "reserved",
        }
        base.update(overrides)
        return base

    def test_valid_record_normalizes(self) -> None:
        record = mp.validate_dispatch_identity(self._record())
        self.assertEqual(record["action_id"], 42)
        self.assertIsNone(record["observed_model"])

    def test_invalid_observation_state_rejected(self) -> None:
        with self.assertRaises(mp.ModelPolicyError):
            mp.validate_dispatch_identity(self._record(observation_state="bogus"))

    def test_invalid_reservation_state_rejected(self) -> None:
        with self.assertRaises(mp.ModelPolicyError):
            mp.validate_dispatch_identity(self._record(reservation_state="bogus"))

    def test_non_policy_role_rejected(self) -> None:
        with self.assertRaises(mp.ModelPolicyError):
            mp.validate_dispatch_identity(self._record(role="controller"))

    def test_mismatch_only_when_non_null_and_different(self) -> None:
        self.assertTrue(mp.mismatch(self._record(observed_model="other/model")))
        self.assertFalse(mp.mismatch(self._record(observed_model=None)))
        self.assertFalse(mp.mismatch(self._record(observed_model="cheap/author")))

    def test_retain_on_unknown_or_interrupted(self) -> None:
        self.assertTrue(mp.retain_on_unknown(self._record(observation_state="unknown")))
        self.assertTrue(mp.retain_on_unknown(self._record(observation_state="interrupted")))
        self.assertFalse(mp.retain_on_unknown(self._record(observation_state="observed")))
        self.assertFalse(mp.retain_on_unknown(self._record(observation_state="requested")))

    def test_requested_matches_pin(self) -> None:
        selection = mp.encode_model_selection(_selection())
        self.assertTrue(
            mp.requested_matches_pin(
                {"role": "supervised_author", "requested_model": "cheap/author"}, selection
            )
        )
        self.assertFalse(
            mp.requested_matches_pin(
                {"role": "supervised_author", "requested_model": "other"}, selection
            )
        )


class PolicyCheckTests(unittest.TestCase):
    def test_allowlisted_resolved_role_proceeds(self) -> None:
        decision = mp.check_dispatch(
            _policy(), role="implementer", resolved_model="cheap/implementer"
        )
        self.assertTrue(decision["allowed"], decision["reason"])
        self.assertFalse(decision["budget_counted"])

    def test_missing_role_blocks_with_named_reason(self) -> None:
        decision = mp.check_dispatch(_policy(), role="fixer", resolved_model=None)
        self.assertFalse(decision["allowed"])
        self.assertIn("fixer", decision["reason"])
        self.assertIn("unresolved", decision["reason"])

    def test_unallowlisted_role_blocks(self) -> None:
        decision = mp.check_dispatch(_policy(), role="fixer", resolved_model="expensive/fixer")
        self.assertFalse(decision["allowed"])
        self.assertIn("allowlist", decision["reason"])
        self.assertEqual(decision["model"], "expensive/fixer")

    def test_identifier_syntax_invalid_blocks_as_unavailable(self) -> None:
        decision = mp.check_dispatch(
            _policy(),
            role="implementer",
            resolved_model="cheap/implementer",
            syntax_warnings=["implementer: bare identifier rejected"],
        )
        self.assertFalse(decision["allowed"])
        self.assertIn("unavailable", decision["reason"])

    def test_unrelated_syntax_warning_does_not_block(self) -> None:
        # A warning naming a different role must not make this role
        # unavailable; only the requested role's own warnings block.
        decision = mp.check_dispatch(
            _policy(),
            role="implementer",
            resolved_model="cheap/implementer",
            syntax_warnings=["archiver: bare identifier rejected"],
        )
        self.assertTrue(decision["allowed"], decision["reason"])

    def test_scoped_syntax_warning_still_blocks(self) -> None:
        decision = mp.check_dispatch(
            _policy(),
            role="implementer",
            resolved_model="cheap/implementer",
            syntax_warnings=[
                "archiver: bare identifier rejected",
                "implementer: bare identifier rejected",
            ],
        )
        self.assertFalse(decision["allowed"])
        self.assertIn("unavailable", decision["reason"])
        self.assertNotIn("archiver", decision["reason"])

    def test_supervisor_exempt_from_allowlist_and_budget_counted(self) -> None:
        decision = mp.check_dispatch(
            _policy(), role="supervisor", resolved_model="frontier/supervisor"
        )
        self.assertTrue(decision["allowed"], decision["reason"])
        self.assertTrue(decision["budget_counted"])

    def test_supervisor_off_pin_still_blocks(self) -> None:
        decision = mp.check_dispatch(
            _policy(), role="supervisor", resolved_model="frontier/other"
        )
        self.assertFalse(decision["allowed"])
        self.assertIn("pin", decision["reason"])

    def test_off_pin_resolved_identifier_blocks(self) -> None:
        # An allowlisted model that is not the role's exact pin still blocks.
        policy = _policy(inexpensive_allowlist=_allowlist(["cheap/other"]))
        decision = mp.check_dispatch(
            policy, role="implementer", resolved_model="cheap/other"
        )
        self.assertFalse(decision["allowed"])
        self.assertIn("pin", decision["reason"])

    def test_escalation_role_is_checked_like_any_other(self) -> None:
        decision = mp.check_dispatch(
            _policy(), role="implementer_escalation", resolved_model="expensive/esc"
        )
        self.assertFalse(decision["allowed"])
        decision = mp.check_dispatch(
            _policy(), role="implementer_escalation", resolved_model="cheap/escalation"
        )
        self.assertTrue(decision["allowed"], decision["reason"])

    def test_legacy_unversioned_selection_blocks_closed(self) -> None:
        policy = _policy(model_selection={"implementer": "cheap/implementer"})
        decision = mp.check_dispatch(
            policy, role="implementer", resolved_model="cheap/implementer"
        )
        self.assertFalse(decision["allowed"])
        self.assertIn("legacy_unversioned", decision["reason"])

    def test_legacy_unversioned_allowlist_blocks_non_supervisor(self) -> None:
        policy = _policy(inexpensive_allowlist=["cheap/implementer"])
        decision = mp.check_dispatch(
            policy, role="implementer", resolved_model="cheap/implementer"
        )
        self.assertFalse(decision["allowed"])
        self.assertIn("legacy_unversioned", decision["reason"])

    def test_controller_is_not_a_supervised_role(self) -> None:
        decision = mp.check_dispatch(
            _policy(), role="controller", resolved_model="base/controller"
        )
        self.assertFalse(decision["allowed"])
        self.assertIn("not a supervised policy role", decision["reason"])

    def test_decoder_tagged_results_accepted(self) -> None:
        policy = {
            "model_selection": mp.decode_model_selection(_selection()),
            "inexpensive_allowlist": mp.decode_allowlist(_allowlist()),
        }
        decision = mp.check_dispatch(
            policy, role="reviewer", resolved_model="cheap/reviewer"
        )
        self.assertTrue(decision["allowed"], decision["reason"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
