import random
from datetime import datetime

from sqlalchemy.orm import Session

from app.adapters.base import AdapterOrder
from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.mt5 import MT5Adapter
from app.config.settings import get_settings
from app.db.models import Alert, ArbitrageOpportunity, Fill, HedgeGroup, HedgeGroupEvent, Order, StrategySetting, SymbolMapping, SystemSetting
from app.market.mt5_sessions import mt5_action_allowed, mt5_session_state
from app.market.quotes import quote_synchronizer
from app.risk.engine import pre_trade_check, record_risk_event


def live_trading_enabled(db: Session) -> bool:
    row = db.query(SystemSetting).filter(SystemSetting.key == "live_trading_enabled").first()
    return bool(row and row.value == "true")


def open_hedge_group(db: Session, opportunity_id: int, source: str = "system") -> HedgeGroup:
    opportunity = db.get(ArbitrageOpportunity, opportunity_id)
    if not opportunity:
        raise ValueError("机会不存在")
    if opportunity.status not in {"executable", "executing"}:
        raise ValueError("只有 executable 状态的机会允许执行")
    strategy = db.query(StrategySetting).first() or StrategySetting()
    settings = get_settings()
    mode = strategy.execution_mode
    live = mode == "live" and live_trading_enabled(db)
    synced, sync_reason = quote_synchronizer.synchronized(
        opportunity.symbol,
        mode="strict",
        max_time_diff_ms=settings.strict_quote_sync_ms,
        max_age_ms=settings.quote_stale_ms,
    )
    if not synced:
        record_risk_event(db, "strict_quote_sync", sync_reason, opportunity.symbol)
        raise ValueError(sync_reason)
    use_live_account_risk = live or (mode == "paper" and strategy.paper_use_live_account_risk)
    decision = pre_trade_check(db, opportunity.symbol, opportunity.notional, synced.time_diff_ms / 10, synced.hyperliquid.local_recv_ts, use_live_account_risk=use_live_account_risk)
    if not decision.allowed:
        record_risk_event(db, "pre_trade", decision.reason, opportunity.symbol)
        raise ValueError(decision.reason)

    group = HedgeGroup(
        symbol=opportunity.symbol,
        direction=opportunity.direction,
        status="opening",
        execution_mode="live" if live else mode,
        notional=opportunity.notional,
        quantity=opportunity.quantity,
        mt5_quantity=opportunity.mt5_quantity or opportunity.quantity,
        hyperliquid_quantity=opportunity.hyperliquid_quantity or opportunity.quantity,
        open_cost=opportunity.total_cost,
        entry_spread=opportunity.gross_spread,
        entry_threshold=opportunity.entry_threshold,
        exit_target=opportunity.exit_target,
        overheat_threshold=opportunity.overheat_threshold,
        source=source,
    )
    db.add(group)
    db.flush()

    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == opportunity.symbol).first()
    if not mapping:
        raise ValueError("品种映射不存在")
    hl_side = "buy" if opportunity.direction == "long_hyperliquid_short_mt5" else "sell"
    mt5_side = "sell" if opportunity.direction == "long_hyperliquid_short_mt5" else "buy"
    hl = HyperliquidAdapter(live=live)
    mt5 = MT5Adapter(live=live)
    hl_quantity = opportunity.hyperliquid_quantity or opportunity.quantity
    mt5_quantity = opportunity.mt5_quantity or opportunity.quantity
    if mapping.execution_style == "hyper_maker_mt5_taker":
        results = _execute_hyper_maker_then_mt5(db, group.id, mapping, opportunity.symbol, hl, mt5, hl_side, mt5_side, hl_quantity, mt5_quantity, synced)
    else:
        results = []
        for platform, adapter, side, order_type, order_quantity in [
            ("hyperliquid", hl, hl_side, mapping.hl_open_order_type, hl_quantity),
            ("mt5", mt5, mt5_side, mapping.mt5_open_order_type, mt5_quantity),
        ]:
            result = _place_and_record(db, group.id, platform, adapter, opportunity.symbol, side, order_quantity, order_type, None, False, 0, strategy)
            results.append(result)

    if all(result.success for result in results):
        group.status = "open"
        group.opened_at = datetime.utcnow()
        group.fees = sum(result.fee for result in results)
        opportunity.status = "executed"
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="opened", detail="双边订单成交"))
    elif any(result.success for result in results):
        group.status = "manual_intervention"
        db.add(Alert(level="critical", title="单边成交异常", message=f"{opportunity.symbol} 对冲组需要人工处理"))
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="manual_intervention", detail="一边成交一边失败"))
    else:
        group.status = "failed"
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="failed", detail="双边下单均失败"))
    db.commit()
    db.refresh(group)
    return group


def _execute_hyper_maker_then_mt5(db: Session, group_id: int, mapping: SymbolMapping, symbol: str, hl, mt5, hl_side: str, mt5_side: str, hl_quantity: float, mt5_quantity: float, synced) -> list:
    strategy = db.query(StrategySetting).first() or StrategySetting()
    hl_price = _maker_price(hl_side, synced.hyperliquid.bid, synced.hyperliquid.ask, mapping.hl_maker_offset_bps)
    hl_result = _place_and_record(
        db,
        group_id,
        "hyperliquid",
        hl,
        symbol,
        hl_side,
        hl_quantity,
        "limit",
        hl_price,
        True,
        mapping.hl_order_ttl_seconds,
        strategy,
    )
    if not hl_result.success:
        db.add(HedgeGroupEvent(hedge_group_id=group_id, event_type="maker_unfilled", detail=hl_result.error_message or "Hyperliquid maker 未成交"))
        return [hl_result]
    fill_ratio = hl_result.filled_quantity / hl_quantity if hl_quantity > 0 else 0.0
    mt5_result = _place_and_record(db, group_id, "mt5", mt5, symbol, mt5_side, mt5_quantity * fill_ratio, "market", None, False, 0, strategy)
    return [hl_result, mt5_result]


