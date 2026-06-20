import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.db.models import ArbitrageOpportunity, HedgeGroup, StrategySetting, SystemLog, WorkerRun
from app.db.retention import prune_table_by_id
from app.execution.engine import open_hedge_group


@dataclass
class OpportunityConfirmation:
    first_seen: float
    last_seen: float
    ticks: int


_confirmations: dict[tuple[int, str, str], OpportunityConfirmation] = {}
_cooldown_until: dict[tuple[str, str], float] = {}


OPEN_GROUP_STATUSES = ("opening", "open", "open_partial", "manual_intervention")


def run_auto_execute(db: Session) -> int:
    started = time.perf_counter()
    executed = 0
    strategy = db.query(StrategySetting).first() or StrategySetting()
    if not strategy.auto_execute_enabled:
        return 0
    if strategy.auto_execute_paper_only and strategy.execution_mode != "paper":
        _record_skip(db, "auto_execute 要求 paper 模式，当前执行模式不是 paper")
        return 0

    opportunities = (
        db.query(ArbitrageOpportunity)
        .filter(ArbitrageOpportunity.status == "executable")
        .order_by(desc(ArbitrageOpportunity.net_profit))
        .limit(20)
        .all()
    )
    for opportunity in opportunities:
        allowed, reason = _eligible(db, strategy, opportunity)
        if not allowed:
            opportunity.reject_reason = reason
            continue
        confirmed, reason = _confirm(strategy, opportunity)
        if not confirmed:
            opportunity.reject_reason = reason
            continue
        decision_delay_ms = _decision_delay_ms(strategy)
        if decision_delay_ms > 0:
            time.sleep(decision_delay_ms / 1000)
        try:
            opportunity.status = "executing"
            opportunity.reject_reason = "auto_execute executing"
            db.commit()
            group = open_hedge_group(db, opportunity.id, source="auto_paper")
            _set_cooldown(strategy, opportunity.symbol, opportunity.direction)
            _confirmations.pop(_confirmation_key(opportunity), None)
            db.add(SystemLog(level="info", category="auto_execute", message=f"自动纸面执行成功: {opportunity.symbol} #{group.id}"))
            prune_table_by_id(db, SystemLog)
            db.commit()
            executed += 1
        except Exception as exc:
            db.rollback()
            row = db.get(ArbitrageOpportunity, opportunity.id)
            if row:
                row.status = "executable"
                row.reject_reason = f"自动执行失败: {exc}"
            _set_cooldown(strategy, opportunity.symbol, opportunity.direction)
            db.add(SystemLog(level="warning", category="auto_execute", message=f"自动执行失败: {opportunity.symbol}", context=str(exc)))
            db.add(WorkerRun(worker_name="auto_executor", status="failed", duration_ms=int((time.perf_counter() - started) * 1000), error_message=str(exc)))
            prune_table_by_id(db, SystemLog)
            prune_table_by_id(db, WorkerRun)
            db.commit()
    return executed


def _eligible(db: Session, strategy: StrategySetting, opportunity: ArbitrageOpportunity) -> tuple[bool, str]:
    min_profit = strategy.auto_execute_min_net_profit or strategy.min_net_profit
    if opportunity.net_profit < min_profit:
        return False, f"自动执行净利润不足: {opportunity.net_profit:.2f} < {min_profit:.2f}"
    cooldown_key = (opportunity.symbol, opportunity.direction)
    remaining = _cooldown_until.get(cooldown_key, 0.0) - time.time()
    if remaining > 0:
        return False, f"自动执行冷却中: {remaining:.1f}s"
    symbol_open_count = (
        db.query(HedgeGroup)
        .filter(HedgeGroup.symbol == opportunity.symbol, HedgeGroup.status.in_(OPEN_GROUP_STATUSES))
        .count()
    )
    if symbol_open_count >= strategy.auto_execute_max_per_symbol_open_groups:
        return False, f"同品种未平对冲组已达上限: {symbol_open_count}"
    global_open_count = db.query(HedgeGroup).filter(HedgeGroup.status.in_(OPEN_GROUP_STATUSES)).count()
    if global_open_count >= strategy.auto_execute_max_global_open_groups:
        return False, f"全局未平对冲组已达上限: {global_open_count}"
    return True, ""


def _confirm(strategy: StrategySetting, opportunity: ArbitrageOpportunity) -> tuple[bool, str]:
    now = time.time()
    key = _confirmation_key(opportunity)
    current = _confirmations.get(key)
    if not current:
        current = OpportunityConfirmation(first_seen=now, last_seen=now, ticks=0)
        _confirmations[key] = current
    current.ticks += 1
    current.last_seen = now
    hold_ms = (now - current.first_seen) * 1000
    required_ticks = max(strategy.auto_execute_confirm_ticks, 1)
    required_hold = max(strategy.auto_execute_min_hold_ms, 0)
    if current.ticks < required_ticks:
        return False, f"自动执行确认次数不足: {current.ticks}/{required_ticks}"
    if hold_ms < required_hold:
        return False, f"自动执行持续时间不足: {hold_ms:.0f}/{required_hold}ms"
    return True, ""


def _confirmation_key(opportunity: ArbitrageOpportunity) -> tuple[int, str, str]:
    return (opportunity.id, opportunity.symbol, opportunity.direction)


def _set_cooldown(strategy: StrategySetting, symbol: str, direction: str) -> None:
    _cooldown_until[(symbol, direction)] = time.time() + max(strategy.auto_execute_cooldown_seconds, 0)


def _decision_delay_ms(strategy: StrategySetting) -> int:
    low = max(int(strategy.paper_decision_delay_ms_min), 0)
    high = max(int(strategy.paper_decision_delay_ms_max), low)
    return random.randint(low, high)


def _record_skip(db: Session, message: str) -> None:
    db.add(SystemLog(level="warning", category="auto_execute", message=message))
    prune_table_by_id(db, SystemLog)
    db.commit()
