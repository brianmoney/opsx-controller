"""Cost estimation for direct-stage telemetry.

Estimates a stage's cost from usage/model telemetry against the
`lib.pricing` catalog.
"""
from __future__ import annotations

from pathlib import Path

from lib.orchestrator import base

# Subscription usage denominator configuration.
# Maps provider -> model_id -> denominator (positive float).
# Populated by the operator for subscription-billed models.
SUBSCRIPTION_DENOMINATORS: dict[str, dict[str, float]] = {}

# Module-level catalog instance (lazy-init).
_cost_catalog: object = None  # PricingCatalog | None


def _get_catalog(repo: Path | None = None):
    """Lazily initialise and return the pricing catalog.

    Returns ``(PricingCatalog, UnresolvedPrice)`` or None on failure.
    """
    global _cost_catalog
    if _cost_catalog is None:
        try:
            base.ensure_own_root_on_syspath()
            from lib.pricing import PricingCatalog, UnresolvedPrice  # noqa: F811

            _cost_catalog = (PricingCatalog(), UnresolvedPrice)
        except Exception:
            _cost_catalog = False  # Sentinel for failed init
    if _cost_catalog is False:
        return None
    return _cost_catalog  # (PricingCatalog, UnresolvedPrice) tuple


def _build_price_snapshot(resolved_price, catalog_version,
                          denom_value=None, denom_source=None):
    """Build a ``price_snapshot`` dict from a resolved pricing entry.

    For per_token models, includes all rate fields from the catalog entry.
    For subscription models, also includes denominator fields when present.
    Returns ``None`` when *resolved_price* is None.
    """
    if resolved_price is None:
        return None

    snapshot = {
        "provider": resolved_price.provider,
        "model_id": resolved_price.model_id,
        "display_name": resolved_price.display_name,
        "billing_mode": resolved_price.billing_mode,
        "currency": resolved_price.currency,
        "effective_date": resolved_price.effective_date,
        "catalog_version": catalog_version,
    }

    if resolved_price.billing_mode == "per_token":
        snapshot["input_price_per_mtok"] = resolved_price.input_price_per_mtok
        snapshot["output_price_per_mtok"] = resolved_price.output_price_per_mtok
        snapshot["cached_input_price_per_mtok"] = resolved_price.cached_input_price_per_mtok
        snapshot["reasoning_price_per_mtok"] = resolved_price.reasoning_price_per_mtok
    elif resolved_price.billing_mode == "subscription":
        snapshot["subscription_period"] = resolved_price.subscription_period
        snapshot["subscription_price"] = resolved_price.subscription_price
        if denom_value is not None:
            snapshot["usage_denominator_units"] = denom_value
            snapshot["usage_denominator_source"] = denom_source or "config"

    return snapshot


def _compute_per_token_cost(usage, resolved_price):
    """Compute per-token cost or return ``(None, unresolved_reason)``.

    When *estimated_cost* is not None the estimate succeeded.
    When *unresolved_reason* is not None estimation was not possible.
    """
    total = 0.0

    token_categories = [
        ("input_tokens", "input_price_per_mtok"),
        ("output_tokens", "output_price_per_mtok"),
        ("cached_input_tokens", "cached_input_price_per_mtok"),
        ("reasoning_tokens", "reasoning_price_per_mtok"),
    ]

    for token_field, rate_field in token_categories:
        token_count = usage.get(token_field)
        rate = getattr(resolved_price, rate_field, None)

        # null token counts are unavailable — skip
        if token_count is None:
            continue

        # positive usage with no matching rate is unresolved
        if token_count > 0 and rate is None:
            return None, f"missing rate for observed token category: {token_field}"

        if token_count > 0 and rate is not None:
            total += (token_count / 1_000_000.0) * rate

    return total, None


