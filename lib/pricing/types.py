"""Type definitions for the pricing catalog."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CatalogEntry:
    """A raw catalog entry as loaded from the TOML file.

    Fields match the TOML ``[[entries]]`` schema exactly.
    """

    provider: str
    model_id: str
    display_name: str
    billing_mode: str
    currency: str
    effective_date: str
    input_price_per_mtok: Optional[float] = None
    output_price_per_mtok: Optional[float] = None
    cached_input_price_per_mtok: Optional[float] = None
    reasoning_price_per_mtok: Optional[float] = None
    subscription_period: Optional[str] = None
    subscription_price: Optional[float] = None
    notes: Optional[str] = None


@dataclass
class CatalogMetadata:
    """Metadata from the ``[catalog]`` section of the catalog file."""

    version: str
    updated: str


@dataclass
class ResolvedPrice:
    """Result of a successful catalog lookup.

    Contains all pricing fields needed by downstream cost estimation.
    Per-token rate fields are ``None`` for subscription models, and
    subscription fields are ``None`` for per-token models.
    """

    provider: str
    model_id: str
    display_name: str
    billing_mode: str
    currency: str
    effective_date: str
    input_price_per_mtok: Optional[float] = None
    output_price_per_mtok: Optional[float] = None
    cached_input_price_per_mtok: Optional[float] = None
    reasoning_price_per_mtok: Optional[float] = None
    subscription_period: Optional[str] = None
    subscription_price: Optional[float] = None
    notes: Optional[str] = None


@dataclass
class UnresolvedPrice:
    """Result of a failed catalog lookup.

    The ``reason`` field explains why resolution failed so downstream
    code can populate ``cost.unresolved_reason`` in telemetry.
    """

    provider: Optional[str] = None
    model_id: Optional[str] = None
    reason: str = ""
