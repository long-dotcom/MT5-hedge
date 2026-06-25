from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic, perf_counter
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.db.models import ArbitrageOpportunity, MarketSnapshot, SpreadBucket, SpreadCurrent, SpreadDirectionCurrent, SpreadSnapshot, StrategySetting, SymbolMapping, SystemLog, WorkerRun
from app.db.retention import prune_table_by_id
from app.market.symbols import enabled_mappings
from app.market.fx import fx_to_usd
from app.market.orderbook import order_book_cache, simulate_market_fill
from app.market.quotes import quote_synchronizer
from app.market.scan_state import scan_state_store
from app.market.mt5_sessions import mt5_action_allowed, mt5_session_state
from app.market.mt5_tradability import mt5_tradability_cache
from app.strategy.cost import estimate_cost
from app.strategy.live_costs import hyperliquid_cost_inputs, mt5_cost_inputs
from app.strategy.statistical_signal import evaluate_entry_signal
from app.strategy.spread_math import DIRECTIONS, LONG_HL_SHORT_MT5, spreads_for_direction


@dataclass
class BucketAccumulator:
    symbol: str
    direction: str
    bucket_start: datetime
    bucket_seconds: int
    open_spread: float
    high_spread: float
    low_spread: float
    close_spread: float
    unit_cost_sum: float
    unit_net_profit_sum: float
    spread_sum: float
    close_basis_sum: float
    mid_spread_sum: float
    spread_cost_sum: float
    sample_count: int


@dataclass
class PositionSizing:
    mt5_quantity: float
    mt5_base_quantity: float
    mt5_point_value_usd: float
    hyperliquid_quantity: float
    notional_usd: float
    currency: str
    fx_rate_to_usd: float
    fx_source: str


_bucket_accumulators: dict[tuple[str, str], BucketAccumulator] = {}
_last_snapshot_flush: dict[tuple[str, str], float] = {}
_scan_timings: dict[str, dict[str, float]] = {}
_strategy_cache: tuple[int, float, SimpleNamespace | None] = (0, 0.0, None)
_STRATEGY_CACHE_TTL_SECONDS = 2.0


def clear_strategy_setting_cache() -> None:
    global _strategy_cache
    _strategy_cache = (0, 0.0, None)


def get_strategy_setting(db: Session) -> SimpleNamespace:
    global _strategy_cache
    now = monotonic()
    bind_id = id(db.get_bind())
    cached_bind_id, cached_at, cached = _strategy_cache
    if cached and cached_bind_id == bind_id and now - cached_at < _STRATEGY_CACHE_TTL_SECONDS:
        return cached
    setting = db.query(StrategySetting).first()
    if not setting:
        setting = StrategySetting()
        db.add(setting)
        db.commit()
        db.refresh(setting)
    cached = SimpleNamespace(**{column.name: getattr(setting, column.name) for column in setting.__table__.columns})
    _strategy_cache = (bind_id, now, cached)
    return cached


