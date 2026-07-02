import math
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.mt5 import MT5Adapter
from app.adapters.mt5 import mt5_market_order_check
from app.adapters.venue import build_market_adapter, NATIVE_VENUES
from app.config.settings import get_settings
from app.db.models import Alert, ArbitrageOpportunity, Fill, HedgeGroup, HedgeGroupEvent, Order, StrategySetting, SymbolMapping, SystemSetting
from app.execution.gateway import LegOrderIntent, build_execution_gateway
from app.execution.hedge_pool import hedge_pool
from app.execution.pnl import actual_entry_spread_from_fills, pnl_from_close_spread, realized_pnl_from_fills
from app.execution.readiness import live_execution_readiness, paper_execution_readiness
from app.execution.runtime_settings import paper_live_probe_enabled_for_venue, runtime_paper_live_parallel_execution
from app.market.active_refresh import refresh_execution_quotes
from app.market.mt5_sessions import mt5_action_allowed, mt5_session_state
from app.market.mt5_tradability import block_mt5_tradability, mt5_tradability_cache
from app.market.quotes import quote_synchronizer
from app.risk.engine import pre_trade_check, record_risk_event
from app.strategy.spread_math import spreads_for_direction


def live_trading_enabled(db: Session) -> bool:
    row = db.query(SystemSetting).filter(SystemSetting.key == "live_trading_enabled").first()
    return bool(row and row.value == "true")


def open_hedge_group(db: Session, opportunity_id: int, source: str = "system", *, force_strategy_checks: bool = False) -> HedgeGroup:
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
    simulated = mode == "paper"
    if simulated:
        _ensure_paper_execution_ready(db)
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == opportunity.symbol).first()
    if not mapping:
        raise ValueError("品种映射不存在")
    leg_b_side = "sell" if opportunity.direction == "long_leg_a_short_leg_b" else "buy"
    leg_b_quantity = opportunity.leg_b_quantity or opportunity.quantity
    session_state = mt5_session_state(mapping)
    leg_b_open_allowed, leg_b_open_reason = mt5_action_allowed(session_state, opportunity.direction, "open")
    if not leg_b_open_allowed:
        opportunity.reject_reason = leg_b_open_reason
        db.add(opportunity)
        record_risk_event(db, "mt5_session_open", leg_b_open_reason, opportunity.symbol)
        db.commit()
        raise ValueError(leg_b_open_reason)
    if live or simulated:
        mt5_check = mt5_market_order_check(mapping.mt5_symbol, leg_b_side, leg_b_quantity, demo=simulated)
        mt5_tradability_cache.update(opportunity.symbol, mapping.mt5_symbol, leg_b_side, leg_b_quantity, mt5_check, "execution")
        if not mt5_check.allowed:
            reason = f"MT5 当前订单预检查失败: {mt5_check.message}"
            opportunity.reject_reason = reason
            db.add(opportunity)
            record_risk_event(db, "mt5_order_check_open", reason, opportunity.symbol)
            db.commit()
            raise ValueError(reason)
    synced, sync_reason, refreshed = _strict_sync_for_execution(mapping, opportunity.symbol, settings)
    if not synced:
        record_risk_event(db, "strict_quote_sync", sync_reason, opportunity.symbol)
        raise ValueError(sync_reason)
    if refreshed:
        still_executable, reason = _refreshed_opportunity_still_executable(opportunity, synced, strategy)
        if not still_executable:
            record_risk_event(db, "execution_quote_refresh", reason, opportunity.symbol)
            raise ValueError(reason)
    use_live_account_risk = live or (mode == "paper" and strategy.paper_use_live_account_risk)
    slippage_bps = settings.default_slippage_bps if refreshed else synced.time_diff_ms / 10
    decision = pre_trade_check(db, opportunity.symbol, opportunity.notional, slippage_bps, synced.leg_a.local_recv_ts, use_live_account_risk=use_live_account_risk)
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
        leg_b_quantity=opportunity.leg_b_quantity or opportunity.quantity,
        leg_a_quantity=opportunity.leg_a_quantity or opportunity.quantity,
        open_cost=opportunity.total_cost,
        trigger_spread=opportunity.gross_spread,
        trigger_leg_a_bid=opportunity.trigger_leg_a_bid,
        trigger_leg_a_ask=opportunity.trigger_leg_a_ask,
        trigger_leg_b_bid=opportunity.trigger_leg_b_bid,
        trigger_leg_b_ask=opportunity.trigger_leg_b_ask,
        entry_spread=opportunity.gross_spread,
        entry_threshold=opportunity.entry_threshold,
        exit_target=opportunity.exit_target,
        overheat_threshold=opportunity.overheat_threshold,
        source=source,
    )
    db.add(group)
    db.flush()

    leg_a_side = "buy" if opportunity.direction == "long_leg_a_short_leg_b" else "sell"
    leg_a_adapter, leg_b_adapter = _execution_adapters(live=live, simulated=simulated, mapping=mapping, db=db)
    leg_a_quantity = opportunity.leg_a_quantity or opportunity.quantity
    if mapping.execution_style == "hyper_maker_mt5_taker":
        results = _execute_hyper_maker_then_mt5(db, group.id, mapping, opportunity.symbol, leg_a_adapter, leg_b_adapter, leg_a_side, leg_b_side, leg_a_quantity, leg_b_quantity, synced)
    elif _paper_live_parallel_enabled(live=live, simulated=simulated, hl=leg_a_adapter, mapping=mapping, db=db):
        results = _execute_parallel_legs_with_compensation(
            db,
            group.id,
            mapping,
            opportunity.symbol,
            leg_a_adapter,
            leg_b_adapter,
            leg_a_side,
            leg_b_side,
            leg_a_quantity,
            leg_b_quantity,
            mapping.hl_open_order_type,
            mapping.mt5_open_order_type,
            strategy,
            reduce_only=False,
        )
    else:
        results = _execute_hyper_then_mt5_after_fill(
            db,
            group.id,
            mapping,
            opportunity.symbol,
            leg_a_adapter,
            leg_b_adapter,
            leg_a_side,
            leg_b_side,
            leg_a_quantity,
            leg_b_quantity,
            mapping.hl_open_order_type,
            mapping.mt5_open_order_type,
            strategy,
        )
    _quarantine_mt5_send_rejects(db, opportunity.symbol, mapping, leg_b_side, leg_b_quantity, results)

    if all(_has_position_effect(result) for result in results):
        group.status = "open"
        group.opened_at = datetime.now(timezone.utc).replace(tzinfo=None)
        group.fees = sum(result.fee for result in results)
        actual_entry_spread = actual_entry_spread_from_fills(db, group)
        if actual_entry_spread is not None:
            group.entry_spread = actual_entry_spread
        opportunity.status = "executed"
        detail = "双边订单成交"
        if actual_entry_spread is not None:
            detail = f"{detail}，真实开仓价差 {actual_entry_spread:.8f}"
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="opened", detail=detail))
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
    hedge_pool.upsert_group(group)
    return group