def _maker_price(side: str, bid: float, ask: float, offset_bps: float) -> float:
    if side == "buy":
        return bid * (1 - offset_bps / 10_000)
    return ask * (1 + offset_bps / 10_000)


def _place_and_record(db: Session, group_id: int, platform: str, adapter, symbol: str, side: str, quantity: float, order_type: str, price: float | None, post_only: bool, ttl_seconds: int, strategy: StrategySetting):
    order = Order(
        hedge_group_id=group_id,
        platform=platform,
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type,
        price=price,
        post_only=post_only,
        ttl_seconds=ttl_seconds,
        status="new",
    )
    db.add(order)
    db.flush()
    result = adapter.place_order(
        AdapterOrder(
            platform=platform,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            order_type=order_type,
            post_only=post_only,
            ttl_seconds=ttl_seconds,
            paper_latency_ms=_paper_latency_ms(strategy, platform, adapter),
        )
    )
    order.status = result.status
    order.external_order_id = result.external_order_id
    order.price = result.average_price or price
    order.error_message = result.error_message
    if result.success:
        db.add(Fill(order_id=order.id, platform=platform, symbol=symbol, side=side, quantity=result.filled_quantity, price=result.average_price, fee=result.fee))
    return result


def _paper_latency_ms(strategy: StrategySetting, platform: str, adapter) -> int:
    if getattr(adapter, "live", False):
        return 0
    if platform == "hyperliquid":
        low = strategy.paper_hyperliquid_latency_ms_min
        high = strategy.paper_hyperliquid_latency_ms_max
    else:
        low = strategy.paper_mt5_latency_ms_min
        high = strategy.paper_mt5_latency_ms_max
    low = max(int(low), 0)
    high = max(int(high), low)
    return random.randint(low, high)


def close_hedge_group(db: Session, group_id: int, reason: str) -> HedgeGroup:
    group = db.get(HedgeGroup, group_id)
    if not group:
        raise ValueError("对冲组不存在")
    if group.status not in {"open", "open_partial", "manual_intervention"}:
        raise ValueError("当前状态不允许平仓")
    group.status = "closed"
    group.closed_at = datetime.utcnow()
    group.realized_pnl = group.unrealized_pnl - group.fees - group.funding - group.swap
    db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="closed", detail=reason))
    db.commit()
    db.refresh(group)
    return group


def paper_close_hedge_group(db: Session, group_id: int, reason: str, estimated_realized_pnl: float | None = None) -> HedgeGroup:
    group = db.get(HedgeGroup, group_id)
    if not group:
        raise ValueError("对冲组不存在")
    if group.execution_mode != "paper":
        raise ValueError("自动平仓首版仅支持 paper 对冲组")
    if group.status not in {"open", "open_partial"}:
        raise ValueError("当前状态不允许自动平仓")

    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
    if not mapping:
        raise ValueError("品种映射不存在")
    session_state = mt5_session_state(mapping)
    mt5_close_allowed, mt5_close_reason = mt5_action_allowed(session_state, group.direction, "close")
    if not mt5_close_allowed:
        raise ValueError(mt5_close_reason)

    strategy = db.query(StrategySetting).first() or StrategySetting()
    hl_side, mt5_side = _close_sides(group.direction)
    hl = HyperliquidAdapter(live=False)
    mt5 = MT5Adapter(live=False)
    results = []
    for platform, adapter, side, order_type, order_quantity in [
        ("hyperliquid", hl, hl_side, mapping.hl_close_order_type, group.hyperliquid_quantity or group.quantity),
        ("mt5", mt5, mt5_side, mapping.mt5_close_order_type, group.mt5_quantity or group.quantity),
    ]:
        result = _place_and_record(db, group.id, platform, adapter, group.symbol, side, order_quantity, order_type, None, False, 0, strategy)
        results.append(result)

    if all(result.success for result in results):
        group.status = "closed"
        group.closed_at = datetime.utcnow()
        group.fees += sum(result.fee for result in results)
        if estimated_realized_pnl is not None:
            group.realized_pnl = estimated_realized_pnl
        else:
            group.realized_pnl = group.unrealized_pnl - group.fees - group.funding - group.swap
        group.unrealized_pnl = 0.0
        group.close_reason = reason
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="auto_closed", detail=reason))
    elif any(result.success for result in results):
        group.status = "manual_intervention"
        group.close_reason = f"自动平仓单边成交: {reason}"
        db.add(Alert(level="critical", title="自动平仓单边成交", message=f"{group.symbol} 对冲组 #{group.id} 需要人工处理"))
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="manual_intervention", detail=group.close_reason))
    else:
        group.close_reason = f"自动平仓失败: {reason}"
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="auto_close_failed", detail=group.close_reason))
    db.commit()
    db.refresh(group)
    return group


def _close_sides(direction: str) -> tuple[str, str]:
    if direction == "long_hyperliquid_short_mt5":
        return "sell", "buy"
    return "buy", "sell"
