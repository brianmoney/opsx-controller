"""Unit tests for lib.orchestrator.cost (cost estimation for direct-stage
telemetry). Split out of test_opsx_plan.py during the orchestrator module
extraction.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lib.orchestrator import cost


class CostEstimationTests(unittest.TestCase):
    """Unit tests for cost estimation functions (tasks 5.1-5.8)."""

    def setUp(self) -> None:
        # Reset module-level catalog so tests can use their own.
        cost._cost_catalog = None
        self._saved_denoms = dict(cost.SUBSCRIPTION_DENOMINATORS)
        cost.SUBSCRIPTION_DENOMINATORS.clear()

    def tearDown(self) -> None:
        cost._cost_catalog = None
        cost.SUBSCRIPTION_DENOMINATORS.clear()
        cost.SUBSCRIPTION_DENOMINATORS.update(self._saved_denoms)

    @staticmethod
    def _write_catalog(content: str) -> Path:
        """Write a temporary TOML catalog and return its path."""
        from textwrap import dedent

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False, encoding="utf-8",
        )
        tmp.write(dedent(content))
        tmp.close()
        return Path(tmp.name)

    def _set_catalog(self, content: str) -> None:
        """Replace the module-level catalog with one built from *content*."""
        from lib.pricing import PricingCatalog, UnresolvedPrice

        catalog_path = self._write_catalog(content)
        cost._cost_catalog = (PricingCatalog(catalog_path=catalog_path), UnresolvedPrice)

    def _usage(self, **kwargs):
        """Build a usage dict with defaults for a typical available scenario."""
        defaults = {
            "usage_available": True,
            "input_tokens": None,
            "output_tokens": None,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
        }
        defaults.update(kwargs)
        return defaults

    def _model(self, provider="openai", model_id="gpt-4o"):
        return {"provider": provider, "model_id": model_id}

    # 5.1
    def test_per_token_model_with_input_output_produces_estimated(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"

            [[entries]]
            provider = "openai"
            model_id = "gpt-4o"
            display_name = "GPT-4o"
            billing_mode = "per_token"
            currency = "USD"
            input_price_per_mtok = 2.0
            output_price_per_mtok = 8.0
            effective_date = "2025-01-01"
            """
        )
        usage = self._usage(input_tokens=200000, output_tokens=50000)
        model = self._model()

        result = cost.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["pricing_catalog_version"], "1.0.0")
        self.assertEqual(result["estimated_cost"], 0.8)
        self.assertIsNone(result["unresolved_reason"])
        snapshot = result["price_snapshot"]
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["provider"], "openai")
        self.assertEqual(snapshot["model_id"], "gpt-4o")
        self.assertEqual(snapshot["billing_mode"], "per_token")
        self.assertEqual(snapshot["input_price_per_mtok"], 2.0)
        self.assertEqual(snapshot["output_price_per_mtok"], 8.0)

    # 5.2
    def test_cached_input_tokens_contribute_to_estimate(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"

            [[entries]]
            provider = "openai"
            model_id = "gpt-4o"
            display_name = "GPT-4o"
            billing_mode = "per_token"
            currency = "USD"
            input_price_per_mtok = 2.50
            output_price_per_mtok = 10.00
            cached_input_price_per_mtok = 1.25
            effective_date = "2025-01-01"
            """
        )
        # 100000 cached input tokens at $1.25/mtok = $0.125
        usage = self._usage(cached_input_tokens=100000)
        model = self._model()

        result = cost.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["estimated_cost"], 0.125)
        snapshot = result["price_snapshot"]
        self.assertEqual(snapshot["cached_input_price_per_mtok"], 1.25)

    # 5.3
    def test_usage_unavailable_produces_unresolved(self):
        usage = self._usage(usage_available=False)
        model = self._model()

        result = cost.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["unresolved_reason"], "usage unavailable")
        self.assertIsNone(result["estimated_cost"])
        self.assertIsNone(result["price_snapshot"])

    # 5.4
    def test_missing_model_identity_produces_unresolved(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            [[entries]]
            provider = "openai"
            model_id = "gpt-4o"
            display_name = "GPT-4o"
            billing_mode = "per_token"
            currency = "USD"
            input_price_per_mtok = 2.0
            effective_date = "2025-01-01"
            """
        )
        usage = self._usage(input_tokens=100)

        for model in (
            {"provider": "", "model_id": ""},
            {"provider": None, "model_id": None},
            {"provider": "openai", "model_id": ""},
            {"provider": "", "model_id": "gpt-4o"},
        ):
            with self.subTest(model=model):
                result = cost.estimate_stage_cost(usage, model)
                self.assertEqual(result["status"], "unresolved")
                self.assertEqual(
                    result["unresolved_reason"], "model identity unavailable",
                )

    # 5.5
    def test_unknown_model_pricing_produces_unresolved(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            [[entries]]
            provider = "openai"
            model_id = "gpt-4o"
            display_name = "GPT-4o"
            billing_mode = "per_token"
            currency = "USD"
            input_price_per_mtok = 2.0
            effective_date = "2025-01-01"
            """
        )
        usage = self._usage(input_tokens=100)

        # Unknown model
        result = cost.estimate_stage_cost(
            usage, {"provider": "openai", "model_id": "gpt-99"},
        )
        self.assertEqual(result["status"], "unresolved")
        self.assertIn("unknown model", result["unresolved_reason"])

        # Unknown provider
        result = cost.estimate_stage_cost(
            usage, {"provider": "nobody", "model_id": "model"},
        )
        self.assertEqual(result["status"], "unresolved")
        self.assertIn("unknown provider", result["unresolved_reason"])

    # 5.6
    def test_token_category_positive_usage_unpriced_produces_unresolved(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            [[entries]]
            provider = "openai"
            model_id = "gpt-4o"
            display_name = "GPT-4o"
            billing_mode = "per_token"
            currency = "USD"
            input_price_per_mtok = 2.0
            effective_date = "2025-01-01"
            """
        )
        # reasoning_tokens has positive usage but no reasoning_rate in catalog
        usage = self._usage(reasoning_tokens=1000)
        model = self._model()

        result = cost.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "unresolved")
        self.assertIn("missing rate for observed token category", result["unresolved_reason"])
        self.assertIn("reasoning_tokens", result["unresolved_reason"])

    # 5.7
    def test_subscription_with_valid_denominator_produces_estimated(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            [[entries]]
            provider = "github"
            model_id = "copilot"
            display_name = "GitHub Copilot"
            billing_mode = "subscription"
            currency = "USD"
            subscription_period = "monthly"
            subscription_price = 10.0
            effective_date = "2025-01-01"
            """
        )
        # usage.total_tokens = 100000, denominator = 50000000
        # expected cost = 10.0 * (100000 / 50000000) = 0.02
        usage = self._usage(total_tokens=100000)
        model = self._model("github", "copilot")
        denoms = {"github": {"copilot": 50000000.0}}

        result = cost.estimate_stage_cost(usage, model, denoms)

        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["estimated_cost"], 0.02)
        snapshot = result["price_snapshot"]
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["billing_mode"], "subscription")
        self.assertEqual(snapshot["subscription_price"], 10.0)
        self.assertEqual(snapshot["usage_denominator_units"], 50000000)
        self.assertEqual(snapshot["usage_denominator_source"], "config")

    # 5.8
    def test_subscription_without_denominator_produces_unresolved(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            [[entries]]
            provider = "github"
            model_id = "copilot"
            display_name = "GitHub Copilot"
            billing_mode = "subscription"
            currency = "USD"
            subscription_period = "monthly"
            subscription_price = 10.0
            effective_date = "2025-01-01"
            """
        )
        usage = self._usage(total_tokens=100000)
        model = self._model("github", "copilot")

        result = cost.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(
            result["unresolved_reason"], "missing subscription denominator",
        )
        self.assertIsNone(result["estimated_cost"])

    def test_subscription_uses_total_tokens_from_categories_when_total_is_null(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            [[entries]]
            provider = "github"
            model_id = "copilot"
            display_name = "GitHub Copilot"
            billing_mode = "subscription"
            currency = "USD"
            subscription_period = "monthly"
            subscription_price = 10.0
            effective_date = "2025-01-01"
            """
        )
        # total_tokens is None, but input + output = 200000
        usage = self._usage(total_tokens=None, input_tokens=150000, output_tokens=50000)
        model = self._model("github", "copilot")
        denoms = {"github": {"copilot": 50000000.0}}

        result = cost.estimate_stage_cost(usage, model, denoms)

        self.assertEqual(result["status"], "estimated")
        # 10.0 * (200000 / 50000000) = 0.04
        self.assertEqual(result["estimated_cost"], 0.04)

    def test_module_level_denominators_are_used(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            [[entries]]
            provider = "github"
            model_id = "copilot"
            display_name = "GitHub Copilot"
            billing_mode = "subscription"
            currency = "USD"
            subscription_period = "monthly"
            subscription_price = 10.0
            effective_date = "2025-01-01"
            """
        )
        usage = self._usage(total_tokens=100000)
        model = self._model("github", "copilot")
        cost.SUBSCRIPTION_DENOMINATORS["github"] = {"copilot": 50000000.0}

        result = cost.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["estimated_cost"], 0.02)

    def test_invalid_denominator_produces_unresolved(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            [[entries]]
            provider = "github"
            model_id = "copilot"
            display_name = "GitHub Copilot"
            billing_mode = "subscription"
            currency = "USD"
            subscription_period = "monthly"
            subscription_price = 10.0
            effective_date = "2025-01-01"
            """
        )
        usage = self._usage(total_tokens=100000)
        model = self._model("github", "copilot")

        for bad_denom in (-1, 0):
            with self.subTest(denom=bad_denom):
                denoms = {"github": {"copilot": float(bad_denom)}}
                result = cost.estimate_stage_cost(usage, model, denoms)
                self.assertEqual(result["status"], "unresolved")
                self.assertEqual(
                    result["unresolved_reason"], "invalid subscription denominator",
                )

    def test_non_numeric_and_nan_denominator_produce_unresolved(self):
        """Regression: non-numeric and NaN denominator values must produce
        unresolved status instead of crashing or producing NaN costs."""
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            [[entries]]
            provider = "github"
            model_id = "copilot"
            display_name = "GitHub Copilot"
            billing_mode = "subscription"
            currency = "USD"
            subscription_period = "monthly"
            subscription_price = 10.0
            effective_date = "2025-01-01"
            """
        )
        usage = self._usage(total_tokens=100000)
        model = self._model("github", "copilot")

        # Non-numeric string values should not crash or fall back to
        # "unavailable" — they should produce "unresolved" with a clear
        # reason.
        for bad_denom in ("not-a-number", "invalid", ""):
            with self.subTest(denom=bad_denom):
                denoms = {"github": {"copilot": bad_denom}}
                result = cost.estimate_stage_cost(usage, model, denoms)
                self.assertEqual(result["status"], "unresolved")
                self.assertEqual(
                    result["unresolved_reason"], "invalid subscription denominator",
                )

        # NaN values (float('nan')) must also produce "unresolved" — they are
        # not usable numbers even though isinstance checks pass.
        denoms = {"github": {"copilot": float("nan")}}
        result = cost.estimate_stage_cost(usage, model, denoms)
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(
            result["unresolved_reason"], "invalid subscription denominator",
        )

    def test_zero_token_count_contributes_zero_cost(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            [[entries]]
            provider = "openai"
            model_id = "gpt-4o"
            display_name = "GPT-4o"
            billing_mode = "per_token"
            currency = "USD"
            input_price_per_mtok = 2.0
            output_price_per_mtok = 8.0
            effective_date = "2025-01-01"
            """
        )
        usage = self._usage(input_tokens=0, output_tokens=0)
        model = self._model()

        result = cost.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["estimated_cost"], 0.0)

    def test_null_token_count_does_not_contribute(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            [[entries]]
            provider = "openai"
            model_id = "gpt-4o"
            display_name = "GPT-4o"
            billing_mode = "per_token"
            currency = "USD"
            input_price_per_mtok = 2.0
            output_price_per_mtok = 8.0
            effective_date = "2025-01-01"
            """
        )
        # Only input tokens present; output is null
        usage = self._usage(input_tokens=100000, output_tokens=None)
        model = self._model()

        result = cost.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["estimated_cost"], 0.2)  # 100000/1e6 * 2.0

    def test_price_snapshot_per_token_includes_all_rates(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.2.3"
            updated = "2026-01-01"
            [[entries]]
            provider = "openai"
            model_id = "o3"
            display_name = "o3"
            billing_mode = "per_token"
            currency = "USD"
            input_price_per_mtok = 10.0
            output_price_per_mtok = 40.0
            cached_input_price_per_mtok = 2.5
            reasoning_price_per_mtok = 20.0
            effective_date = "2025-01-01"
            """
        )
        usage = self._usage(input_tokens=10000, output_tokens=5000,
                            cached_input_tokens=2000, reasoning_tokens=1000)
        model = self._model("openai", "o3")

        result = cost.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "estimated")
        snapshot = result["price_snapshot"]
        self.assertEqual(snapshot["catalog_version"], "1.2.3")
        self.assertEqual(snapshot["input_price_per_mtok"], 10.0)
        self.assertEqual(snapshot["output_price_per_mtok"], 40.0)
        self.assertEqual(snapshot["cached_input_price_per_mtok"], 2.5)
        self.assertEqual(snapshot["reasoning_price_per_mtok"], 20.0)
        self.assertEqual(snapshot["display_name"], "o3")
        self.assertNotIn("subscription_price", snapshot)

    def test_empty_catalog_produces_unresolved(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            """
        )
        usage = self._usage(input_tokens=100)
        model = self._model()

        result = cost.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["unresolved_reason"], "empty catalog")

    def test_cost_estimation_does_not_crash_on_uninitialized_catalog(self):
        """When the catalog fails to load, estimation returns unresolved."""
        cost._cost_catalog = False  # sentinel for failed init
        usage = self._usage(input_tokens=100)
        model = self._model()

        result = cost.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(
            result["unresolved_reason"], "pricing catalog failed to load",
        )

    def test_cli_path_regression_repo_arg_loads_real_catalog(self):
        """CLI-path regression: when repo is passed, the installed
        opsx-plan/opsx-run can discover lib.pricing and load the real
        catalog for cost estimation."""
        import sys

        cost._cost_catalog = None
        actual_repo = Path(__file__).resolve().parents[2]
        usage = self._usage(input_tokens=100000, output_tokens=50000)
        model = self._model("openai", "gpt-4o")

        # Cold-start lib.pricing: earlier tests may have imported
        # lib.pricing (e.g. via _set_catalog), which caches it in
        # sys.modules and masks the CLI-path regression.  Pop every
        # key rooted at 'lib' so the next import is a true cold load.
        saved_lib_modules = {
            k: v for k, v in sys.modules.items()
            if k == "lib" or k.startswith("lib.")
        }
        for k in saved_lib_modules:
            del sys.modules[k]

        # Temporarily remove the repo root from sys.path to simulate
        # the installed CLI environment where only the script directory
        # is on the path.
        repo_str = str(actual_repo)
        removed: list[str] = []
        while repo_str in sys.path:
            sys.path.remove(repo_str)
            removed.append(repo_str)

        try:
            result = cost.estimate_stage_cost(usage, model,
                                                         repo=actual_repo)
        finally:
            # Restore whatever we removed.
            for p in removed:
                if p not in sys.path:
                    sys.path.insert(0, p)
            # Restore saved lib modules so later tests are not affected.
            for k, v in saved_lib_modules.items():
                sys.modules[k] = v

        self.assertEqual(result["status"], "estimated",
                         f"expected estimated, got {result}")
        self.assertEqual(result["pricing_catalog_version"], "1.3.0")
        # input 100k * 2.50/mtok + output 50k * 10.00/mtok
        # = 0.25 + 0.50 = 0.75
        self.assertEqual(result["estimated_cost"], 0.75)
        self.assertIsNotNone(result["price_snapshot"])