def run_scan(db: Session) -> int:
    started = perf_counter()
    created = 0
    strategy = get_strategy_setting(db)
    settings = get_settings()
    try:
        current_payloads: list[dict] = []
        direction_payloads_all: list[dict] = []
        opportunity_payloads: list[dict] = []
        for mapping in enabled_mappings(db):
            symbol_started = perf_counter()
            timings: dict[str, float] = {}
            try:
                quote_sync_started = perf_counter()
                session_state = mt5_session_state(mapping)
                if not session_state.can_quote:
                    _record_duration(timings, "quote_sync_duration_ms", quote_sync_started)
                    persist_started = perf_counter()
                    current_payloads.append(_current_payload(
                        symbol=mapping.symbol,
                        direction="none",
                        hyperliquid_bid=0,
                        hyperliquid_ask=0,
                        mt5_bid=0,
                        mt5_ask=0,
                        quantity=0,
                        gross_spread=0,
                        unit_cost=0,
                        unit_net_profit=0,
                        total_cost=0,
                        net_profit=0,
                        annualized_return=0,
                        status="rejected",
                        reason=f"MT5 不可报价/不可交易: {session_state.status}，{session_state.reason}",
                    ))
                    _record_duration(timings, "persist_duration_ms", persist_started)
                    continue
                synced, sync_reason = quote_synchronizer.synchronized(
                    mapping.symbol,
                    mode="loose",
                    max_time_diff_ms=settings.loose_quote_sync_ms,
                    max_age_ms=max(settings.quote_stale_ms, settings.loose_quote_sync_ms),
                )
                _record_duration(timings, "quote_sync_duration_ms", quote_sync_started)
                if not synced:
                    persist_started = perf_counter()
                    current_payloads.append(_current_payload(
                        symbol=mapping.symbol,
                        direction="none",
                        hyperliquid_bid=0,
                        hyperliquid_ask=0,
                        mt5_bid=0,
                        mt5_ask=0,
                        quantity=0,
                        gross_spread=0,
                        unit_cost=0,
                        unit_net_profit=0,
                        total_cost=0,
                        net_profit=0,
                        annualized_return=0,
                        status="rejected",
                        reason=sync_reason,
                    ))
                    _record_duration(timings, "persist_duration_ms", persist_started)
                    continue
                hl = synced.hyperliquid
                mt = synced.mt5

                sizing_started = perf_counter()
                try:
                    sizing = _position_sizing(mapping, mt.mid, hl.mid, strategy.default_notional)
                except ValueError as exc:
                    _record_duration(timings, "sizing_duration_ms", sizing_started)
                    persist_started = perf_counter()
                    current_payloads.append(_current_payload(
                        symbol=mapping.symbol,
                        direction="none",
                        hyperliquid_bid=hl.bid,
                        hyperliquid_ask=hl.ask,
                        mt5_bid=mt.bid,
                        mt5_ask=mt.ask,
                        quantity=0,
                        mt5_quantity=0,
                        hyperliquid_quantity=0,
                        notional_currency=(mapping.mt5_currency_profit or mapping.quote_asset or "USD"),
                        fx_rate_to_usd=0,
                        gross_spread=0,
                        unit_cost=0,
                        unit_net_profit=0,
                        total_cost=0,
                        net_profit=0,
                        annualized_return=0,
                        status="rejected",
                        reason=str(exc),
                    ))
                    _record_duration(timings, "persist_duration_ms", persist_started)
                    continue
                _record_duration(timings, "sizing_duration_ms", sizing_started)

                holding_hours = max(strategy.max_holding_minutes / 60, 1)
                hl_costs = hyperliquid_cost_inputs(mapping.hyperliquid_symbol)
                persist_started = perf_counter()
                direction_payloads = []
                cost_started = perf_counter()
                signal_started = perf_counter()
                candidate_started = perf_counter()
                for direction in DIRECTIONS:
                    spread_values = spreads_for_direction(direction, hl.bid, hl.ask, mt.bid, mt.ask)
                    gross_spread = spread_values.entry_spread
                    gross_profit = gross_spread * sizing.hyperliquid_quantity
                    quantity = sizing.mt5_quantity
                    notional = sizing.notional_usd
                    hyperliquid_side = "buy" if direction == LONG_HL_SHORT_MT5 else "sell"
                    mt5_side = "sell" if direction == LONG_HL_SHORT_MT5 else "buy"
                    mt5_costs = mt5_cost_inputs(mapping.mt5_symbol, mt5_side, sizing.mt5_quantity, holding_hours / 24)
                    cost = estimate_cost(
                        notional,
                        mt.bid,
                        mt.ask,
                        min(mapping.max_slippage_bps, settings.default_slippage_bps),
                        quantity=sizing.hyperliquid_quantity,
                        hyperliquid_bid=hl.bid,
                        hyperliquid_ask=hl.ask,
                        hyperliquid_fee_rate=_hl_fee_rate(mapping.hl_open_order_type, hl_costs),
                        hyperliquid_fee_round_trips=settings.hyperliquid_fee_round_trips,
                        hyperliquid_close_fee_rate=_hl_fee_rate(mapping.hl_close_order_type, hl_costs),
                        hyperliquid_funding_rate=hl_costs.funding_rate,
                        hyperliquid_side=hyperliquid_side,
                        mt5_commission_rate=mt5_costs.commission_rate,
                        mt5_swap_cost=mt5_costs.swap_cost,
                        holding_hours=holding_hours,
                        mt5_spread_rebate_rate=settings.mt5_spread_rebate_rate,
                        fx_cost_rate=settings.default_fx_cost_rate,
                        source=f"{hl_costs.source};{mt5_costs.source}",
                    )
                    net_profit = gross_profit - cost.total
                    unit_cost = cost.total / sizing.hyperliquid_quantity if sizing.hyperliquid_quantity > 0 else cost.total
                    unit_net_profit = gross_spread - unit_cost
                    annualized_return = (net_profit / notional) * (365 * 24 / holding_hours)
                    statistical_signal = evaluate_entry_signal(
                        db,
                        strategy,
                        mapping.symbol,
                        direction,
                        gross_spread,
                        unit_cost,
                        unit_net_profit,
                        net_profit,
                        annualized_return,
                    )
                    signal = statistical_signal.result
                    entry_threshold = _effective_entry_threshold(mapping, statistical_signal.reachable_entry)
                    exit_target = _effective_exit_target(mapping, statistical_signal.exit_target)
                    min_entry_spread = float(mapping.min_entry_spread or 0.0)
                    if min_entry_spread > 0 and gross_spread < min_entry_spread and signal.status in {"candidate", "executable"}:
                        signal.status = "candidate"
                        signal.reason = f"未达到品种最小买入价差: {gross_spread:.2f} < {min_entry_spread:.2f}"
                    if signal.status in {"candidate", "executable"}:
                        liquidity_reason = _hyperliquid_liquidity_reason(mapping.symbol, hyperliquid_side, sizing.hyperliquid_quantity, notional, hl.depth_notional)
                        if liquidity_reason:
                            signal.status = "candidate"
                            signal.reason = liquidity_reason
                    mt5_open_allowed, mt5_open_reason = mt5_action_allowed(session_state, direction, "open")
                    if not mt5_open_allowed:
                        signal.status = "rejected"
                        signal.reason = mt5_open_reason
                    else:
                        tradability_allowed, tradability_reason = mt5_tradability_cache.is_fresh_allowed(mapping.symbol, mt5_side)
                        if not tradability_allowed:
                            signal.status = "rejected"
                            signal.reason = f"MT5 交易能力未确认: {tradability_reason}"
                    reason = signal.reason or f"loose_sync={synced.time_diff_ms:.0f}ms; mt5_session={session_state.status}"
                    payload = dict(
                        symbol=mapping.symbol,
                        direction=direction,
                        hyperliquid_bid=hl.bid,
                        hyperliquid_ask=hl.ask,
                        mt5_bid=mt.bid,
                        mt5_ask=mt.ask,
                        quantity=quantity,
                        mt5_quantity=sizing.mt5_quantity,
                        hyperliquid_quantity=sizing.hyperliquid_quantity,
                        notional_currency=sizing.currency,
                        fx_rate_to_usd=sizing.fx_rate_to_usd,
                        gross_spread=gross_spread,
                        entry_spread=spread_values.entry_spread,
                        close_spread=spread_values.close_spread,
                        mid_spread=spread_values.mid_spread,
                        spread_cost=spread_values.spread_cost,
                        unit_cost=unit_cost,
                        unit_net_profit=unit_net_profit,
                        total_cost=cost.total,
                        net_profit=net_profit,
                        annualized_return=annualized_return,
                        status=signal.status,
                        reason=reason,
                        sampled_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        hyperliquid_captured_at=hl.local_recv_ts,
                        mt5_captured_at=mt.local_recv_ts,
                        hyperliquid_depth_notional=hl.depth_notional,
                        mt5_depth_notional=mt.depth_notional,
                    )
                    direction_payloads_all.append(payload)
                    opportunity_payload = _opportunity_payload(
                        payload,
                        notional=notional,
                        entry_threshold=entry_threshold,
                        exit_target=exit_target,
                        overheat_threshold=statistical_signal.overheat,
                        signal_sample_count=statistical_signal.sample_count,
                        reason=signal.reason,
                    )
                    if opportunity_payload:
                        created += 1
                        opportunity_payloads.append(opportunity_payload)
                    direction_payloads.append(payload)
                _record_duration(timings, "cost_duration_ms", cost_started)
                _record_duration(timings, "signal_duration_ms", signal_started)
                _record_duration(timings, "candidate_sync_duration_ms", candidate_started)
                best_payload = _best_current_payload(direction_payloads)
                current_payloads.append(best_payload)
                _record_duration(timings, "persist_duration_ms", persist_started)
            finally:
                timings["symbol_scan_duration_ms"] = _elapsed_ms(symbol_started)
                _scan_timings[mapping.symbol.upper()] = timings
        _update_scan_state_store(current_payloads, opportunity_payloads, direction_payloads_all)
        return created
    except Exception as exc:
        db.rollback()
        try:
            db.add(WorkerRun(worker_name="spread_scanner", status="failed", duration_ms=int((perf_counter() - started) * 1000), error_message=str(exc)))
            db.add(SystemLog(level="error", category="scanner", message="价差扫描失败", context=str(exc)))
            prune_table_by_id(db, WorkerRun)
            prune_table_by_id(db, SystemLog)
            db.commit()
        except Exception:
            db.rollback()
        raise