def _quarantine_mt5_send_rejects(db: Session, symbol: str, mapping: SymbolMapping, mt5_side: str, mt5_quantity: float, results: list) -> None:
    for result in results:
        message = str(getattr(result, "error_message", "") or "")
        if "retcode=10044" not in message:
            continue
        block_message = f"MT5 实际下单返回只允许平仓: {message}"
        block_mt5_tradability(db, symbol, mapping.mt5_symbol, mt5_side, mt5_quantity, block_message, source="order_send_reject")
        record_risk_event(db, "mt5_order_send_quarantine", block_message, symbol)


def _strict_sync_for_execution(mapping: SymbolMapping, symbol: str, settings) -> tuple[object | None, str, bool]:
    synced, sync_reason = quote_synchronizer.synchronized(
        symbol,
        mode="strict",
        max_time_diff_ms=settings.strict_quote_sync_ms,
        max_age_ms=settings.quote_stale_ms,
    )
    if synced:
        return synced, sync_reason, False
    refreshed_platforms = refresh_execution_quotes(mapping)
    if not refreshed_platforms:
        return synced, sync_reason, False
    refreshed_synced, refreshed_reason = quote_synchronizer.synchronized(
        symbol,
        mode="strict",
        max_time_diff_ms=settings.strict_quote_sync_ms,
        max_age_ms=settings.quote_stale_ms,
    )
    if not refreshed_synced:
        return refreshed_synced, f"{refreshed_reason}；执行前主动刷新: {','.join(refreshed_platforms)}", True
    return refreshed_synced, "", True


