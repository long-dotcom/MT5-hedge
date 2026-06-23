from dataclasses import dataclass
from statistics import mean, pstdev
from time import monotonic

from sqlalchemy.orm import Session

from app.analytics.spreads import SpreadPoint, load_spread_points
from app.db.models import StrategySetting, SymbolMapping
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


@dataclass(frozen=True)
class SignalStats:
    sample_count: int
    reachable_entry: float
    cost_guard: float
    strong_entry: float
    exit_percentile_target: float
    overheat: float


_stats_cache: dict[tuple, tuple[float, SignalStats]] = {}


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

    stats = _signal_stats(db, strategy, symbol, direction)
    if stats.sample_count < strategy.statistical_min_samples:
        result = SignalResult("candidate", f"统计样本不足 {stats.sample_count}/{strategy.statistical_min_samples}，等待参考数据")
        return StatisticalSignal(result, 0.0, unit_cost, 0.0, 0.0, 0.0, stats.sample_count)

    exit_target = _exit_target_with_profit_buffer(
        percentile_target=stats.exit_percentile_target,
        entry_spread=current_spread,
        unit_cost=stats.cost_guard,
        unit_profit_buffer=_strategy_float(strategy, "auto_close_unit_profit_buffer", 0.0),
    )
    unit_edge = current_spread - stats.cost_guard

    if current_spread <= stats.cost_guard:
        result = SignalResult("rejected", f"价差 {current_spread:.2f} 未覆盖成本保护线 {stats.cost_guard:.2f}")
    elif current_spread < stats.reachable_entry:
        result = SignalResult("candidate", f"价差 {current_spread:.2f} 未达到可达入场线 {stats.reachable_entry:.2f}")
    elif stats.overheat > 0 and current_spread > stats.overheat:
        result = SignalResult("candidate", f"价差 {current_spread:.2f} 超过过热线 {stats.overheat:.2f}，等待确认")
    elif unit_edge < _strategy_float(strategy, "min_unit_edge", 0.0):
        result = SignalResult("candidate", f"每份边际 {unit_edge:.2f} 低于最小边际 {_strategy_float(strategy, 'min_unit_edge', 0.0):.2f}")
    elif total_net_profit < _strategy_float(strategy, "min_total_profit", 0.0):
        result = SignalResult("candidate", f"总净利润 {total_net_profit:.2f} 低于最小总利润 {_strategy_float(strategy, 'min_total_profit', 0.0):.2f}")
    else:
        result = SignalResult("executable", f"达到可达入场线 {stats.reachable_entry:.2f}，成本保护线 {stats.cost_guard:.2f}")
    return StatisticalSignal(result, stats.reachable_entry, stats.cost_guard, stats.strong_entry, exit_target, stats.overheat, stats.sample_count)


def clear_signal_stats_cache() -> None:
    _stats_cache.clear()


def refresh_signal_stats_cache(db: Session) -> int:
    strategy = db.query(StrategySetting).first() or StrategySetting()
    if strategy.signal_mode != "statistical":
        return 0
    refreshed = 0
    symbols = [row.symbol for row in db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True)).all()]
    now = monotonic()
    for symbol in symbols:
        for direction in ("long_hyperliquid_short_mt5", "long_mt5_short_hyperliquid"):
            points = load_spread_points(db, symbol, direction, strategy.statistical_lookback_range)
            stats = _compute_signal_stats(points, strategy)
            _stats_cache[_stats_cache_key(db, strategy, symbol, direction)] = (now, stats)
            refreshed += 1
    return refreshed


def _signal_stats(db: Session, strategy: StrategySetting, symbol: str, direction: str) -> SignalStats:
    key = _stats_cache_key(db, strategy, symbol, direction)
    cached = _stats_cache.get(key)
    if cached:
        return cached[1]
    points = load_spread_points(db, symbol, direction, strategy.statistical_lookback_range)
    stats = _compute_signal_stats(points, strategy)
    _stats_cache[key] = (monotonic(), stats)
    return stats


def _stats_cache_key(db: Session, strategy: StrategySetting, symbol: str, direction: str) -> tuple:
    return (
        id(db.get_bind()),
        symbol.upper(),
        direction,
        strategy.statistical_lookback_range,
        strategy.statistical_min_samples,
        _strategy_float(strategy, "reachable_entry_percentile", 0.75),
        _strategy_float(strategy, "reachable_entry_zscore", 1.0),
        _strategy_float(strategy, "cost_guard_percentile", 0.90),
        _strategy_float(strategy, "exit_target_percentile", 0.25),
    )


def _compute_signal_stats(points: list[SpreadPoint], strategy: StrategySetting) -> SignalStats:
    sample_count = len(points)
    if not points:
        return SignalStats(0, 0.0, 0.0, 0.0, 0.0, 0.0)
    spreads = [point.spread for point in points]
    costs = [point.total_cost for point in points]
    avg = mean(spreads)
    std = pstdev(spreads) if sample_count > 1 else 0.0
    reachable_entry = max(_percentile(spreads, _strategy_float(strategy, "reachable_entry_percentile", 0.75)), avg + _strategy_float(strategy, "reachable_entry_zscore", 1.0) * std)
    cost_guard = _percentile(costs, _strategy_float(strategy, "cost_guard_percentile", 0.90))
    strong_entry = max(_percentile(spreads, 0.90), avg + 1.5 * std)
    exit_percentile_target = _percentile(spreads, _strategy_float(strategy, "exit_target_percentile", 0.25))
    overheat = _percentile(spreads, 0.99)
    return SignalStats(sample_count, reachable_entry, cost_guard, strong_entry, exit_percentile_target, overheat)


def _strategy_float(strategy: StrategySetting, name: str, default: float) -> float:
    value = getattr(strategy, name, None)
    return default if value is None else float(value)


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