def _round_up_to_step(value: float, step: float, precision: int) -> float:
    if step <= 0:
        return round(value, precision)
    units = int(value / step)
    if units * step < value:
        units += 1
    return round(units * step, precision)


def _position_sizing(mapping, mt5_mid: float, hyperliquid_mid: float, target_notional_usd: float) -> PositionSizing:
    currency = (mapping.mt5_currency_profit or mapping.quote_asset or "USD").upper()
    fx = fx_to_usd(currency)
    contract_size = mapping.mt5_contract_size or mapping.contract_multiplier or 1.0
    lot_min = mapping.mt5_min_lot or mapping.min_order_size or 0.0
    lot_step = mapping.mt5_volume_step or lot_min or 0.0
    if mt5_mid <= 0 or hyperliquid_mid <= 0:
        raise ValueError("报价异常，无法计算名义价值")
    lot_notional_usd = mt5_mid * contract_size * fx.rate_to_usd
    if lot_notional_usd <= 0:
        raise ValueError("MT5 单手 USD 名义价值异常")
    raw_lots = max(target_notional_usd / lot_notional_usd, lot_min)
    mt5_lots = _round_up_to_step(raw_lots, lot_step, _decimal_places(lot_step))
    mt5_base_quantity = mt5_lots * contract_size
    mt5_point_value_usd = mt5_base_quantity * fx.rate_to_usd
    notional_usd = mt5_mid * mt5_base_quantity * fx.rate_to_usd
    hyperliquid_quantity = mt5_point_value_usd
    if hyperliquid_quantity <= 0:
        raise ValueError("Hyperliquid 数量异常")
    return PositionSizing(
        mt5_quantity=mt5_lots,
        mt5_base_quantity=mt5_base_quantity,
        mt5_point_value_usd=mt5_point_value_usd,
        hyperliquid_quantity=round(hyperliquid_quantity, 8),
        notional_usd=notional_usd,
        currency=currency,
        fx_rate_to_usd=fx.rate_to_usd,
        fx_source=fx.source,
    )


def _decimal_places(value: float) -> int:
    text = f"{value:.12f}".rstrip("0").rstrip(".")
    return len(text.split(".", 1)[1]) if "." in text else 0


def _hl_fee_rate(order_type: str, hl_costs) -> float:
    return hl_costs.maker_fee_rate if order_type == "limit" else hl_costs.taker_fee_rate


