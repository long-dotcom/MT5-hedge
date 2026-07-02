from dataclasses import dataclass


@dataclass
class CostBreakdown:
    leg_a_fee: float
    leg_a_spread: float
    leg_a_funding: float
    leg_b_spread: float
    leg_b_commission: float
    leg_b_swap: float
    slippage: float
    fx_cost: float
    leg_a_fee_rate: float = 0.00045
    leg_a_funding_rate: float = 0.00010
    leg_b_commission_rate: float = 0.00035
    source: str = "static"

    # Backward-compatible aliases
    @property
    def hyperliquid_fee(self) -> float:
        return self.leg_a_fee

    @property
    def hyperliquid_spread(self) -> float:
        return self.leg_a_spread

    @property
    def hyperliquid_funding(self) -> float:
        return self.leg_a_funding

    @property
    def mt5_spread(self) -> float:
        return self.leg_b_spread

    @property
    def mt5_commission(self) -> float:
        return self.leg_b_commission

    @property
    def mt5_swap(self) -> float:
        return self.leg_b_swap

    @property
    def total(self) -> float:
        return (
            self.leg_a_fee
            + self.leg_a_spread
            + self.leg_a_funding
            + self.leg_b_spread
            + self.leg_b_commission
            + self.leg_b_swap
            + self.slippage
            + self.fx_cost
        )


def estimate_cost(
    notional: float,
    leg_b_bid: float,
    leg_b_ask: float,
    max_slippage_bps: float,
    quantity: float = 0.0,
    leg_a_bid: float = 0.0,
    leg_a_ask: float = 0.0,
    leg_a_fee_rate: float = 0.00045,
    leg_a_fee_round_trips: float = 2.0,
    leg_a_close_fee_rate: float | None = None,
    leg_a_funding_rate: float = 0.00010,
    leg_a_side: str = "buy",
    leg_b_commission_rate: float = 0.00035,
    leg_b_swap_cost: float | None = None,
    holding_hours: float = 4.0,
    leg_b_spread_rebate_rate: float = 0.0,
    fx_cost_rate: float = 0.0,
    source: str = "static",
) -> CostBreakdown:
    leg_b_spread_cost = abs(leg_b_ask - leg_b_bid) / max((leg_b_ask + leg_b_bid) / 2, 1) * notional * (1 - leg_b_spread_rebate_rate)
    # 中文注释：Hyperliquid funding 是持仓收益/成本，正 funding 通常多头支付、空头收取。
    funding_direction = 1 if leg_a_side == "buy" else -1
    leg_a_spread_cost = max(leg_a_ask - leg_a_bid, 0) * quantity if quantity > 0 else 0.0
    return CostBreakdown(
        leg_a_fee=notional * _fee_multiplier(leg_a_fee_rate, leg_a_close_fee_rate, leg_a_fee_round_trips),
        leg_a_spread=leg_a_spread_cost,
        leg_a_funding=notional * leg_a_funding_rate * max(holding_hours, 0) * funding_direction,
        leg_b_spread=leg_b_spread_cost,
        leg_b_commission=notional * leg_b_commission_rate,
        leg_b_swap=leg_b_swap_cost if leg_b_swap_cost is not None else 0.0,
        slippage=notional * max_slippage_bps / 10_000,
        fx_cost=notional * fx_cost_rate,
        leg_a_fee_rate=leg_a_fee_rate,
        leg_a_funding_rate=leg_a_funding_rate,
        leg_b_commission_rate=leg_b_commission_rate,
        source=source,
    )


def _fee_multiplier(open_fee_rate: float, close_fee_rate: float | None, round_trips: float) -> float:
    if close_fee_rate is not None:
        return open_fee_rate + close_fee_rate
    return open_fee_rate * round_trips
