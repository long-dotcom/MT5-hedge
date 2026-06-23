from __future__ import annotations

from datetime import datetime
from typing import Any

from app.db.models import HedgeGroup
from app.market.quotes import quote_cache


def hedge_group_spreads(group: HedgeGroup) -> dict[str, Any]:
    hl = quote_cache.latest("hyperliquid", group.symbol)
    mt5 = quote_cache.latest("mt5", group.symbol)
    if not hl or not mt5:
        return {
            "current_entry_spread": None,
            "current_close_spread": None,
            "quote_time_diff_ms": None,
            "quote_age_ms": None,
        }
    now = datetime.utcnow()
    return {
        "current_entry_spread": _entry_spread(group.direction, hl.bid, hl.ask, mt5.bid, mt5.ask),
        "current_close_spread": _close_spread(group.direction, hl.bid, hl.ask, mt5.bid, mt5.ask),
        "quote_time_diff_ms": abs((hl.local_recv_ts - mt5.local_recv_ts).total_seconds() * 1000),
        "quote_age_ms": max((now - hl.local_recv_ts).total_seconds() * 1000, (now - mt5.local_recv_ts).total_seconds() * 1000),
    }


def _entry_spread(direction: str, hl_bid: float, hl_ask: float, mt5_bid: float, mt5_ask: float) -> float:
    if direction == "long_hyperliquid_short_mt5":
        return mt5_bid - hl_ask
    return hl_bid - mt5_ask


def _close_spread(direction: str, hl_bid: float, hl_ask: float, mt5_bid: float, mt5_ask: float) -> float:
    if direction == "long_hyperliquid_short_mt5":
        return mt5_ask - hl_bid
    return hl_ask - mt5_bid
