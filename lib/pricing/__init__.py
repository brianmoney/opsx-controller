"""Pricing catalog package.

Provides a versioned, operator-maintainable catalog of AI model pricing
entries and a loader with typed lookup results for downstream cost estimation.
"""

from lib.pricing.loader import PROVIDER_ALIASES, CatalogLoadError, PricingCatalog
from lib.pricing.types import CatalogEntry, CatalogMetadata, ResolvedPrice, UnresolvedPrice

__all__ = [
    "CatalogEntry",
    "CatalogMetadata",
    "CatalogLoadError",
    "PROVIDER_ALIASES",
    "PricingCatalog",
    "ResolvedPrice",
    "UnresolvedPrice",
]