def _compute_subscription_cost(usage, resolved_price, denominator):
    """Compute subscription cost or return ``(None, unresolved_reason)``."""
    if denominator is None:
        return None, "missing subscription denominator"
    if not isinstance(denominator, (int, float)):
        return None, "invalid subscription denominator"
    # NaN is technically a float, but it is not a usable number.
    # NaN != NaN evaluates to True, which is the standard Python idiom for NaN
    # detection without importing math.
    if denominator != denominator:
        return None, "invalid subscription denominator"
    if denominator <= 0:
        return None, "invalid subscription denominator"

    # Derive stage usage units
    stage_units = usage.get("total_tokens")
    if stage_units is None:
        # Fall back to the sum of non-null token categories
        stage_units = 0
        found_any = False
        for field in ("input_tokens", "output_tokens",
                       "cached_input_tokens", "reasoning_tokens"):
            val = usage.get(field)
            if isinstance(val, (int, float)):
                stage_units += val
                found_any = True
        if not found_any:
            return None, "usage unavailable"

    return resolved_price.subscription_price * (stage_units / denominator), None


def estimate_stage_cost(usage, model,
                        subscription_denominators=None,
                        repo: Path | None = None):
    """Estimate stage cost from telemetry *usage*, *model*, and pricing catalog.

    Args:
        usage: Normalised usage dict from ``extract_usage_and_model``.
        model: Normalised model dict from ``extract_usage_and_model``.
        subscription_denominators: Optional ``provider -> model_id -> float``
            mapping.  When ``None``, uses the module-level
            ``SUBSCRIPTION_DENOMINATORS``.
        repo: Optional repo-root path.  Passed through to the catalog
            loader so installed orchestrator copies can discover
            ``lib.pricing``.

    Returns a dict matching the telemetry ``cost`` schema with keys
    ``status``, ``pricing_catalog_version``, ``price_snapshot``,
    ``unresolved_reason``, and ``estimated_cost``.
    """
    result = {
        "status": "unavailable",
        "pricing_catalog_version": None,
        "price_snapshot": None,
        "unresolved_reason": None,
        "estimated_cost": None,
    }

    # Check usage availability -----------------------------------------------
    if not usage.get("usage_available"):
        result["status"] = "unresolved"
        result["unresolved_reason"] = "usage unavailable"
        return result

    # Check model identity ---------------------------------------------------
    provider = (model.get("provider") or "").strip()
    model_id = (model.get("model_id") or "").strip()
    if not provider or not model_id:
        result["status"] = "unresolved"
        result["unresolved_reason"] = "model identity unavailable"
        return result

    # Resolve pricing --------------------------------------------------------
    catalog_info = _get_catalog(repo)
    if catalog_info is None:
        result["status"] = "unresolved"
        result["unresolved_reason"] = "pricing catalog failed to load"
        return result

    catalog, UnresolvedPriceCls = catalog_info
    catalog_version = catalog.get_catalog_version()
    result["pricing_catalog_version"] = catalog_version

    price_result = catalog.resolve(provider, model_id)

    if isinstance(price_result, UnresolvedPriceCls):
        result["status"] = "unresolved"
        result["unresolved_reason"] = price_result.reason
        return result

    # Compute estimate based on billing mode ---------------------------------
    if price_result.billing_mode == "per_token":
        estimated_cost, unresolved = _compute_per_token_cost(usage, price_result)
        if unresolved is not None:
            result["status"] = "unresolved"
            result["unresolved_reason"] = unresolved
        else:
            result["status"] = "estimated"
            result["estimated_cost"] = estimated_cost
            result["price_snapshot"] = _build_price_snapshot(price_result, catalog_version)

    elif price_result.billing_mode == "subscription":
        denoms = (subscription_denominators
                  if subscription_denominators is not None
                  else SUBSCRIPTION_DENOMINATORS)
        denom_value = None
        denom_source = None
        provider_denoms = denoms.get(provider, {})
        if model_id in provider_denoms:
            denom_value = provider_denoms[model_id]
            denom_source = "config"

        estimated_cost, unresolved = _compute_subscription_cost(
            usage, price_result, denom_value,
        )
        if unresolved is not None:
            result["status"] = "unresolved"
            result["unresolved_reason"] = unresolved
        else:
            result["status"] = "estimated"
            result["estimated_cost"] = estimated_cost
            result["price_snapshot"] = _build_price_snapshot(
                price_result, catalog_version, denom_value, denom_source,
            )

    return result
