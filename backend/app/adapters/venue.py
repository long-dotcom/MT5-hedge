from __future__ import annotations

from app.adapters.base import ExchangeAdapter
from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.mt5 import MT5Adapter
from app.adapters.nautilus import NautilusReadOnlyAdapter

NATIVE_VENUES = {"hyperliquid", "mt5"}


def normalize_venue(value: str) -> str:
    return (value or "").strip().lower()


def build_market_adapter(venue: str, *, live: bool = False) -> ExchangeAdapter:
    venue = normalize_venue(venue)
    if venue == "hyperliquid":
        return HyperliquidAdapter(live=live)
    if venue == "mt5":
        return MT5Adapter(live=live)
    return NautilusReadOnlyAdapter(venue, live=live)


def is_native_pair(mapping) -> bool:
    """Return True when the mapping matches the currently executable native route."""
    leg_a_venue, _ = mapping_leg(mapping, "a")
    leg_b_venue, _ = mapping_leg(mapping, "b")
    return leg_a_venue == "hyperliquid" and leg_b_venue == "mt5"


# Backward-compatible alias
is_native_hyper_mt5_pair = is_native_pair


def mapping_leg(mapping, index: str) -> tuple[str, str]:
    if index == "a":
        venue = normalize_venue(getattr(mapping, "leg_a_venue", "")) or "hyperliquid"
        symbol = str(
            getattr(mapping, "leg_a_symbol", "")
            or getattr(mapping, "leg_a_venue_symbol", "")
            or getattr(mapping, "symbol", "")
        )
        return venue, symbol
    venue = normalize_venue(getattr(mapping, "leg_b_venue", "")) or "mt5"
    symbol = str(
        getattr(mapping, "leg_b_symbol", "")
        or getattr(mapping, "mt5_symbol", "")
        or getattr(mapping, "symbol", "")
    )
    return venue, symbol


def nautilus_venues_from_mappings(mappings) -> list[str]:
    venues: list[str] = []
    for mapping in mappings:
        for index in ("a", "b"):
            venue, _ = mapping_leg(mapping, index)
            if venue in NATIVE_VENUES or venue in venues:
                continue
            venues.append(venue)
    return venues