def _hyperliquid_liquidity_reason(symbol: str, side: str, quantity: float, notional: float, top_depth_notional: float) -> str:
    book = order_book_cache.latest("hyperliquid", symbol)
    if book:
        fill = simulate_market_fill(book, side, quantity)
        if not fill.enough_liquidity:
            return f"Hyperliquid L2 深度不足: 目标 {quantity:.8f}，可成交 {fill.filled_quantity:.8f}"
        return ""
    if top_depth_notional > 0 and notional > top_depth_notional:
        return f"Hyperliquid 顶层深度不足: 目标 {notional:.2f} USD > 深度 {top_depth_notional:.2f} USD"
    return ""


def _effective_entry_threshold(mapping: SymbolMapping, statistical_threshold: float) -> float:
    min_entry_spread = float(getattr(mapping, "min_entry_spread", 0.0) or 0.0)
    return max(float(statistical_threshold or 0.0), min_entry_spread)


def _effective_exit_target(mapping: SymbolMapping, statistical_target: float) -> float:
    max_close_spread = float(getattr(mapping, "max_close_spread", 0.0) or 0.0)
    statistical = float(statistical_target or 0.0)
    if max_close_spread <= 0:
        return statistical
    if statistical <= 0:
        return max_close_spread
    return min(statistical, max_close_spread)


def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000


def _record_duration(timings: dict[str, float], key: str, started: float) -> None:
    timings[key] = _elapsed_ms(started)


def _current_payload(**values) -> dict:
    values.setdefault("entry_spread", values.get("gross_spread", 0.0))
    values.setdefault("close_spread", values.get("gross_spread", 0.0))
    values.setdefault("mid_spread", values.get("gross_spread", 0.0))
    values.setdefault("spread_cost", values.get("close_spread", 0.0) - values.get("entry_spread", 0.0))
    values.setdefault("mt5_quantity", values.get("quantity", 0.0))
    values.setdefault("hyperliquid_quantity", values.get("quantity", 0.0))
    values.setdefault("notional_currency", "USD")
    values.setdefault("fx_rate_to_usd", 1.0)
    values.setdefault("sampled_at", datetime.now(timezone.utc).replace(tzinfo=None))
    return values


def _opportunity_payload(
    payload: dict,
    *,
    notional: float,
    entry_threshold: float,
    exit_target: float,
    overheat_threshold: float,
    signal_sample_count: int,
    reason: str,
) -> dict | None:
    if payload["status"] not in {"candidate", "executable", "executing"}:
        return None
    return {
        **{key: payload[key] for key in (
            "symbol", "direction", "hyperliquid_bid", "hyperliquid_ask", "mt5_bid", "mt5_ask",
            "quantity", "mt5_quantity", "hyperliquid_quantity", "notional_currency", "fx_rate_to_usd",
            "gross_spread", "unit_cost", "unit_net_profit", "total_cost", "net_profit",
            "annualized_return", "status"
        )},
        "notional": notional,
        "entry_threshold": entry_threshold,
        "exit_target": exit_target,
        "overheat_threshold": overheat_threshold,
        "signal_sample_count": signal_sample_count,
        "reason": reason,
        "created_at": payload.get("sampled_at"),
        "updated_at": payload.get("sampled_at"),
    }


def persist_scan_state(db: Session) -> dict[str, int]:
    state = scan_state_store.snapshot()
    if not state["ready"]:
        return {"spread_direction_current": 0, "spread_current": 0, "opportunities": 0, "history": 0}
    settings = get_settings()
    direction_spreads = state.get("direction_spreads", [])
    current_spreads = state.get("spreads", [])
    opportunities = state.get("opportunities", [])
    ids_by_key: dict[tuple[str, str], int] = {}

    for payload in direction_spreads:
        _upsert_direction_current(db, **_filter_payload(payload, _spread_direction_fields()))
        _record_spread_history_from_payload(db, payload, settings)
    for payload in current_spreads:
        _upsert_current_spread(db, **_filter_payload(payload, _spread_current_fields()))
    scanned_symbols = {str(row.get("symbol", "")).upper() for row in [*direction_spreads, *current_spreads] if row.get("symbol")}
    changed_opportunities = _persist_opportunities(db, opportunities, ids_by_key, scanned_symbols)
    db.commit()
    scan_state_store.merge_opportunity_ids(ids_by_key)
    return {
        "spread_direction_current": len(direction_spreads),
        "spread_current": len(current_spreads),
        "opportunities": changed_opportunities,
        "history": len(direction_spreads),
    }


def _filter_payload(payload: dict, fields: set[str]) -> dict:
    return {key: payload[key] for key in fields if key in payload}


def _spread_current_fields() -> set[str]:
    return {
        "symbol", "direction", "hyperliquid_bid", "hyperliquid_ask", "mt5_bid", "mt5_ask",
        "quantity", "gross_spread", "unit_cost", "unit_net_profit", "total_cost", "net_profit",
        "annualized_return", "status", "reason", "entry_spread", "close_spread", "mid_spread",
        "spread_cost", "mt5_quantity", "hyperliquid_quantity", "notional_currency", "fx_rate_to_usd",
    }


def _spread_direction_fields() -> set[str]:
    return _spread_current_fields() - {"direction"} | {"direction"}


