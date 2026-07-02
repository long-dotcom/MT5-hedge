from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.db.models import HedgeGroup
from app.market.quotes import quote_cache
from app.strategy.spread_math import spreads_for_direction
from app.adapters.venue import mapping_leg


def hedge_group_spreads(group: HedgeGroup, mapping=None) -> dict[str, Any]:
    if mapping is not None:
        leg_a_venue, _ = mapping_leg(mapping, "a")
        leg_b_venue, _ = mapping_leg(mapping, "b")
    else:
        leg_a_venue, leg_b_venue = "hyperliquid", "mt5"
    hl = quote_cache.latest(leg_a_venue, group.symbol)
    mt5 = quote_cache.latest(leg_b_venue, group.symbol)
    if not hl or not mt5:
        return {
            "current_entry_spread": None,
            "current_close_spread": None,
            "quote_time_diff_ms": None,
            "quote_age_ms": None,
        }
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    spreads = spreads_for_direction(group.direction, hl.bid, hl.ask, mt5.bid, mt5.ask)
    return {
        "current_entry_spread": spreads.entry_spread,
        "current_close_spread": spreads.close_spread,
        "current_mid_spread": spreads.mid_spread,
        "current_spread_cost": spreads.spread_cost,
        "quote_time_diff_ms": abs((hl.local_recv_ts - mt5.local_recv_ts).total_seconds() * 1000),
        "quote_age_ms": max((now - hl.local_recv_ts).total_seconds() * 1000, (now - mt5.local_recv_ts).total_seconds() * 1000),
    }
