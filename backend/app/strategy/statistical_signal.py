from dataclasses import dataclass
from statistics import mean, pstdev

from sqlalchemy.orm import Session

from app.analytics.spreads import SpreadPoint, load_spread_points
from app.db.models import StrategySetting
from app.strategy.signals import SignalResult, evaluate_signal


@dataclass
class StatisticalSignal:
    result: SignalResult
    reachable_entry: float
    cost_guard: float
    strong_entry: float
    exit_target: float
    overheat: float
    sample_count: int


def evaluate_entry_signal(
    db: Session,
    strategy: StrategySetting,
    symbol: str,
    direction: str,
    current_spread: float,
    unit_cost: float,
    unit_net_profit: float,
    total_net_profit: float,
    annualized_return: float,
) -> StatisticalSignal:
    if strategy.signal_mode != "statistical":
        return StatisticalSignal(
            result=evaluate_signal(total_net_profit, annualized_return, strategy.min_net_profit, strategy.min_annualized_return),
            reachable_entry=0.0,
            cost_guard=unit_cost,
            strong_entry=0.0,
            exit_target=0.0,
            overheat=0.0,
            sample_count=0,
        )

    points = load_spread_points(db, symbol, direction, strategy.statistical_lookback_range)
    sample_count = len(points)
    if sample_count < strategy.statistical_min_samples:
        fallback = evaluate_signal(total_net_profit, annualized_return, strategy.min_net_profit, strategy.min_annualized_return)
        fallback.reason = f"统计样本不足 {sample_count}/{strategy.statistical_min_samples}，回退固定利润规则: {fallback.reason or '通过'}"
        return StatisticalSignal(fallback, 0.0, unit_cost, 0.0, 0.0, 0.0, sample_count)

    spreads = [point.spread for point in points]
    costs = [point.total_cost for point in points]
    avg = mean(spreads)
    std = pstdev(spreads) if sample_count > 1 else 0.0
    reachable_entry = max(_percentile(spreads, strategy.reachable_entry_percentile), avg + strategy.reachable_entry_zscore * std)
    cost_guard = _percentile(costs, strategy.cost_guard_percentile)
    strong_entry = max(_percentile(spreads, 0.90), avg + 1.5 * std)
    exit_target = _exit_target_with_profit_buffer(
        percentile_target=_percentile(spreads, strategy.exit_target_percentile),
        entry_spread=current_spread,
        unit_cost=cost_guard,
        unit_profit_buffer=strategy.auto_close_unit_profit_buffer,
    )
    overheat = _percentile(spreads, 0.99)
    unit_edge = current_spread - cost_guard

    if current_spread <= cost_guard:
        result = SignalResult("rejected", f"价差 {current_spread:.2f} 未覆盖成本保护线 {cost_guard:.2f}")
    elif current_spread < reachable_entry:
        result = SignalResult("candidate", f"价差 {current_spread:.2f} 未达到可达入场线 {reachable_entry:.2f}")
    elif overheat > 0 and current_spread > overheat:
        result = SignalResult("candidate", f"价差 {current_spread:.2f} 超过过热线 {overheat:.2f}，等待确认")
    elif unit_edge < strategy.min_unit_edge:
        result = SignalResult("candidate", f"每份边际 {unit_edge:.2f} 低于最小边际 {strategy.min_unit_edge:.2f}")
    elif total_net_profit < strategy.min_total_profit:
        result = SignalResult("candidate", f"总净利润 {total_net_profit:.2f} 低于最小总利润 {strategy.min_total_profit:.2f}")
    else:
        result = SignalResult("executable", f"达到可达入场线 {reachable_entry:.2f}，成本保护线 {cost_guard:.2f}")
    return StatisticalSignal(result, reachable_entry, cost_guard, strong_entry, exit_target, overheat, sample_count)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pct = min(max(percentile, 0.0), 1.0)
    index = pct * (len(ordered) - 1)
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _exit_target_with_profit_buffer(
    *,
    percentile_target: float,
    entry_spread: float,
    unit_cost: float,
    unit_profit_buffer: float,
) -> float:
    profit_safe_target = entry_spread - max(unit_cost, 0.0) - max(unit_profit_buffer, 0.0)
    if percentile_target <= 0 or profit_safe_target <= 0:
        return 0.0
    return min(percentile_target, profit_safe_target)