def _refreshed_opportunity_still_executable(opportunity: ArbitrageOpportunity, synced, strategy: StrategySetting) -> tuple[bool, str]:
    refreshed_spread = spreads_for_direction(
        opportunity.direction,
        synced.leg_a.bid,
        synced.leg_a.ask,
        synced.leg_b.bid,
        synced.leg_b.ask,
    ).entry_spread
    entry_threshold = float(opportunity.entry_threshold or 0.0)
    if entry_threshold > 0 and refreshed_spread < entry_threshold:
        return False, f"主动刷新后价差不再满足入场线: {refreshed_spread:.6f} < {entry_threshold:.6f}"
    quantity = float(opportunity.leg_a_quantity or opportunity.quantity or 0.0)
    unit_cost = float(opportunity.unit_cost or 0.0)
    refreshed_net_profit = (refreshed_spread - unit_cost) * quantity
    min_profit = max(float(strategy.min_total_profit or 0.0), float(strategy.min_net_profit or 0.0))
    if refreshed_net_profit < min_profit:
        return False, f"主动刷新后净利润不足: {refreshed_net_profit:.2f} < {min_profit:.2f}"
    return True, ""


def _final_close_still_executable(db: Session, group: HedgeGroup, mapping: SymbolMapping, strategy: StrategySetting, reason: str) -> tuple[bool, str]:
    settings = get_settings()
    synced, sync_reason, refreshed = _strict_sync_for_execution(mapping, group.symbol, settings)
    if not synced:
        return False, sync_reason
    close_spread = spreads_for_direction(
        group.direction,
        synced.leg_a.bid,
        synced.leg_a.ask,
        synced.leg_b.bid,
        synced.leg_b.ask,
    ).close_spread
    exit_target = _effective_close_exit_target(group, mapping)
    hold_expired = "超过最大持仓时间" in reason
    if exit_target != 0 and close_spread > exit_target and not hold_expired:
        suffix = "；执行前主动刷新" if refreshed else ""
        return False, f"自动平仓最终复核失败{suffix}: 平仓价差 {close_spread:.6f} > 退出线 {exit_target:.6f}"
    estimated_profit = pnl_from_close_spread(group, close_spread)
    min_profit = float(strategy.auto_close_min_profit or 0.0)
    if estimated_profit < min_profit:
        suffix = "；执行前主动刷新" if refreshed else ""
        return False, f"自动平仓最终复核失败{suffix}: 估算平仓利润 {estimated_profit:.2f} < {min_profit:.2f}"
    return True, ""


def _effective_close_exit_target(group: HedgeGroup, mapping: SymbolMapping) -> float:
    group_target = float(group.exit_target or 0.0)
    mapping_target = float(getattr(mapping, "max_close_spread", 0.0) or 0.0)
    if mapping_target == 0:
        return group_target
    if group_target == 0:
        return mapping_target
    return min(group_target, mapping_target)


def _execute_hyper_then_mt5_after_fill(
    db: Session,
    group_id: int,
    mapping: SymbolMapping,
    symbol: str,
    hl,
    mt5,
    hl_side: str,
    mt5_side: str,
    hl_quantity: float,
    mt5_quantity: float,
    hl_order_type: str,
    mt5_order_type: str,
    strategy: StrategySetting,
    *,
    reduce_only: bool = False,
) -> list:
    hl_result = _place_and_record(
        db,
        group_id,
        mapping.leg_a_venue,
        hl,
        symbol,
        mapping.leg_a_venue_symbol,
        hl_side,
        hl_quantity,
        hl_order_type,
        None,
        False,
        0,
        strategy,
        reduce_only=reduce_only,
        mapping=mapping,
    )
    if not _has_position_effect(hl_result):
        return [hl_result]
    fill_ratio = hl_result.filled_quantity / hl_quantity if hl_quantity > 0 else 0.0
    mt5_result = _place_and_record(
        db,
        group_id,
        mapping.leg_b_venue,
        mt5,
        symbol,
        mapping.mt5_symbol,
        mt5_side,
        mt5_quantity * fill_ratio,
        mt5_order_type,
        None,
        False,
        0,
        strategy,
        reduce_only=reduce_only,
        mapping=mapping,
    )
    return [hl_result, mt5_result]


