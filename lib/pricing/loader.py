"""Pricing catalog loader — parses ``catalog.toml`` and provides typed lookups."""

from __future__ import annotations

import tomllib
from datetime import date
from pathlib import Path
from typing import Optional

from lib.pricing.types import CatalogEntry, CatalogMetadata, ResolvedPrice, UnresolvedPrice

# ---------------------------------------------------------------------------
# Recognised ISO 4217 currency codes (minimal set matching the catalog)
# ---------------------------------------------------------------------------
_VALID_CURRENCY_CODES: frozenset[str] = frozenset(
    {
        "AUD",
        "BRL",
        "CAD",
        "CHF",
        "CNY",
        "EUR",
        "GBP",
        "HKD",
        "INR",
        "JPY",
        "KRW",
        "MXN",
        "NOK",
        "NZD",
        "RUB",
        "SEK",
        "SGD",
        "TRY",
        "USD",
        "ZAR",
    }
)

_VALID_BILLING_MODES: frozenset[str] = frozenset({"per_token", "subscription"})
_VALID_SUBSCRIPTION_PERIODS: frozenset[str] = frozenset({"monthly", "yearly"})


class CatalogLoadError(Exception):
    """Raised when the catalog file contains one or more invalid entries.

    Attributes:
        entry_index: 0-based index of the failing ``[[entries]]`` array element.
        field:        name of the field that failed validation (when applicable).
    """

    def __init__(self, message: str, *, entry_index: Optional[int] = None, field: Optional[str] = None) -> None:
        self.entry_index = entry_index
        self.field = field
        super().__init__(message)