def _upsert_current_spread(
    db: Session,
    *,
    symbol: str,
    direction: str,
    hyperliquid_bid: float,
    hyperliquid_ask: float,
    mt5_bid: float,
    mt5_ask: float,
    quantity: float,
    gross_spread: float,
    unit_cost: float,
    unit_net_profit: float,
    total_cost: float,
    net_profit: float,
    annualized_return: float,
    status: str,
    reason: str,
    entry_spread: float = 0.0,
    close_spread: float = 0.0,
    mid_spread: float = 0.0,
    spread_cost: float = 0.0,
    mt5_quantity: float = 0.0,
    hyperliquid_quantity: float = 0.0,
    notional_currency: str = "USD",
    fx_rate_to_usd: float = 1.0,
) -> SpreadCurrent:
    row = db.query(SpreadCurrent).filter(SpreadCurrent.symbol == symbol).first()
    if not row:
        row = SpreadCurrent(symbol=symbol)
        db.add(row)
    row.direction = direction
    row.hyperliquid_bid = hyperliquid_bid
    row.hyperliquid_ask = hyperliquid_ask
    row.mt5_bid = mt5_bid
    row.mt5_ask = mt5_ask
    row.quantity = quantity
    row.mt5_quantity = mt5_quantity or quantity
    row.hyperliquid_quantity = hyperliquid_quantity or quantity
    row.notional_currency = notional_currency
    row.fx_rate_to_usd = fx_rate_to_usd
    row.gross_spread = gross_spread
    row.entry_spread = entry_spread or gross_spread
    row.close_spread = close_spread if close_spread or entry_spread else gross_spread
    row.mid_spread = mid_spread if mid_spread or entry_spread else gross_spread
    row.spread_cost = spread_cost if spread_cost or entry_spread else row.close_spread - row.entry_spread
    row.unit_cost = unit_cost
    row.unit_net_profit = unit_net_profit
    row.total_cost = total_cost
    row.net_profit = net_profit
    row.annualized_return = annualized_return
    row.status = status
    row.reason = reason
    row.sampled_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return row


def _upsert_direction_current(
    db: Session,
    *,
    symbol: str,
    direction: str,
    hyperliquid_bid: float,
    hyperliquid_ask: float,
    mt5_bid: float,
    mt5_ask: float,
    quantity: float,
    gross_spread: float,
    entry_spread: float,
    close_spread: float,
    mid_spread: float,
    spread_cost: float,
    unit_cost: float,
    unit_net_profit: float,
    total_cost: float,
    net_profit: float,
    annualized_return: float,
    status: str,
    reason: str,
    mt5_quantity: float = 0.0,
    hyperliquid_quantity: float = 0.0,
    notional_currency: str = "USD",
    fx_rate_to_usd: float = 1.0,
) -> SpreadDirectionCurrent:
    row = db.query(SpreadDirectionCurrent).filter(SpreadDirectionCurrent.symbol == symbol, SpreadDirectionCurrent.direction == direction).first()
    if not row:
        row = SpreadDirectionCurrent(symbol=symbol, direction=direction)
        db.add(row)
    row.hyperliquid_bid = hyperliquid_bid
    row.hyperliquid_ask = hyperliquid_ask
    row.mt5_bid = mt5_bid
    row.mt5_ask = mt5_ask
    row.quantity = quantity
    row.mt5_quantity = mt5_quantity or quantity
    row.hyperliquid_quantity = hyperliquid_quantity or quantity
    row.notional_currency = notional_currency
    row.fx_rate_to_usd = fx_rate_to_usd
    row.gross_spread = entry_spread
    row.entry_spread = entry_spread
    row.close_spread = close_spread
    row.mid_spread = mid_spread
    row.spread_cost = spread_cost
    row.unit_cost = unit_cost
    row.unit_net_profit = unit_net_profit
    row.total_cost = total_cost
    row.net_profit = net_profit
    row.annualized_return = annualized_return
    row.status = status
    row.reason = reason
    row.sampled_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return row


