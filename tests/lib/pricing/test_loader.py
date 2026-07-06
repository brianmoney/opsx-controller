"""Unit tests for lib.pricing.loader and supporting types."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

from lib.pricing.loader import CatalogLoadError, PricingCatalog
from lib.pricing.types import ResolvedPrice, UnresolvedPrice


def _write_catalog(content: str) -> Path:
    """Write *content* to a temporary ``.toml`` file and return its path."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False, encoding="utf-8")
    tmp.write(dedent(content))
    tmp.close()
    return Path(tmp.name)


# ---------------------------------------------------------------------------
# 4.2  ResolvedPrice for known per-token model
# 4.12 get_catalog_version()
# ---------------------------------------------------------------------------


class ResolvePerTokenTests(unittest.TestCase):
    def test_resolve_per_token_model(self) -> None:
        path = _write_catalog(
            """\
            [catalog]
            version = "1.2.3"
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
            notes = "test"
            """
        )
        catalog = PricingCatalog(catalog_path=path)
        result = catalog.resolve("openai", "gpt-4o")
        self.assertIsInstance(result, ResolvedPrice)
        self.assertEqual(result.provider, "openai")
        self.assertEqual(result.model_id, "gpt-4o")
        self.assertEqual(result.display_name, "GPT-4o")
        self.assertEqual(result.billing_mode, "per_token")
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.input_price_per_mtok, 2.50)
        self.assertEqual(result.output_price_per_mtok, 10.00)
        self.assertEqual(result.cached_input_price_per_mtok, 1.25)
        self.assertIsNone(result.reasoning_price_per_mtok)
        self.assertIsNone(result.subscription_period)
        self.assertIsNone(result.subscription_price)
        self.assertEqual(result.effective_date, "2025-01-01")
        self.assertEqual(result.notes, "test")

        # 4.12
        self.assertEqual(catalog.get_catalog_version(), "1.2.3")


# ---------------------------------------------------------------------------
# 4.3  ResolvedPrice for subscription model
# ---------------------------------------------------------------------------


class ResolveSubscriptionTests(unittest.TestCase):
    def test_resolve_subscription_model(self) -> None:
        path = _write_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"

            [[entries]]
            provider = "github"
            model_id = "copilot"
            display_name = "Copilot"
            billing_mode = "subscription"
            currency = "USD"
            subscription_period = "monthly"
            subscription_price = 10.00
            effective_date = "2025-01-01"
            """
        )
        catalog = PricingCatalog(catalog_path=path)
        result = catalog.resolve("github", "copilot")
        self.assertIsInstance(result, ResolvedPrice)
        self.assertEqual(result.billing_mode, "subscription")
        self.assertEqual(result.subscription_period, "monthly")
        self.assertEqual(result.subscription_price, 10.0)
        # All per-mtok rates must be None for subscription models.
        self.assertIsNone(result.input_price_per_mtok)
        self.assertIsNone(result.output_price_per_mtok)
        self.assertIsNone(result.cached_input_price_per_mtok)
        self.assertIsNone(result.reasoning_price_per_mtok)


# ---------------------------------------------------------------------------
# 4.4  UnresolvedPrice — unknown model
# ---------------------------------------------------------------------------


class UnresolvedUnknownModelTests(unittest.TestCase):
    def test_unknown_model_reason(self) -> None:
        path = _write_catalog(
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
            effective_date = "2025-01-01"
            """
        )
        catalog = PricingCatalog(catalog_path=path)
        result = catalog.resolve("openai", "gpt-99")
        self.assertIsInstance(result, UnresolvedPrice)
        self.assertIn("unknown model", result.reason)


# ---------------------------------------------------------------------------
# 4.5  UnresolvedPrice — unknown provider
# ---------------------------------------------------------------------------