def _execute_parallel_legs_with_compensation(
    db: Session,
    group_id: int,
    mapping: SymbolMapping,
    symbol: str,
    hl,
    mt5,
    hl_side: str,
    mt5_side: str,
    hl_quantity: float,
    mt5_quantity: float,
    hl_order_type: str,
    mt5_order_type: str,
    strategy: StrategySetting,
    *,
    reduce_only: bool,
) -> list:
    strategy_for_threads = _strategy_latency_snapshot(strategy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            mapping.leg_a_venue: pool.submit(
                _submit_leg_order,
                hl,
                mapping.leg_a_venue,
                symbol,
                mapping.leg_a_venue_symbol,
                hl_side,
                hl_quantity,
                hl_order_type,
                None,
                False,
                0,
                strategy_for_threads,
                reduce_only,
                mapping,
            ),
            mapping.leg_b_venue: pool.submit(
                _submit_leg_order,
                mt5,
                mapping.leg_b_venue,
                symbol,
                mapping.mt5_symbol,
                mt5_side,
                mt5_quantity,
                mt5_order_type,
                None,
                False,
                0,
                strategy_for_threads,
                reduce_only,
                mapping,
            ),
        }
        gateway_results = {platform: future.result() for platform, future in futures.items()}

    results = {
        mapping.leg_a_venue: _record_gateway_result(db, group_id, mapping.leg_a_venue, symbol, hl_side, hl_quantity, hl_order_type, None, False, 0, reduce_only, gateway_results[mapping.leg_a_venue]),
        mapping.leg_b_venue: _record_gateway_result(db, group_id, mapping.leg_b_venue, symbol, mt5_side, mt5_quantity, mt5_order_type, None, False, 0, reduce_only, gateway_results[mapping.leg_b_venue]),
    }
    ordered_results = [results[mapping.leg_a_venue], results[mapping.leg_b_venue]]
    filled = {platform: result for platform, result in results.items() if _has_position_effect(result)}
    if len(filled) == 1:
        platform, result = next(iter(filled.items()))
        compensation = _compensate_parallel_single_leg(db, group_id, mapping, symbol, platform, result, reduce_only=reduce_only)
        if compensation is not None:
            ordered_results.append(compensation)
    return ordered_results


def _submit_leg_order(
    adapter,
    platform: str,
    symbol: str,
    venue_symbol: str,
    side: str,
    quantity: float,
    order_type: str,
    price: float | None,
    post_only: bool,
    ttl_seconds: int,
    strategy: StrategySetting,
    reduce_only: bool,
    mapping: SymbolMapping | None = None,
):
    gateway = build_execution_gateway(adapter)
    return gateway.submit_order(
        LegOrderIntent(
            platform,
            symbol,
            side,
            quantity,
            venue_symbol=venue_symbol,
            price=price,
            order_type=order_type,
            post_only=post_only,
            reduce_only=reduce_only,
            ttl_seconds=ttl_seconds,
        ),
        paper_latency_ms=_paper_latency_ms(strategy, platform, adapter, mapping=mapping),
    )


