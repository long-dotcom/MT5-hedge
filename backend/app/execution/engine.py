import random
from datetime import datetime

from sqlalchemy.orm import Session

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.mt5 import MT5Adapter
from app.config.settings import get_settings
from app.db.models import Alert, ArbitrageOpportunity, Fill, HedgeGroup, HedgeGroupEvent, Order, StrategySetting, SymbolMapping, SystemSetting
from app.execution.gateway import LegOrderIntent, build_execution_gateway
from app.execution.readiness import live_execution_readiness
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
    if live:
        _ensure_live_execution_ready(db)
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
        for platform, adapter, side, order_type, order_quantity, venue_symbol in [
            ("hyperliquid", hl, hl_side, mapping.hl_open_order_type, hl_quantity, mapping.hyperliquid_symbol),
            ("mt5", mt5, mt5_side, mapping.mt5_open_order_type, mt5_quantity, mapping.mt5_symbol),
        ]:
            result = _place_and_record(db, group.id, platform, adapter, opportunity.symbol, venue_symbol, side, order_quantity, order_type, None, False, 0, strategy)
            results.append(result)

    if all(_has_position_effect(result) for result in results):
        group.status = "open"
        group.opened_at = datetime.utcnow()
        group.fees = sum(result.fee for result in results)
        opportunity.status = "executed"
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="opened", detail="双边订单成交"))
    elif any(_has_position_effect(result) for result in results):
        group.status = "manual_intervention"
        db.add(Alert(level="critical", title="单边成交异常", message=f"{opportunity.symbol} 对冲组需要人工处理"))
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="manual_intervention", detail="一边成交一边失败"))
    elif any(_is_pending_result(result) for result in results):
        group.status = "opening"
        opportunity.status = "executing"
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="orders_pending", detail="订单已提交，等待成交回报"))
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
        mapping.hyperliquid_symbol,
        hl_side,
        hl_quantity,
        "limit",
        hl_price,
        True,
        mapping.hl_order_ttl_seconds,
        strategy,
    )
    if not _has_position_effect(hl_result):
        event_type = "maker_pending" if _is_pending_result(hl_result) else "maker_unfilled"
        db.add(HedgeGroupEvent(hedge_group_id=group_id, event_type=event_type, detail=hl_result.error_message or "Hyperliquid maker 未成交"))
        return [hl_result]
    fill_ratio = hl_result.filled_quantity / hl_quantity if hl_quantity > 0 else 0.0
    mt5_result = _place_and_record(db, group_id, "mt5", mt5, symbol, mapping.mt5_symbol, mt5_side, mt5_quantity * fill_ratio, "market", None, False, 0, strategy)
    return [hl_result, mt5_result]


def _maker_price(side: str, bid: float, ask: float, offset_bps: float) -> float:
    if side == "buy":
        return bid * (1 - offset_bps / 10_000)
    return ask * (1 + offset_bps / 10_000)


def _place_and_record(
    db: Session,
    group_id: int,
    platform: str,
    adapter,
    symbol: str,
    venue_symbol: str,
    side: str,
    quantity: float,
    order_type: str,
    price: float | None,
    post_only: bool,
    ttl_seconds: int,
    strategy: StrategySetting,
    reduce_only: bool = False,
):
    order = Order(
        hedge_group_id=group_id,
        platform=platform,
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type,
        price=price,
        post_only=post_only,
        reduce_only=reduce_only,
        ttl_seconds=ttl_seconds,
        status="new",
    )
    db.add(order)
    db.flush()
    gateway = build_execution_gateway(adapter)
    gateway_result = gateway.submit_order(
        LegOrderIntent(
            platform=platform,
            symbol=symbol,
            side=side,
            quantity=quantity,
            venue_symbol=venue_symbol,
            price=price,
            order_type=order_type,
            post_only=post_only,
            reduce_only=reduce_only,
            ttl_seconds=ttl_seconds,
            hedge_group_id=group_id,
        ),
        paper_latency_ms=_paper_latency_ms(strategy, platform, adapter),
    )
    result = gateway_result.adapter_result
    order.status = result.status
    order.external_order_id = result.external_order_id
    order.price = result.average_price or price
    order.error_message = result.error_message
    for fill_event in gateway_result.fill_events:
        db.add(
            Fill(
                order_id=order.id,
                platform=fill_event.platform,
                symbol=fill_event.symbol,
                side=fill_event.side,
                quantity=fill_event.quantity,
                price=fill_event.price,
                fee=fill_event.fee,
            )
        )
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
    if group.execution_mode == "paper":
        return _execute_close_hedge_group(db, group, reason, live=False, estimated_realized_pnl=None, success_event_type="closed", pending_event_type="close_pending", failed_event_type="close_failed")
    if group.execution_mode == "live":
        if not live_trading_enabled(db):
            raise ValueError("实盘平仓需要先开启 live_trading_enabled")
        _ensure_live_execution_ready(db)
        return _execute_close_hedge_group(db, group, reason, live=True, estimated_realized_pnl=None, success_event_type="closed", pending_event_type="close_pending", failed_event_type="close_failed")

    group.status = "closed"
    group.closed_at = datetime.utcnow()
    group.realized_pnl = group.unrealized_pnl - group.fees - group.funding - group.swap
    group.close_reason = reason
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
    return _execute_close_hedge_group(
        db,
        group,
        reason,
        live=False,
        estimated_realized_pnl=estimated_realized_pnl,
        success_event_type="auto_closed",
        pending_event_type="auto_close_pending",
        failed_event_type="auto_close_failed",
    )