class PricingCatalog:
    """Loads a versioned TOML pricing catalog and resolves model prices.

    Typical usage::

        catalog = PricingCatalog()
        result = catalog.resolve("openai", "gpt-4o")
        if isinstance(result, ResolvedPrice):
            print(result.input_price_per_mtok)
        else:
            print(f"Unresolved: {result.reason}")
    """

    def __init__(self, catalog_path: Optional[Path] = None) -> None:
        """Create a new catalog instance.

        Args:
            catalog_path: Path to a ``catalog.toml`` file.  Defaults to the
                ``catalog.toml`` shipped in the same package directory.
        """
        if catalog_path is None:
            catalog_path = Path(__file__).resolve().parent / "catalog.toml"

        self._catalog_path: Path = catalog_path
        self._metadata: CatalogMetadata
        self._entries: list[CatalogEntry] = []
        # (provider, model_id) -> list[CatalogEntry] sorted by effective_date desc
        self._by_provider_model: dict[tuple[str, str], list[CatalogEntry]] = {}

        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, provider: str, model_id: str) -> ResolvedPrice | UnresolvedPrice:
        """Look up pricing for the given provider and model identifier.

        Returns a ``ResolvedPrice`` when a matching entry exists.  When
        multiple entries share the same ``(provider, model_id)`` pair the
        entry with the latest ``effective_date`` is returned.

        Returns an ``UnresolvedPrice`` when no entry matches, with a
        ``reason`` describing why resolution failed.
        """
        if not self._entries:
            return UnresolvedPrice(provider=provider, model_id=model_id, reason="empty catalog")

        key = (provider, model_id)
        entries = self._by_provider_model.get(key)
        if entries is None:
            provider_exists = any(k[0] == provider for k in self._by_provider_model)
            reason = "unknown model" if provider_exists else "unknown provider"
            return UnresolvedPrice(provider=provider, model_id=model_id, reason=reason)

        # Latest entry by effective_date (list is sorted descending)
        entry = entries[0]
        return ResolvedPrice(
            provider=entry.provider,
            model_id=entry.model_id,
            display_name=entry.display_name,
            billing_mode=entry.billing_mode,
            currency=entry.currency,
            effective_date=entry.effective_date,
            input_price_per_mtok=entry.input_price_per_mtok,
            output_price_per_mtok=entry.output_price_per_mtok,
            cached_input_price_per_mtok=entry.cached_input_price_per_mtok,
            reasoning_price_per_mtok=entry.reasoning_price_per_mtok,
            subscription_period=entry.subscription_period,
            subscription_price=entry.subscription_price,
            notes=entry.notes,
        )

    def get_catalog_version(self) -> str:
        """Return the ``version`` string from the ``[catalog]`` metadata section."""
        return self._metadata.version

    # ------------------------------------------------------------------
    # Internal: loading & validation
    # ------------------------------------------------------------------

    def _load(self) -> None:
        with open(self._catalog_path, "rb") as fh:
            data = tomllib.load(fh)

        # --- metadata ----------------------------------------------------
        catalog_section = data.get("catalog", {})
        self._metadata = CatalogMetadata(
            version=str(catalog_section.get("version", "")),
            updated=str(catalog_section.get("updated", "")),
        )

        # --- entries -----------------------------------------------------
        raw_entries: list[dict] = data.get("entries") or []
        for idx, raw in enumerate(raw_entries):
            entry = self._validate_one(idx, raw)
            self._entries.append(entry)
            key = (entry.provider, entry.model_id)
            self._by_provider_model.setdefault(key, []).append(entry)

        # Sort each group so the first element is the latest effective_date.
        for group in self._by_provider_model.values():
            group.sort(key=lambda e: e.effective_date, reverse=True)

    def _validate_one(self, idx: int, raw: dict) -> CatalogEntry:
        """Validate a single raw TOML entry dict and return a ``CatalogEntry``.

        Raises ``CatalogLoadError`` on any validation failure.
        """
        # -- required string fields ---------------------------------------
        provider = raw.get("provider")
        model_id = raw.get("model_id")
        display_name = raw.get("display_name")
        billing_mode = raw.get("billing_mode")
        currency = raw.get("currency")
        effective_date = raw.get("effective_date")

        for field_name, value in [
            ("provider", provider),
            ("model_id", model_id),
            ("display_name", display_name),
            ("billing_mode", billing_mode),
            ("currency", currency),
            ("effective_date", effective_date),
        ]:
            if not value:
                raise CatalogLoadError(
                    f"Entry {idx}: missing required field '{field_name}'",
                    entry_index=idx,
                    field=field_name,
                )

        billing_mode = str(billing_mode)
        if billing_mode not in _VALID_BILLING_MODES:
            raise CatalogLoadError(
                f"Entry {idx}: invalid billing_mode '{billing_mode}' "
                f"(expected 'per_token' or 'subscription')",
                entry_index=idx,
                field="billing_mode",
            )

        currency = str(currency).upper()
        if currency not in _VALID_CURRENCY_CODES:
            raise CatalogLoadError(
                f"Entry {idx}: invalid currency '{currency}' "
                f"(not a recognised ISO 4217 code)",
                entry_index=idx,
                field="currency",
            )

        # -- effective_date (ISO-8601) ------------------------------------
        effective_date = str(effective_date)
        try:
            date.fromisoformat(effective_date)
        except (ValueError, TypeError):
            raise CatalogLoadError(
                f"Entry {idx}: invalid effective_date '{effective_date}' "
                f"(expected ISO-8601 date, e.g. 2025-01-01)",
                entry_index=idx,
                field="effective_date",
            )

        # -- per-mtok rate fields -----------------------------------------
        input_price = raw.get("input_price_per_mtok")
        output_price = raw.get("output_price_per_mtok")
        cached_input_price = raw.get("cached_input_price_per_mtok")
        reasoning_price = raw.get("reasoning_price_per_mtok")

        if billing_mode == "per_token":
            if input_price is None:
                raise CatalogLoadError(
                    f"Entry {idx}: per-token entry missing 'input_price_per_mtok'",
                    entry_index=idx,
                    field="input_price_per_mtok",
                )
            if not isinstance(input_price, (int, float)) or input_price <= 0:
                raise CatalogLoadError(
                    f"Entry {idx}: per-token entry has invalid "
                    f"input_price_per_mtok={input_price!r} (must be positive)",
                    entry_index=idx,
                    field="input_price_per_mtok",
                )

        # Any present per-mtok rate must be non-negative.
        for field_name, value in [
            ("input_price_per_mtok", input_price),
            ("output_price_per_mtok", output_price),
            ("cached_input_price_per_mtok", cached_input_price),
            ("reasoning_price_per_mtok", reasoning_price),
        ]:
            if value is not None and (not isinstance(value, (int, float)) or value < 0):
                raise CatalogLoadError(
                    f"Entry {idx}: negative {field_name}={value!r}",
                    entry_index=idx,
                    field=field_name,
                )

        # -- subscription fields ------------------------------------------
        sub_period = raw.get("subscription_period")
        sub_price = raw.get("subscription_price")

        if billing_mode == "subscription":
            if sub_price is None:
                raise CatalogLoadError(
                    f"Entry {idx}: subscription entry missing 'subscription_price'",
                    entry_index=idx,
                    field="subscription_price",
                )
            if sub_period is None:
                raise CatalogLoadError(
                    f"Entry {idx}: subscription entry missing 'subscription_period'",
                    entry_index=idx,
                    field="subscription_period",
                )
            sub_period = str(sub_period)
            if sub_period not in _VALID_SUBSCRIPTION_PERIODS:
                raise CatalogLoadError(
                    f"Entry {idx}: invalid subscription_period '{sub_period}' "
                    f"(expected 'monthly' or 'yearly')",
                    entry_index=idx,
                    field="subscription_period",
                )

        # -- build entry --------------------------------------------------
        return CatalogEntry(
            provider=str(provider),
            model_id=str(model_id),
            display_name=str(display_name),
            billing_mode=billing_mode,
            currency=currency,
            effective_date=effective_date,
            input_price_per_mtok=float(input_price) if input_price is not None else None,
            output_price_per_mtok=float(output_price) if output_price is not None else None,
            cached_input_price_per_mtok=float(cached_input_price) if cached_input_price is not None else None,
            reasoning_price_per_mtok=float(reasoning_price) if reasoning_price is not None else None,
            subscription_period=str(sub_period) if sub_period is not None else None,
            subscription_price=float(sub_price) if sub_price is not None else None,
            notes=str(raw.get("notes")) if raw.get("notes") is not None else None,
        )