def _record_gateway_result(
    db: Session,
    group_id: int,
    platform: str,
    symbol: str,
    side: str,
    quantity: float,
    order_type: str,
    price: float | None,
    post_only: bool,
    ttl_seconds: int,
    reduce_only: bool,
    gateway_result,
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
    db.flush()
    return result


def _compensate_parallel_single_leg(db: Session, group_id: int, mapping: SymbolMapping, symbol: str, platform: str, result, *, reduce_only: bool):
    side = "sell" if _latest_filled_order_side(db, group_id, platform) == "buy" else "buy"
    quantity = float(result.filled_quantity or 0.0)
    if quantity <= 0:
        return None
    adapter_live, simulated = (False, True)
    leg_a_adapter, leg_b_adapter = _execution_adapters(live=adapter_live, simulated=simulated, mapping=mapping, db=db)
    compensation_reduce_only = not reduce_only
    if platform == mapping.leg_a_venue:
        db.add(HedgeGroupEvent(hedge_group_id=group_id, event_type="parallel_single_leg_compensation", detail=f"Leg B 腿失败，反向冲销 {mapping.leg_a_venue} {quantity:g}"))
        return _place_and_record(db, group_id, mapping.leg_a_venue, leg_a_adapter, symbol, mapping.leg_a_venue_symbol, side, quantity, "market", None, False, 0, db.query(StrategySetting).first() or StrategySetting(), reduce_only=compensation_reduce_only, mapping=mapping)
    db.add(HedgeGroupEvent(hedge_group_id=group_id, event_type="parallel_single_leg_compensation", detail=f"{mapping.leg_a_venue} 腿失败，反向冲销 {mapping.leg_b_venue} {quantity:g}"))
    return _place_and_record(db, group_id, mapping.leg_b_venue, leg_b_adapter, symbol, mapping.mt5_symbol, side, quantity, "market", None, False, 0, db.query(StrategySetting).first() or StrategySetting(), reduce_only=compensation_reduce_only, mapping=mapping)


def _latest_filled_order_side(db: Session, group_id: int, platform: str) -> str:
    order = db.query(Order).filter(Order.hedge_group_id == group_id, Order.platform == platform).order_by(Order.created_at.desc()).first()
    return order.side if order else "buy"


def _execute_hyper_maker_then_mt5(db: Session, group_id: int, mapping: SymbolMapping, symbol: str, hl, mt5, hl_side: str, mt5_side: str, hl_quantity: float, mt5_quantity: float, synced) -> list:
    strategy = db.query(StrategySetting).first() or StrategySetting()
    hl_price = _maker_price(hl_side, synced.leg_a.bid, synced.leg_a.ask, mapping.hl_maker_offset_bps, mapping)
    hl_result = _place_and_record(
        db,
        group_id,
        mapping.leg_a_venue,
        hl,
        symbol,
        mapping.leg_a_venue_symbol,
        hl_side,
        hl_quantity,
        "limit",
        hl_price,
        True,
        mapping.hl_order_ttl_seconds,
        strategy,
        mapping=mapping,
    )
    if not _has_position_effect(hl_result):
        event_type = "maker_pending" if _is_pending_result(hl_result) else "maker_unfilled"
        db.add(HedgeGroupEvent(hedge_group_id=group_id, event_type=event_type, detail=hl_result.error_message or f"{mapping.leg_a_venue} maker 未成交"))
        return [hl_result]
    fill_ratio = hl_result.filled_quantity / hl_quantity if hl_quantity > 0 else 0.0
    mt5_result = _place_and_record(db, group_id, mapping.leg_b_venue, mt5, symbol, mapping.mt5_symbol, mt5_side, mt5_quantity * fill_ratio, "market", None, False, 0, strategy, mapping=mapping)
    return [hl_result, mt5_result]


def _maker_price(side: str, bid: float, ask: float, offset_bps: float, mapping: SymbolMapping | None = None) -> float:
    if side == "buy":
        raw_price = bid * (1 - offset_bps / 10_000)
        return _normalize_limit_price(raw_price, side, mapping)
    raw_price = ask * (1 + offset_bps / 10_000)
    return _normalize_limit_price(raw_price, side, mapping)


def _normalize_limit_price(price: float, side: str, mapping: SymbolMapping | None = None) -> float:
    if price <= 0:
        return price
    tick = float(getattr(mapping, "min_tick", 0.0) or 0.0) if mapping else 0.0
    if tick > 0:
        units = price / tick
        price = math.floor(units) * tick if side == "buy" else math.ceil(units) * tick
    precision = int(getattr(mapping, "price_precision", 9) if mapping else 9)
    precision = max(min(precision, 9), 0)
    return round(price, precision)


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
    mapping: SymbolMapping | None = None,
):
    if mapping is not None and platform == mapping.leg_a_venue and getattr(adapter, "simulated", False):
        refresh_execution_quotes(mapping, refresh_mt5=False)
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
        paper_latency_ms=_paper_latency_ms(strategy, platform, adapter, mapping=mapping),
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
    db.flush()
    return result