class UnresolvedUnknownProviderTests(unittest.TestCase):
    def test_unknown_provider_reason(self) -> None:
        path = _write_catalog(
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
            effective_date = "2025-01-01"
            """
        )
        catalog = PricingCatalog(catalog_path=path)
        result = catalog.resolve("nonexistent", "model")
        self.assertIsInstance(result, UnresolvedPrice)
        self.assertIn("unknown provider", result.reason)


# ---------------------------------------------------------------------------
# 4.6  Multiple entries — latest effective_date wins
# ---------------------------------------------------------------------------


class MultipleEntriesLatestWinsTests(unittest.TestCase):
    def test_latest_effective_date_wins(self) -> None:
        path = _write_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"

            [[entries]]
            provider = "openai"
            model_id = "gpt-4o"
            display_name = "GPT-4o (old)"
            billing_mode = "per_token"
            currency = "USD"
            input_price_per_mtok = 2.50
            effective_date = "2025-01-01"

            [[entries]]
            provider = "openai"
            model_id = "gpt-4o"
            display_name = "GPT-4o (new)"
            billing_mode = "per_token"
            currency = "USD"
            input_price_per_mtok = 2.00
            effective_date = "2025-07-01"
            """
        )
        catalog = PricingCatalog(catalog_path=path)
        result = catalog.resolve("openai", "gpt-4o")
        self.assertIsInstance(result, ResolvedPrice)
        self.assertEqual(result.input_price_per_mtok, 2.00)
        self.assertEqual(result.effective_date, "2025-07-01")
        self.assertEqual(result.display_name, "GPT-4o (new)")


# ---------------------------------------------------------------------------
# 4.7  Missing required field raises CatalogLoadError
# ---------------------------------------------------------------------------


class MissingRequiredFieldTests(unittest.TestCase):
    def _assert_missing_field_error(self, content: str, expected_field: str) -> None:
        path = _write_catalog(content)
        with self.assertRaises(CatalogLoadError) as ctx:
            PricingCatalog(catalog_path=path)
        self.assertIsNotNone(ctx.exception.entry_index)
        self.assertEqual(ctx.exception.field, expected_field)
        self.assertIn(expected_field, str(ctx.exception))

    def test_missing_provider(self) -> None:
        self._assert_missing_field_error(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"

            [[entries]]
            model_id = "gpt-4o"
            display_name = "GPT-4o"
            billing_mode = "per_token"
            currency = "USD"
            input_price_per_mtok = 2.50
            effective_date = "2025-01-01"
            """,
            "provider",
        )

    def test_missing_model_id(self) -> None:
        self._assert_missing_field_error(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"

            [[entries]]
            provider = "openai"
            display_name = "GPT-4o"
            billing_mode = "per_token"
            currency = "USD"
            input_price_per_mtok = 2.50
            effective_date = "2025-01-01"
            """,
            "model_id",
        )

    def test_missing_billing_mode(self) -> None:
        self._assert_missing_field_error(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"

            [[entries]]
            provider = "openai"
            model_id = "gpt-4o"
            display_name = "GPT-4o"
            currency = "USD"
            input_price_per_mtok = 2.50
            effective_date = "2025-01-01"
            """,
            "billing_mode",
        )

    def test_missing_currency(self) -> None:
        self._assert_missing_field_error(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"

            [[entries]]
            provider = "openai"
            model_id = "gpt-4o"
            display_name = "GPT-4o"
            billing_mode = "per_token"
            input_price_per_mtok = 2.50
            effective_date = "2025-01-01"
            """,
            "currency",
        )


# ---------------------------------------------------------------------------
# 4.8  Negative input_price_per_mtok raises CatalogLoadError
# ---------------------------------------------------------------------------


class NegativeRateTests(unittest.TestCase):
    def test_negative_input_price(self) -> None:
        path = _write_catalog(
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
            input_price_per_mtok = -1.0
            effective_date = "2025-01-01"
            """
        )
        with self.assertRaises(CatalogLoadError) as ctx:
            PricingCatalog(catalog_path=path)
        self.assertEqual(ctx.exception.field, "input_price_per_mtok")
        self.assertIn("input_price_per_mtok", str(ctx.exception))


# ---------------------------------------------------------------------------
# 4.9  Unknown billing_mode raises CatalogLoadError
# ---------------------------------------------------------------------------


class UnknownBillingModeTests(unittest.TestCase):
    def test_unknown_billing_mode(self) -> None:
        path = _write_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"

            [[entries]]
            provider = "openai"
            model_id = "gpt-4o"
            display_name = "GPT-4o"
            billing_mode = "flat_rate"
            currency = "USD"
            input_price_per_mtok = 2.50
            effective_date = "2025-01-01"
            """
        )
        with self.assertRaises(CatalogLoadError) as ctx:
            PricingCatalog(catalog_path=path)
        self.assertEqual(ctx.exception.field, "billing_mode")
        self.assertIn("flat_rate", str(ctx.exception))


# ---------------------------------------------------------------------------
# 4.10 Subscription entry missing subscription_price raises CatalogLoadError
# ---------------------------------------------------------------------------


