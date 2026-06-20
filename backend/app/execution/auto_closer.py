import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.analytics.spreads import load_spread_points
from app.config.settings import get_settings
from app.db.models import HedgeGroup, StrategySetting, SystemLog, WorkerRun
from app.db.retention import prune_table_by_id
from app.execution.engine import paper_close_hedge_group
from app.market.quotes import quote_synchronizer
from app.strategy.statistical_signal import _percentile


@dataclass(frozen=True)
class CloseEvaluation:
    should_close: bool
    reason: str
    close_spread: float
    exit_target: float
    estimated_profit: float


AUTO_CLOSE_STATUSES = ("open", "open_partial")


def run_auto_close(db: Session) -> int:
    started = time.perf_counter()
    closed = 0
    strategy = db.query(StrategySetting).first() or StrategySetting()
    if not strategy.auto_close_enabled:
        return 0

    groups = (
        db.query(HedgeGroup)
        .filter(HedgeGroup.status.in_(AUTO_CLOSE_STATUSES), HedgeGroup.execution_mode == "paper")
        .order_by(HedgeGroup.opened_at)
        .limit(50)
        .all()
    )
    for group in groups:
        try:
            evaluation = evaluate_auto_close(db, strategy, group)
            group.unrealized_pnl = evaluation.estimated_profit
            if not evaluation.should_close:
                continue
            paper_close_hedge_group(db, group.id, evaluation.reason, evaluation.estimated_profit)
            db.add(SystemLog(level="info", category="auto_close", message=f"自动纸面平仓成功: {group.symbol} #{group.id}", context=evaluation.reason))
            prune_table_by_id(db, SystemLog)
            db.commit()
            closed += 1
        except Exception as exc:
            db.rollback()
            db.add(SystemLog(level="warning", category="auto_close", message=f"自动平仓检查失败: {group.symbol} #{group.id}", context=str(exc)))
            db.add(WorkerRun(worker_name="auto_closer", status="failed", duration_ms=int((time.perf_counter() - started) * 1000), error_message=str(exc)))
            prune_table_by_id(db, SystemLog)
            prune_table_by_id(db, WorkerRun)
            db.commit()
    db.commit()
    return closed


def evaluate_auto_close(db: Session, strategy: StrategySetting, group: HedgeGroup) -> CloseEvaluation:
    settings = get_settings()
    synced, sync_reason = quote_synchronizer.synchronized(
        group.symbol,
        mode="strict",
        max_time_diff_ms=settings.strict_quote_sync_ms,
        max_age_ms=settings.quote_stale_ms,
    )
    if not synced:
        return CloseEvaluation(False, sync_reason, 0.0, group.exit_target or 0.0, group.unrealized_pnl)

    close_spread = _close_spread(group.direction, synced.hyperliquid.bid, synced.hyperliquid.ask, synced.mt5.bid, synced.mt5.ask)
    exit_target = group.exit_target or _fallback_exit_target(db, strategy, group)
    entry_spread = group.entry_spread or group.entry_threshold
    quantity = group.hyperliquid_quantity or group.quantity or 1.0
    estimated_profit = (entry_spread - close_spread) * quantity - group.open_cost
    min_profit = strategy.auto_close_min_profit
    hold_expired = _hold_expired(group, strategy)

    if exit_target <= 0:
        return CloseEvaluation(False, "缺少退出线，等待更多统计样本", close_spread, exit_target, estimated_profit)
    if estimated_profit < min_profit:
        return CloseEvaluation(False, f"估算平仓利润不足: {estimated_profit:.2f} < {min_profit:.2f}", close_spread, exit_target, estimated_profit)
    if close_spread <= exit_target:
        return CloseEvaluation(True, f"价差回归至退出线: {close_spread:.2f} <= {exit_target:.2f}", close_spread, exit_target, estimated_profit)
    if hold_expired:
        return CloseEvaluation(True, f"超过最大持仓时间且利润达标: {estimated_profit:.2f}", close_spread, exit_target, estimated_profit)
    return CloseEvaluation(False, f"等待价差回归: {close_spread:.2f} > {exit_target:.2f}", close_spread, exit_target, estimated_profit)


def _close_spread(direction: str, hl_bid: float, hl_ask: float, mt5_bid: float, mt5_ask: float) -> float:
    if direction == "long_hyperliquid_short_mt5":
        return mt5_ask - hl_bid
    return hl_ask - mt5_bid


def _fallback_exit_target(db: Session, strategy: StrategySetting, group: HedgeGroup) -> float:
    points = load_spread_points(db, group.symbol, group.direction, strategy.statistical_lookback_range)
    if len(points) < strategy.statistical_min_samples:
        return 0.0
    spreads = [point.spread for point in points]
    costs = [point.total_cost for point in points]
    cost_guard = _percentile(costs, strategy.cost_guard_percentile)
    return max(_percentile(spreads, strategy.exit_target_percentile), cost_guard + max(strategy.auto_close_unit_profit_buffer, 0.0))


def _hold_expired(group: HedgeGroup, strategy: StrategySetting) -> bool:
    if not group.opened_at:
        return False
    return datetime.utcnow() - group.opened_at >= timedelta(minutes=max(strategy.max_holding_minutes, 1))