def _paper_latency_ms(strategy: StrategySetting, platform: str, adapter, *, mapping: SymbolMapping | None = None) -> int:
    if getattr(adapter, "live", False):
        return 0
    is_leg_a = mapping is not None and platform == mapping.leg_a_venue
    is_leg_b = mapping is not None and platform == mapping.leg_b_venue
    if is_leg_a or (mapping is None and platform == "hyperliquid"):
        low = strategy.paper_leg_a_latency_ms_min
        high = strategy.paper_leg_a_latency_ms_max
    elif is_leg_b or mapping is None:
        low = strategy.paper_leg_b_latency_ms_min
        high = strategy.paper_leg_b_latency_ms_max
    else:
        low = strategy.paper_leg_b_latency_ms_min
        high = strategy.paper_leg_b_latency_ms_max
    low = max(int(low), 0)
    high = max(int(high), low)
    return random.randint(low, high)


def _strategy_latency_snapshot(strategy: StrategySetting):
    return SimpleNamespace(
        paper_leg_a_latency_ms_min=int(strategy.paper_leg_a_latency_ms_min or 0),
        paper_leg_a_latency_ms_max=int(strategy.paper_leg_a_latency_ms_max or 0),
        paper_leg_b_latency_ms_min=int(strategy.paper_leg_b_latency_ms_min or 0),
        paper_leg_b_latency_ms_max=int(strategy.paper_leg_b_latency_ms_max or 0),
    )


def close_hedge_group(db: Session, group_id: int, reason: str, *, validate_final_close: bool = False) -> HedgeGroup:
    group = db.get(HedgeGroup, group_id)
    if not group:
        raise ValueError("对冲组不存在")
    if group.status not in {"open", "open_partial", "manual_intervention"}:
        raise ValueError("当前状态不允许平仓")
    if group.execution_mode == "paper":
        return _execute_close_hedge_group(db, group, reason, live=False, simulated=True, estimated_realized_pnl=None, success_event_type="closed", pending_event_type="close_pending", failed_event_type="close_failed", validate_final_close=validate_final_close)
    if group.execution_mode == "live":
        if not live_trading_enabled(db):
            raise ValueError("实盘平仓需要先开启 live_trading_enabled")
        _ensure_live_execution_ready(db)
        return _execute_close_hedge_group(db, group, reason, live=True, simulated=False, estimated_realized_pnl=None, success_event_type="closed", pending_event_type="close_pending", failed_event_type="close_failed", validate_final_close=validate_final_close)

    group.status = "closed"
    group.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    group.realized_pnl = group.unrealized_pnl - group.fees - group.funding - group.swap
    group.close_reason = reason
    db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="closed", detail=reason))
    db.commit()
    db.refresh(group)
    hedge_pool.upsert_group(group)
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
        simulated=True,
        estimated_realized_pnl=estimated_realized_pnl,
        success_event_type="auto_closed",
        pending_event_type="auto_close_pending",
        failed_event_type="auto_close_failed",
        validate_final_close=True,
    )


def _execute_close_hedge_group(
    db: Session,
    group: HedgeGroup,
    reason: str,
    *,
    live: bool,
    simulated: bool,
    estimated_realized_pnl: float | None,
    success_event_type: str,
    pending_event_type: str,
    failed_event_type: str,
    validate_final_close: bool = False,
) -> HedgeGroup:
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
    if not mapping:
        raise ValueError("品种映射不存在")
    session_state = mt5_session_state(mapping)
    mt5_close_allowed, mt5_close_reason = mt5_action_allowed(session_state, group.direction, "close")
    if not mt5_close_allowed:
        raise ValueError(mt5_close_reason)

    strategy = db.query(StrategySetting).first() or StrategySetting()
    if validate_final_close:
        final_ok, final_reason = _final_close_still_executable(db, group, mapping, strategy, reason)
        if not final_ok:
            record_risk_event(db, "auto_close_final_quote_check", final_reason, group.symbol)
            raise ValueError(final_reason)
    hl_side, mt5_side = _close_sides(group.direction)
    if simulated:
        _ensure_paper_execution_ready(db)
    leg_a_adapter, leg_b_adapter = _execution_adapters(live=live, simulated=simulated, mapping=mapping, db=db)
    leg_a_quantity = _platform_close_quantity(group.leg_a_quantity, group.quantity)
    leg_b_quantity = _platform_close_quantity(group.leg_b_quantity, group.quantity)
    results = []
    if leg_a_quantity > 0:
        if _paper_live_parallel_enabled(live=live, simulated=simulated, hl=leg_a_adapter, mapping=mapping, db=db):
            results = _execute_parallel_legs_with_compensation(
                db,
                group.id,
                mapping,
                group.symbol,
                leg_a_adapter,
                leg_b_adapter,
                hl_side,
                mt5_side,
                leg_a_quantity,
                leg_b_quantity,
                mapping.hl_close_order_type,
                mapping.mt5_close_order_type,
                strategy,
                reduce_only=True,
            )
        else:
            results = _execute_hyper_then_mt5_after_fill(
                db,
                group.id,
                mapping,
                group.symbol,
                leg_a_adapter,
                leg_b_adapter,
                hl_side,
                mt5_side,
                leg_a_quantity,
                leg_b_quantity,
                mapping.hl_close_order_type,
                mapping.mt5_close_order_type,
                strategy,
                reduce_only=True,
            )
    elif leg_b_quantity > 0:
        result = _place_and_record(db, group.id, mapping.leg_b_venue, leg_b_adapter, group.symbol, mapping.mt5_symbol, mt5_side, leg_b_quantity, mapping.mt5_close_order_type, None, False, 0, strategy, reduce_only=True, mapping=mapping)
        results.append(result)
    if not results:
        raise ValueError("对冲组没有可平仓数量")

    if all(_has_position_effect(result) for result in results):
        group.status = "closed"
        group.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        group.fees += sum(result.fee for result in results)
        realized_from_fills = realized_pnl_from_fills(db, group)
        if realized_from_fills is not None:
            group.realized_pnl = realized_from_fills
        elif estimated_realized_pnl is not None:
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
    hedge_pool.upsert_group(group)
    return group