class MissingSubscriptionPriceTests(unittest.TestCase):
    def test_missing_subscription_price(self) -> None:
        path = _write_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"

            [[entries]]
            provider = "github"
            model_id = "copilot"
            display_name = "Copilot"
            billing_mode = "subscription"
            currency = "USD"
            subscription_period = "monthly"
            effective_date = "2025-01-01"
            """
        )
        with self.assertRaises(CatalogLoadError) as ctx:
            PricingCatalog(catalog_path=path)
        self.assertEqual(ctx.exception.field, "subscription_price")
        self.assertIn("subscription_price", str(ctx.exception))


# ---------------------------------------------------------------------------
# 4.11 Empty catalog — loads successfully, all resolves return UnresolvedPrice
# ---------------------------------------------------------------------------


class EmptyCatalogTests(unittest.TestCase):
    def test_empty_catalog_loads_and_returns_unresolved(self) -> None:
        path = _write_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            """
        )
        catalog = PricingCatalog(catalog_path=path)
        result = catalog.resolve("any", "model")
        self.assertIsInstance(result, UnresolvedPrice)
        self.assertEqual(result.reason, "empty catalog")


# ---------------------------------------------------------------------------
# 4.13 Optional rate fields omitted — loads and returns None for those fields
# ---------------------------------------------------------------------------


class OptionalRateFieldsTests(unittest.TestCase):
    def test_optional_rates_omitted(self) -> None:
        path = _write_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"

            [[entries]]
            provider = "minimal"
            model_id = "mini"
            display_name = "Mini Model"
            billing_mode = "per_token"
            currency = "USD"
            input_price_per_mtok = 0.10
            effective_date = "2025-01-01"
            """
        )
        catalog = PricingCatalog(catalog_path=path)
        result = catalog.resolve("minimal", "mini")
        self.assertIsInstance(result, ResolvedPrice)
        self.assertEqual(result.input_price_per_mtok, 0.10)
        self.assertIsNone(result.output_price_per_mtok)
        self.assertIsNone(result.cached_input_price_per_mtok)
        self.assertIsNone(result.reasoning_price_per_mtok)


# ---------------------------------------------------------------------------
# 4.14 Whitespace — exact match required (no normalisation)
# ---------------------------------------------------------------------------


class WhitespaceExactMatchTests(unittest.TestCase):
    def test_whitespace_differs_returns_unresolved(self) -> None:
        path = _write_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"

            [[entries]]
            provider = " openai"
            model_id = "gpt-4o "
            display_name = "GPT-4o"
            billing_mode = "per_token"
            currency = "USD"
            input_price_per_mtok = 2.50
            effective_date = "2025-01-01"
            """
        )
        catalog = PricingCatalog(catalog_path=path)
        # Lookup without surrounding whitespace
        result = catalog.resolve("openai", "gpt-4o")
        self.assertIsInstance(result, UnresolvedPrice)

        # Lookup with matching whitespace
        result2 = catalog.resolve(" openai", "gpt-4o ")
        self.assertIsInstance(result2, ResolvedPrice)


# ---------------------------------------------------------------------------
# Regression: ISO 4217 currencies outside the old hardcoded subset (e.g. AED)
# ---------------------------------------------------------------------------


class Iso4217CurrencyRegressionTests(unittest.TestCase):
    """Ensure that currencies outside the original minimal subset are accepted."""

    def test_aed_currency_accepted(self) -> None:
        path = _write_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"

            [[entries]]
            provider = "openai"
            model_id = "gpt-4o"
            display_name = "GPT-4o"
            billing_mode = "per_token"
            currency = "AED"
            input_price_per_mtok = 2.50
            effective_date = "2025-01-01"
            """
        )
        catalog = PricingCatalog(catalog_path=path)
        result = catalog.resolve("openai", "gpt-4o")
        self.assertIsInstance(result, ResolvedPrice)
        self.assertEqual(result.currency, "AED")

    def test_sek_currency_accepted(self) -> None:
        path = _write_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"

            [[entries]]
            provider = "anthropic"
            model_id = "claude-sonnet-4"
            display_name = "Claude Sonnet 4"
            billing_mode = "per_token"
            currency = "SEK"
            input_price_per_mtok = 3.00
            effective_date = "2025-01-01"
            """
        )
        catalog = PricingCatalog(catalog_path=path)
        result = catalog.resolve("anthropic", "claude-sonnet-4")
        self.assertIsInstance(result, ResolvedPrice)
        self.assertEqual(result.currency, "SEK")
