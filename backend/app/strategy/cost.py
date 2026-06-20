from dataclasses import dataclass


@dataclass
class CostBreakdown:
    hyperliquid_fee: float
    hyperliquid_spread: float
    hyperliquid_funding: float
    mt5_spread: float
    mt5_commission: float
    mt5_swap: float
    slippage: float
    fx_cost: float
    hyperliquid_fee_rate: float = 0.00045
    hyperliquid_funding_rate: float = 0.00010
    mt5_commission_rate: float = 0.00035
    source: str = "static"

    @property
    def total(self) -> float:
        return (
            self.hyperliquid_fee
            + self.hyperliquid_spread
            + self.hyperliquid_funding
            + self.mt5_spread
            + self.mt5_commission
            + self.mt5_swap
            + self.slippage
            + self.fx_cost
        )


def estimate_cost(
    notional: float,
    mt5_bid: float,
    mt5_ask: float,
    max_slippage_bps: float,
    quantity: float = 0.0,
    hyperliquid_bid: float = 0.0,
    hyperliquid_ask: float = 0.0,
    hyperliquid_fee_rate: float = 0.00045,
    hyperliquid_fee_round_trips: float = 2.0,
    hyperliquid_close_fee_rate: float | None = None,
    hyperliquid_funding_rate: float = 0.00010,
    hyperliquid_side: str = "buy",
    mt5_commission_rate: float = 0.00035,
    mt5_swap_cost: float | None = None,
    holding_hours: float = 4.0,
    mt5_spread_rebate_rate: float = 0.0,
    fx_cost_rate: float = 0.0,
    source: str = "static",
) -> CostBreakdown:
    mt5_spread_cost = abs(mt5_ask - mt5_bid) / max((mt5_ask + mt5_bid) / 2, 1) * notional * (1 - mt5_spread_rebate_rate)
    # 中文注释：Hyperliquid funding 是持仓收益/成本，正 funding 通常多头支付、空头收取。
    funding_direction = 1 if hyperliquid_side == "buy" else -1
    hyperliquid_spread_cost = max(hyperliquid_ask - hyperliquid_bid, 0) * quantity if quantity > 0 else 0.0
    return CostBreakdown(
        hyperliquid_fee=notional * _fee_multiplier(hyperliquid_fee_rate, hyperliquid_close_fee_rate, hyperliquid_fee_round_trips),
        hyperliquid_spread=hyperliquid_spread_cost,
        hyperliquid_funding=notional * hyperliquid_funding_rate * max(holding_hours, 0) * funding_direction,
        mt5_spread=mt5_spread_cost,
        mt5_commission=notional * mt5_commission_rate,
        mt5_swap=mt5_swap_cost if mt5_swap_cost is not None else 0.0,
        slippage=notional * max_slippage_bps / 10_000,
        fx_cost=notional * fx_cost_rate,
        hyperliquid_fee_rate=hyperliquid_fee_rate,
        hyperliquid_funding_rate=hyperliquid_funding_rate,
        mt5_commission_rate=mt5_commission_rate,
        source=source,
    )


def _fee_multiplier(open_fee_rate: float, close_fee_rate: float | None, round_trips: float) -> float:
    if close_fee_rate is not None:
        return open_fee_rate + close_fee_rate
    return open_fee_rate * round_trips
