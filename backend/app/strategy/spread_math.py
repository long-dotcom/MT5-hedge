from __future__ import annotations

from dataclasses import dataclass


# ── New canonical direction constants ──────────────────────────────────────
LONG_LEG_A_SHORT_LEG_B = "long_leg_a_short_leg_b"
LONG_LEG_B_SHORT_LEG_A = "long_leg_b_short_leg_a"
DIRECTIONS = (LONG_LEG_A_SHORT_LEG_B, LONG_LEG_B_SHORT_LEG_A)

# ── Deprecated aliases (kept for backward compatibility) ───────────────────
LONG_HL_SHORT_MT5 = LONG_LEG_A_SHORT_LEG_B  # deprecated
LONG_MT5_SHORT_HL = LONG_LEG_B_SHORT_LEG_A  # deprecated


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


def spreads_for_direction(
    direction: str,
    leg_a_bid: float,
    leg_a_ask: float,
    leg_b_bid: float,
    leg_b_ask: float,
) -> DirectionSpreads:
    leg_a_mid = (leg_a_bid + leg_a_ask) / 2
    leg_b_mid = (leg_b_bid + leg_b_ask) / 2
    if direction == LONG_LEG_A_SHORT_LEG_B:
        entry_spread = leg_b_bid - leg_a_ask
        close_spread = leg_b_ask - leg_a_bid
        mid_spread = leg_b_mid - leg_a_mid
    elif direction == LONG_LEG_B_SHORT_LEG_A:
        entry_spread = leg_a_bid - leg_b_ask
        close_spread = leg_a_ask - leg_b_bid
        mid_spread = leg_a_mid - leg_b_mid
    else:
        raise ValueError(f"未知价差方向: {direction}")
    return DirectionSpreads(
        direction=direction,
        entry_spread=entry_spread,
        close_spread=close_spread,
        mid_spread=mid_spread,
        spread_cost=close_spread - entry_spread,
    )