def _execute_close_hedge_group(
    db: Session,
    group: HedgeGroup,
    reason: str,
    *,
    live: bool,
    estimated_realized_pnl: float | None,
    success_event_type: str,
    pending_event_type: str,
    failed_event_type: str,
) -> HedgeGroup:
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
    if not mapping:
        raise ValueError("品种映射不存在")
    session_state = mt5_session_state(mapping)
    mt5_close_allowed, mt5_close_reason = mt5_action_allowed(session_state, group.direction, "close")
    if not mt5_close_allowed:
        raise ValueError(mt5_close_reason)

    strategy = db.query(StrategySetting).first() or StrategySetting()
    hl_side, mt5_side = _close_sides(group.direction)
    hl = HyperliquidAdapter(live=live)
    mt5 = MT5Adapter(live=live)
    legs = [
        ("hyperliquid", hl, hl_side, mapping.hl_close_order_type, _platform_close_quantity(group.hyperliquid_quantity, group.quantity), mapping.hyperliquid_symbol),
        ("mt5", mt5, mt5_side, mapping.mt5_close_order_type, _platform_close_quantity(group.mt5_quantity, group.quantity), mapping.mt5_symbol),
    ]
    results = []
    for platform, adapter, side, order_type, order_quantity, venue_symbol in legs:
        if order_quantity <= 0:
            continue
        result = _place_and_record(db, group.id, platform, adapter, group.symbol, venue_symbol, side, order_quantity, order_type, None, False, 0, strategy, reduce_only=True)
        results.append(result)
    if not results:
        raise ValueError("对冲组没有可平仓数量")

    if all(_has_position_effect(result) for result in results):
        group.status = "closed"
        group.closed_at = datetime.utcnow()
        group.fees += sum(result.fee for result in results)
        if estimated_realized_pnl is not None:
            group.realized_pnl = estimated_realized_pnl
        else:
            group.realized_pnl = group.unrealized_pnl - group.fees - group.funding - group.swap
        group.unrealized_pnl = 0.0
        group.close_reason = reason
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type=success_event_type, detail=reason))
    elif any(_has_position_effect(result) for result in results):
        group.status = "manual_intervention"
        group.close_reason = f"平仓单边成交: {reason}"
        db.add(Alert(level="critical", title="平仓单边成交", message=f"{group.symbol} 对冲组 #{group.id} 需要人工处理"))
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="manual_intervention", detail=group.close_reason))
    elif any(_is_pending_result(result) for result in results):
        group.status = "closing"
        group.close_reason = f"平仓订单待成交: {reason}"
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type=pending_event_type, detail=group.close_reason))
    else:
        group.close_reason = f"平仓失败: {reason}"
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type=failed_event_type, detail=group.close_reason))
    db.commit()
    db.refresh(group)
    return group


def _close_sides(direction: str) -> tuple[str, str]:
    if direction == "long_hyperliquid_short_mt5":
        return "sell", "buy"
    return "buy", "sell"


def _platform_close_quantity(platform_quantity: float | None, fallback_quantity: float) -> float:
    if platform_quantity is None:
        return fallback_quantity
    return platform_quantity


def _ensure_live_execution_ready(db: Session) -> None:
    readiness = live_execution_readiness(db)
    blocked = [item for item in readiness.get("checks", []) if item.get("status") == "block"]
    if blocked:
        detail = "; ".join(str(item.get("message") or item.get("component")) for item in blocked)
        raise ValueError(f"实盘执行就绪检查未通过: {detail}")


def _has_position_effect(result) -> bool:
    return bool(result.success and result.filled_quantity > 0 and result.status in {"filled", "partially_filled"})


def _is_pending_result(result) -> bool:
    return result.status in {"accepted", "submitted", "pending", "open", "new"}