def _record_spread_history(
    db: Session,
    *,
    symbol: str,
    direction: str,
    hyperliquid_bid: float,
    hyperliquid_ask: float,
    mt5_bid: float,
    mt5_ask: float,
    hyperliquid,
    mt5,
    quantity: float,
    mt5_quantity: float,
    hyperliquid_quantity: float,
    notional_currency: str,
    fx_rate_to_usd: float,
    gross_spread: float,
    entry_spread: float,
    close_spread: float,
    mid_spread: float,
    spread_cost: float,
    unit_cost: float,
    unit_net_profit: float,
    total_cost: float,
    net_profit: float,
    annualized_return: float,
    status: str,
    reason: str,
    settings,
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    bucket_seconds = max(settings.spread_bucket_seconds, 1)
    bucket_start_ts = int(now.timestamp()) // bucket_seconds * bucket_seconds
    bucket_start = datetime.utcfromtimestamp(bucket_start_ts)
    key = (symbol, direction)
    accumulator = _bucket_accumulators.get(key)
    if not accumulator or accumulator.bucket_start != bucket_start:
        if accumulator:
            _flush_bucket(db, accumulator)
        accumulator = BucketAccumulator(
            symbol=symbol,
            direction=direction,
            bucket_start=bucket_start,
            bucket_seconds=bucket_seconds,
            open_spread=gross_spread,
            high_spread=gross_spread,
            low_spread=gross_spread,
            close_spread=gross_spread,
            unit_cost_sum=unit_cost,
            unit_net_profit_sum=unit_net_profit,
            spread_sum=gross_spread,
            close_basis_sum=close_spread,
            mid_spread_sum=mid_spread,
            spread_cost_sum=spread_cost,
            sample_count=1,
        )
        _bucket_accumulators[key] = accumulator
    else:
        accumulator.high_spread = max(accumulator.high_spread, gross_spread)
        accumulator.low_spread = min(accumulator.low_spread, gross_spread)
        accumulator.close_spread = gross_spread
        accumulator.unit_cost_sum += unit_cost
        accumulator.unit_net_profit_sum += unit_net_profit
        accumulator.spread_sum += gross_spread
        accumulator.close_basis_sum += close_spread
        accumulator.mid_spread_sum += mid_spread
        accumulator.spread_cost_sum += spread_cost
        accumulator.sample_count += 1

    history_interval = max(settings.spread_history_interval_seconds, 1)
    last_flush = _last_snapshot_flush.get(key, 0.0)
    if now.timestamp() - last_flush >= history_interval:
        _flush_bucket(db, accumulator)
        _last_snapshot_flush[key] = now.timestamp()
        db.add(MarketSnapshot(platform="hyperliquid", symbol=symbol, bid=hyperliquid.bid, ask=hyperliquid.ask, mid=hyperliquid.mid, depth_notional=hyperliquid.depth_notional, captured_at=hyperliquid.local_recv_ts))
        db.add(MarketSnapshot(platform="mt5", symbol=symbol, bid=mt5.bid, ask=mt5.ask, mid=mt5.mid, depth_notional=mt5.depth_notional, captured_at=mt5.local_recv_ts))
        db.add(
            SpreadSnapshot(
                symbol=symbol,
                direction=direction,
                hyperliquid_bid=hyperliquid.bid,
                hyperliquid_ask=hyperliquid.ask,
                mt5_bid=mt5.bid,
                mt5_ask=mt5.ask,
                quantity=quantity,
                mt5_quantity=mt5_quantity,
                hyperliquid_quantity=hyperliquid_quantity,
                notional_currency=notional_currency,
                fx_rate_to_usd=fx_rate_to_usd,
                gross_spread=entry_spread,
                entry_spread=entry_spread,
                close_spread=close_spread,
                mid_spread=mid_spread,
                spread_cost=spread_cost,
                unit_cost=unit_cost,
                unit_net_profit=unit_net_profit,
                total_cost=total_cost,
                net_profit=net_profit,
                annualized_return=annualized_return,
                status=status,
                reason=reason,
            )
        )


def _record_spread_history_from_payload(db: Session, payload: dict, settings) -> None:
    now = payload.get("sampled_at") or datetime.now(timezone.utc).replace(tzinfo=None)
    bucket_seconds = max(settings.spread_bucket_seconds, 1)
    bucket_start_ts = int(now.timestamp()) // bucket_seconds * bucket_seconds
    bucket_start = datetime.utcfromtimestamp(bucket_start_ts)
    symbol = payload["symbol"]
    direction = payload["direction"]
    gross_spread = float(payload.get("gross_spread") or 0.0)
    unit_cost = float(payload.get("unit_cost") or 0.0)
    unit_net_profit = float(payload.get("unit_net_profit") or 0.0)
    close_spread = float(payload.get("close_spread") or gross_spread)
    mid_spread = float(payload.get("mid_spread") or gross_spread)
    spread_cost = float(payload.get("spread_cost") or 0.0)
    key = (symbol, direction)
    accumulator = _bucket_accumulators.get(key)
    if not accumulator or accumulator.bucket_start != bucket_start:
        if accumulator:
            _flush_bucket(db, accumulator)
        accumulator = BucketAccumulator(
            symbol=symbol,
            direction=direction,
            bucket_start=bucket_start,
            bucket_seconds=bucket_seconds,
            open_spread=gross_spread,
            high_spread=gross_spread,
            low_spread=gross_spread,
            close_spread=gross_spread,
            unit_cost_sum=unit_cost,
            unit_net_profit_sum=unit_net_profit,
            spread_sum=gross_spread,
            close_basis_sum=close_spread,
            mid_spread_sum=mid_spread,
            spread_cost_sum=spread_cost,
            sample_count=1,
        )
        _bucket_accumulators[key] = accumulator
    else:
        accumulator.high_spread = max(accumulator.high_spread, gross_spread)
        accumulator.low_spread = min(accumulator.low_spread, gross_spread)
        accumulator.close_spread = gross_spread
        accumulator.unit_cost_sum += unit_cost
        accumulator.unit_net_profit_sum += unit_net_profit
        accumulator.spread_sum += gross_spread
        accumulator.close_basis_sum += close_spread
        accumulator.mid_spread_sum += mid_spread
        accumulator.spread_cost_sum += spread_cost
        accumulator.sample_count += 1

    history_interval = max(settings.spread_history_interval_seconds, 1)
    last_flush = _last_snapshot_flush.get(key, 0.0)
    if now.timestamp() - last_flush < history_interval:
        return
    _flush_bucket(db, accumulator)
    _last_snapshot_flush[key] = now.timestamp()
    db.add(MarketSnapshot(platform="hyperliquid", symbol=symbol, bid=payload["hyperliquid_bid"], ask=payload["hyperliquid_ask"], mid=(payload["hyperliquid_bid"] + payload["hyperliquid_ask"]) / 2, depth_notional=float(payload.get("hyperliquid_depth_notional") or 0.0), captured_at=payload.get("hyperliquid_captured_at") or now))
    db.add(MarketSnapshot(platform="mt5", symbol=symbol, bid=payload["mt5_bid"], ask=payload["mt5_ask"], mid=(payload["mt5_bid"] + payload["mt5_ask"]) / 2, depth_notional=float(payload.get("mt5_depth_notional") or 0.0), captured_at=payload.get("mt5_captured_at") or now))
    db.add(
        SpreadSnapshot(
            symbol=symbol,
            direction=direction,
            hyperliquid_bid=payload["hyperliquid_bid"],
            hyperliquid_ask=payload["hyperliquid_ask"],
            mt5_bid=payload["mt5_bid"],
            mt5_ask=payload["mt5_ask"],
            quantity=payload["quantity"],
            mt5_quantity=payload.get("mt5_quantity", payload["quantity"]),
            hyperliquid_quantity=payload.get("hyperliquid_quantity", payload["quantity"]),
            notional_currency=payload.get("notional_currency", "USD"),
            fx_rate_to_usd=payload.get("fx_rate_to_usd", 1.0),
            gross_spread=payload.get("entry_spread", gross_spread),
            entry_spread=payload.get("entry_spread", gross_spread),
            close_spread=close_spread,
            mid_spread=mid_spread,
            spread_cost=spread_cost,
            unit_cost=unit_cost,
            unit_net_profit=unit_net_profit,
            total_cost=payload.get("total_cost", 0.0),
            net_profit=payload.get("net_profit", 0.0),
            annualized_return=payload.get("annualized_return", 0.0),
            status=payload.get("status", "rejected"),
            reason=payload.get("reason", ""),
        )
    )


def _flush_bucket(db: Session, accumulator: BucketAccumulator) -> None:
    row = (
        db.query(SpreadBucket)
        .filter(
            SpreadBucket.symbol == accumulator.symbol,
            SpreadBucket.direction == accumulator.direction,
            SpreadBucket.bucket_start == accumulator.bucket_start,
            SpreadBucket.bucket_seconds == accumulator.bucket_seconds,
        )
        .first()
    )
    if not row:
        row = SpreadBucket(
            symbol=accumulator.symbol,
            direction=accumulator.direction,
            bucket_start=accumulator.bucket_start,
            bucket_seconds=accumulator.bucket_seconds,
        )
        db.add(row)
    row.open_spread = accumulator.open_spread
    row.high_spread = accumulator.high_spread
    row.low_spread = accumulator.low_spread
    row.close_spread = accumulator.close_spread
    row.avg_spread = accumulator.spread_sum / max(accumulator.sample_count, 1)
    row.entry_spread = accumulator.close_spread
    row.avg_entry_spread = row.avg_spread
    row.avg_close_basis_spread = accumulator.close_basis_sum / max(accumulator.sample_count, 1)
    row.avg_mid_spread = accumulator.mid_spread_sum / max(accumulator.sample_count, 1)
    row.avg_spread_cost = accumulator.spread_cost_sum / max(accumulator.sample_count, 1)
    row.avg_unit_cost = accumulator.unit_cost_sum / max(accumulator.sample_count, 1)
    row.avg_unit_net_profit = accumulator.unit_net_profit_sum / max(accumulator.sample_count, 1)
    row.sample_count = accumulator.sample_count


def _sync_current_opportunity(
    db: Session,
    *,
    symbol: str,
    direction: str,
    hyperliquid_bid: float,
    hyperliquid_ask: float,
    mt5_bid: float,
    mt5_ask: float,
    notional: float,
    quantity: float,
    mt5_quantity: float,
    hyperliquid_quantity: float,
    notional_currency: str,
    fx_rate_to_usd: float,
    gross_spread: float,
    unit_cost: float,
    unit_net_profit: float,
    total_cost: float,
    net_profit: float,
    annualized_return: float,
    entry_threshold: float,
    exit_target: float,
    overheat_threshold: float,
    signal_sample_count: int,
    status: str,
    reason: str,
) -> bool:
    active_statuses = ("candidate", "executable", "executing")
    active_rows = db.query(ArbitrageOpportunity).filter(
        ArbitrageOpportunity.symbol == symbol,
        ArbitrageOpportunity.direction == direction,
        ArbitrageOpportunity.status.in_(active_statuses),
    ).all()
    if status not in active_statuses:
        for row in active_rows:
            row.status = "rejected"
            row.reject_reason = reason or "价差回落，不再满足候选条件"
        return False

    current = active_rows[0] if active_rows else None
    if not current:
        current = ArbitrageOpportunity(symbol=symbol, direction=direction)
        db.add(current)
    elif current.status == "executing":
        current.reject_reason = "自动执行中，扫描不覆盖该机会"
        return False

    current.notional = notional
    current.quantity = quantity
    current.mt5_quantity = mt5_quantity
    current.hyperliquid_quantity = hyperliquid_quantity
    current.notional_currency = notional_currency
    current.fx_rate_to_usd = fx_rate_to_usd
    current.gross_spread = gross_spread
    current.trigger_hyperliquid_bid = hyperliquid_bid
    current.trigger_hyperliquid_ask = hyperliquid_ask
    current.trigger_mt5_bid = mt5_bid
    current.trigger_mt5_ask = mt5_ask
    current.unit_cost = unit_cost
    current.unit_net_profit = unit_net_profit
    current.total_cost = total_cost
    current.net_profit = net_profit
    current.annualized_return = annualized_return
    current.entry_threshold = entry_threshold
    current.exit_target = exit_target
    current.overheat_threshold = overheat_threshold
    current.signal_sample_count = signal_sample_count
    current.status = status
    current.reject_reason = reason
    return True


def _persist_opportunities(
    db: Session,
    opportunities: list[dict],
    ids_by_key: dict[tuple[str, str], int],
    scanned_symbols: set[str],
) -> int:
    active_statuses = ("candidate", "executable", "executing")
    active_keys = {(str(row.get("symbol", "")).upper(), str(row.get("direction", ""))) for row in opportunities}
    changed = 0
    symbols = scanned_symbols or {symbol for symbol, _ in active_keys}
    if symbols:
        existing_rows = (
            db.query(ArbitrageOpportunity)
            .filter(ArbitrageOpportunity.symbol.in_(symbols), ArbitrageOpportunity.status.in_(active_statuses))
            .all()
        )
    else:
        existing_rows = db.query(ArbitrageOpportunity).filter(ArbitrageOpportunity.status.in_(active_statuses)).all()
    existing_by_key = {(row.symbol.upper(), row.direction): row for row in existing_rows}

    for payload in opportunities:
        key = (str(payload["symbol"]).upper(), str(payload["direction"]))
        current = existing_by_key.get(key)
        if not current:
            current = ArbitrageOpportunity(symbol=payload["symbol"], direction=payload["direction"])
            db.add(current)
            changed += 1
        elif current.status == "executing":
            ids_by_key[key] = current.id
            continue
        before = _opportunity_signature(current)
        current.notional = payload["notional"]
        current.quantity = payload["quantity"]
        current.mt5_quantity = payload["mt5_quantity"]
        current.hyperliquid_quantity = payload["hyperliquid_quantity"]
        current.notional_currency = payload["notional_currency"]
        current.fx_rate_to_usd = payload["fx_rate_to_usd"]
        current.gross_spread = payload["gross_spread"]
        current.trigger_hyperliquid_bid = payload["hyperliquid_bid"]
        current.trigger_hyperliquid_ask = payload["hyperliquid_ask"]
        current.trigger_mt5_bid = payload["mt5_bid"]
        current.trigger_mt5_ask = payload["mt5_ask"]
        current.unit_cost = payload["unit_cost"]
        current.unit_net_profit = payload["unit_net_profit"]
        current.total_cost = payload["total_cost"]
        current.net_profit = payload["net_profit"]
        current.annualized_return = payload["annualized_return"]
        current.entry_threshold = payload["entry_threshold"]
        current.exit_target = payload["exit_target"]
        current.overheat_threshold = payload["overheat_threshold"]
        current.signal_sample_count = payload["signal_sample_count"]
        current.status = payload["status"]
        current.reject_reason = payload.get("reason", "")
        if current.id is None:
            db.flush()
        if _opportunity_signature(current) != before:
            changed += 1
        ids_by_key[key] = current.id

    for key, row in existing_by_key.items():
        if key in active_keys or row.status == "executing":
            continue
        row.status = "rejected"
        row.reject_reason = "价差回落，不再满足候选条件"
        changed += 1
    return changed


def _opportunity_signature(row: ArbitrageOpportunity) -> tuple:
    return (
        row.status,
        row.gross_spread,
        row.unit_cost,
        row.unit_net_profit,
        row.total_cost,
        row.net_profit,
        row.entry_threshold,
        row.exit_target,
        row.overheat_threshold,
        row.signal_sample_count,
        row.reject_reason,
    )


def _best_current_payload(payloads: list[dict]) -> dict:
    if not payloads:
        raise ValueError("缺少双向价差结果")
    status_rank = {"executable": 3, "candidate": 2, "rejected": 1}
    return max(payloads, key=lambda row: (status_rank.get(str(row.get("status")), 0), float(row.get("net_profit") or 0.0)))


def _update_scan_state_store(spread_rows: list[dict], opportunity_rows: list[dict], direction_rows: list[dict]) -> None:
    if not spread_rows and not opportunity_rows and not direction_rows:
        scan_state_store.update([], [])
        return
    spreads = sorted((_spread_state_dict(row) for row in spread_rows), key=lambda row: str(row.get("symbol", "")))
    opportunities = sorted(
        (_model_dict(row) for row in opportunity_rows),
        key=lambda row: row.get("updated_at") or row.get("created_at") or datetime.min,
        reverse=True,
    )[:50]
    directions = sorted(
        (_spread_state_dict(row) for row in direction_rows),
        key=lambda row: (str(row.get("symbol", "")), str(row.get("direction", ""))),
    )
    scan_state_store.update(
        spreads,
        opportunities,
        directions,
    )


def _spread_state_dict(row) -> dict:
    data = _model_dict(row)
    data.update(_scan_timings.get(str(data.get("symbol", "")).upper(), {}))
    return data


def _model_dict(row) -> dict:
    if isinstance(row, dict):
        return dict(row)
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}