def _close_sides(direction: str) -> tuple[str, str]:
    if direction == "long_leg_a_short_leg_b":
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


def _ensure_paper_execution_ready(db: Session) -> None:
    readiness = paper_execution_readiness(db)
    blocked = [item for item in readiness.get("checks", []) if item.get("status") == "block"]
    if blocked:
        detail = "; ".join(str(item.get("message") or item.get("component")) for item in blocked)
        raise ValueError(f"paper 完整模拟执行就绪检查未通过: {detail}")


def _execution_adapters(*, live: bool, simulated: bool, mapping: SymbolMapping | None = None, db: Session | None = None):
    leg_a_venue = mapping.leg_a_venue if mapping else "hyperliquid"
    leg_b_venue = mapping.leg_b_venue if mapping else "mt5"
    settings = get_settings()
    paper_live_leg_a = simulated and paper_live_probe_enabled_for_venue(db, settings, leg_a_venue)
    paper_live_leg_b = simulated and paper_live_probe_enabled_for_venue(db, settings, leg_b_venue)
    if leg_a_venue == "hyperliquid":
        leg_a_adapter = HyperliquidAdapter(live=live or paper_live_leg_a)
    else:
        leg_a_adapter = build_market_adapter(leg_a_venue, live=live or paper_live_leg_a)
    _configure_paper_live_adapter(leg_a_adapter, simulated=simulated, paper_live_probe=paper_live_leg_a)
    if leg_b_venue == "mt5":
        leg_b_adapter = MT5Adapter(live=live, demo=simulated)
    else:
        leg_b_adapter = build_market_adapter(leg_b_venue, live=live or paper_live_leg_b)
    _configure_paper_live_adapter(leg_b_adapter, simulated=simulated, paper_live_probe=paper_live_leg_b)
    return leg_a_adapter, leg_b_adapter


def _paper_live_parallel_enabled(*, live: bool, simulated: bool, hl, mapping: SymbolMapping, db: Session | None = None) -> bool:
    if live or not simulated:
        return False
    if mapping.execution_style == "hyper_maker_mt5_taker":
        return False
    settings = get_settings()
    return bool(runtime_paper_live_parallel_execution(db, settings) and getattr(hl, "paper_price_probe", False))


def _configure_paper_live_adapter(adapter, *, simulated: bool, paper_live_probe: bool) -> None:
    setattr(adapter, "simulated", bool(simulated))
    setattr(adapter, "paper_price_probe", bool(paper_live_probe))

def _has_position_effect(result) -> bool:
    return bool(result.success and result.filled_quantity > 0 and result.status in {"filled", "partially_filled"})


def _is_pending_result(result) -> bool:
    return result.status in {"accepted", "submitted", "pending", "open", "new"}

