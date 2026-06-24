from __future__ import annotations

from dataclasses import dataclass


LONG_HL_SHORT_MT5 = "long_hyperliquid_short_mt5"
LONG_MT5_SHORT_HL = "long_mt5_short_hyperliquid"
DIRECTIONS = (LONG_HL_SHORT_MT5, LONG_MT5_SHORT_HL)


@dataclass(frozen=True)
class DirectionSpreads:
    direction: str
    entry_spread: float
    close_spread: float
    mid_spread: float
    spread_cost: float

    @property
    def gross_spread(self) -> float:
        return self.entry_spread


def spreads_for_direction(direction: str, hl_bid: float, hl_ask: float, mt5_bid: float, mt5_ask: float) -> DirectionSpreads:
    hl_mid = (hl_bid + hl_ask) / 2
    mt5_mid = (mt5_bid + mt5_ask) / 2
    if direction == LONG_HL_SHORT_MT5:
        entry_spread = mt5_bid - hl_ask
        close_spread = mt5_ask - hl_bid
        mid_spread = mt5_mid - hl_mid
    elif direction == LONG_MT5_SHORT_HL:
        entry_spread = hl_bid - mt5_ask
        close_spread = hl_ask - mt5_bid
        mid_spread = hl_mid - mt5_mid
    else:
        raise ValueError(f"未知价差方向: {direction}")
    return DirectionSpreads(
        direction=direction,
        entry_spread=entry_spread,
        close_spread=close_spread,
        mid_spread=mid_spread,
        spread_cost=close_spread - entry_spread,
    )
