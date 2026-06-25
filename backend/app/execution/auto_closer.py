import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.analytics.spreads import load_spread_points
from app.config.settings import get_settings
from app.db.models import HedgeGroup, StrategySetting, SymbolMapping, SystemLog, SystemSetting, WorkerRun
from app.db.retention import prune_table_by_id
from app.execution.engine import close_hedge_group, paper_close_hedge_group
from app.execution.pnl import pnl_from_close_spread
from app.market.active_refresh import refresh_execution_quotes
from app.market.quotes import quote_synchronizer
from app.strategy.statistical_signal import _exit_target_with_profit_buffer, _percentile
from app.strategy.spread_math import spreads_for_direction


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

    modes = ["paper"]
    if strategy.auto_close_live_enabled:
        modes.append("live")
    groups = db.query(HedgeGroup).filter(HedgeGroup.status.in_(AUTO_CLOSE_STATUSES), HedgeGroup.execution_mode.in_(modes)).order_by(HedgeGroup.opened_at).limit(50).all()
    for group in groups:
        try:
            if group.execution_mode == "live" and not _live_auto_close_allowed(db):
                db.add(SystemLog(level="warning", category="auto_close", message=f"跳过 live 自动平仓: {group.symbol} #{group.id}", context="live_trading_enabled 未开启"))
                prune_table_by_id(db, SystemLog)
                db.commit()
                continue
            evaluation = evaluate_auto_close(db, strategy, group)
            group.unrealized_pnl = evaluation.estimated_profit
            if not evaluation.should_close:
                continue
            if group.execution_mode == "live":
                close_hedge_group(db, group.id, f"auto_live: {evaluation.reason}", validate_final_close=True)
                db.add(SystemLog(level="info", category="auto_close", message=f"自动实盘平仓已提交: {group.symbol} #{group.id}", context=evaluation.reason))
            else:
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
        mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
        refreshed = refresh_execution_quotes(mapping) if mapping else []
        if refreshed:
            synced, sync_reason = quote_synchronizer.synchronized(
                group.symbol,
                mode="strict",
                max_time_diff_ms=settings.strict_quote_sync_ms,
                max_age_ms=settings.quote_stale_ms,
            )
        if not synced:
            suffix = f"；执行前主动刷新: {','.join(refreshed)}" if refreshed else ""
            return CloseEvaluation(False, f"{sync_reason}{suffix}", 0.0, group.exit_target or 0.0, group.unrealized_pnl)

    close_spread = spreads_for_direction(group.direction, synced.hyperliquid.bid, synced.hyperliquid.ask, synced.mt5.bid, synced.mt5.ask).close_spread
    exit_target = group.exit_target or _fallback_exit_target(db, strategy, group)
    estimated_profit = pnl_from_close_spread(group, close_spread)
    min_profit = strategy.auto_close_min_profit
    hold_expired = _hold_expired(group, strategy)

    if estimated_profit < min_profit:
        return CloseEvaluation(False, f"估算平仓利润不足: {estimated_profit:.2f} < {min_profit:.2f}", close_spread, exit_target, estimated_profit)
    if exit_target <= 0:
        if close_spread <= 0:
            return CloseEvaluation(True, f"无统计退出线但平仓价差已回到零轴: {close_spread:.2f} <= 0.00", close_spread, exit_target, estimated_profit)
        if hold_expired:
            return CloseEvaluation(True, f"缺少退出线但超过最大持仓时间且利润达标: {estimated_profit:.2f}", close_spread, exit_target, estimated_profit)
        return CloseEvaluation(False, "缺少退出线，等待更多统计样本", close_spread, exit_target, estimated_profit)
    if close_spread <= exit_target:
        return CloseEvaluation(True, f"平仓价差回归至退出线: {close_spread:.2f} <= {exit_target:.2f}", close_spread, exit_target, estimated_profit)
    if hold_expired:
        return CloseEvaluation(True, f"超过最大持仓时间且利润达标: {estimated_profit:.2f}", close_spread, exit_target, estimated_profit)
    return CloseEvaluation(False, f"等待平仓价差回归: {close_spread:.2f} > {exit_target:.2f}", close_spread, exit_target, estimated_profit)


def _fallback_exit_target(db: Session, strategy: StrategySetting, group: HedgeGroup) -> float:
    points = load_spread_points(db, group.symbol, group.direction, strategy.statistical_lookback_range, basis="close")
    if len(points) < strategy.statistical_min_samples:
        return _mapping_max_close_spread(db, group.symbol)
    spreads = [point.spread for point in points]
    quantity = group.hyperliquid_quantity or group.quantity or 1.0
    entry_spread = group.entry_spread or group.entry_threshold
    unit_open_cost = group.open_cost / max(quantity, 1e-12)
    statistical_target = _exit_target_with_profit_buffer(
        percentile_target=_percentile(spreads, strategy.exit_target_percentile),
        entry_spread=entry_spread,
        unit_cost=unit_open_cost,
        unit_profit_buffer=strategy.auto_close_unit_profit_buffer,
    )
    max_close_spread = _mapping_max_close_spread(db, group.symbol)
    if max_close_spread <= 0:
        return statistical_target
    if statistical_target <= 0:
        return max_close_spread
    return min(statistical_target, max_close_spread)


def _mapping_max_close_spread(db: Session, symbol: str) -> float:
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == symbol).first()
    return float(getattr(mapping, "max_close_spread", 0.0) or 0.0) if mapping else 0.0


def _hold_expired(group: HedgeGroup, strategy: StrategySetting) -> bool:
    if not group.opened_at:
        return False
    return datetime.now(timezone.utc).replace(tzinfo=None) - group.opened_at >= timedelta(minutes=max(strategy.max_holding_minutes, 1))


def _live_auto_close_allowed(db: Session) -> bool:
    row = db.query(SystemSetting).filter(SystemSetting.key == "live_trading_enabled").first()
    return bool(row and row.value == "true")
