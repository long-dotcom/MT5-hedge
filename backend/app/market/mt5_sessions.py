from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
import time as monotonic_time
from typing import Any

from app.config.settings import get_settings
from app.db.models import SymbolMapping
from app.market.mt5_schedule import LocalScheduleState, local_schedule_state


@dataclass(frozen=True)
class MT5SessionState:
    symbol: str
    status: str
    reason: str
    can_quote: bool
    can_open_long: bool
    can_open_short: bool
    can_close_long: bool
    can_close_short: bool
    seconds_to_open: int | None = None
    seconds_to_close: int | None = None
    trade_mode: str = "unknown"
    session_source: str = "fallback"
    mt5_leg: str = "b"

    @property
    def can_open_any(self) -> bool:
        return self.can_open_long or self.can_open_short

    @property
    def can_close_any(self) -> bool:
        return self.can_close_long or self.can_close_short


_session_cache: dict[int, tuple[float, MT5SessionState]] = {}


def _mt5_leg(mapping: SymbolMapping) -> str:
    if str(getattr(mapping, "leg_a_venue", "") or "").strip().lower() == "mt5":
        return "a"
    return "b"


def _direction_is_mt5_long(direction: str, mt5_leg: str = "b") -> bool:
    if direction == "long_mt5_short_hyperliquid":
        return True
    if direction == "long_hyperliquid_short_mt5":
        return False
    if direction == "long_leg_a_short_leg_b":
        return mt5_leg == "a"
    if direction == "long_leg_b_short_leg_a":
        return mt5_leg == "b"
    return False


def mt5_session_state(mapping: SymbolMapping, now: datetime | None = None) -> MT5SessionState:
    current = now or datetime.now()
    mt5_leg = _mt5_leg(mapping)
    local_state = local_schedule_state(mapping, now)
    if local_state and local_state.status != "normal_trade":
        return _from_local_schedule(local_state, mt5_leg)
    settings = get_settings()
    if settings.quote_source_mode != "live":
        return MT5SessionState(
            symbol=mapping.symbol,
            status="normal_trade",
            reason="paper 模式默认可交易",
            can_quote=True,
            can_open_long=True,
            can_open_short=True,
            can_close_long=True,
            can_close_short=True,
            trade_mode="paper",
            session_source="paper",
            mt5_leg=mt5_leg,
        )
    cache_key = mapping.id or hash(mapping.mt5_symbol)
    cached = _session_cache.get(cache_key)
    now_monotonic = monotonic_time.time()
    if cached and now_monotonic - cached[0] < settings.mt5_session_cache_ttl_seconds:
        return cached[1]
    try:
        import MetaTrader5 as mt5  # type: ignore
    except Exception as exc:
        return _fallback_closed(mapping, f"MetaTrader5 包不可用: {exc}")

    if not mt5.initialize():
        return _fallback_closed(mapping, f"MT5 初始化失败: {mt5.last_error()}")

    try:
        mt5.symbol_select(mapping.mt5_symbol, True)
        info = mt5.symbol_info(mapping.mt5_symbol)
        if not info:
            return _remember_session(cache_key, now_monotonic, _fallback_closed(mapping, "MT5 品种不可见或不存在"))

        if not hasattr(mt5, "symbol_info_session_trade") or not hasattr(mt5, "symbol_info_session_quote"):
            return _remember_session(cache_key, now_monotonic, _fallback_from_tick(mt5, mapping, info, current))

        trade_windows = _read_sessions(mt5, mapping.mt5_symbol, current, "trade")
        quote_windows = _read_sessions(mt5, mapping.mt5_symbol, current, "quote")
        in_trade, seconds_to_close, seconds_to_open = _window_state(trade_windows, current)
        in_quote, _, quote_seconds_to_open = _window_state(quote_windows or trade_windows, current)
        trade_mode = _trade_mode_name(mt5, int(getattr(info, "trade_mode", -1)))
        mode_permissions = _permissions_from_trade_mode(mt5, trade_mode, int(getattr(info, "trade_mode", -1)))

        if not in_quote:
            return _remember_session(cache_key, now_monotonic, MT5SessionState(
                symbol=mapping.symbol,
                status="closed",
                reason="MT5 当前不在报价时段",
                can_quote=False,
                can_open_long=False,
                can_open_short=False,
                can_close_long=False,
                can_close_short=False,
                seconds_to_open=quote_seconds_to_open or seconds_to_open,
                seconds_to_close=seconds_to_close,
                trade_mode=trade_mode,
                session_source="mt5_session",
                mt5_leg=mt5_leg,
            ))

        if not in_trade:
            return _remember_session(cache_key, now_monotonic, MT5SessionState(
                symbol=mapping.symbol,
                status="quote_only",
                reason="MT5 当前仅有报价或不在交易时段",
                can_quote=True,
                can_open_long=False,
                can_open_short=False,
                can_close_long=False,
                can_close_short=False,
                seconds_to_open=seconds_to_open,
                seconds_to_close=seconds_to_close,
                trade_mode=trade_mode,
                session_source="mt5_session",
                mt5_leg=mt5_leg,
            ))

        can_open_long, can_open_short, can_close_long, can_close_short = mode_permissions
        status = "normal_trade"
        reason = "MT5 当前处于正常交易时段"

        if trade_mode == "close_only":
            status = "reduce_only"
            reason = "MT5 当前只允许平仓"
        elif not can_open_long and not can_open_short:
            status = "reduce_only"
            reason = "MT5 当前不允许新开仓"

        if seconds_to_open is not None and seconds_to_open <= mapping.mt5_post_open_cooldown_minutes * 60:
            status = "post_open_cooldown"
            reason = "MT5 刚开盘，等待点差和流动性恢复"
            can_open_long = False
            can_open_short = False
        if seconds_to_close is not None and seconds_to_close <= mapping.mt5_pre_close_no_open_minutes * 60:
            status = "pre_close_no_open"
            reason = "MT5 临近休市，禁止新开仓但允许平仓"
            can_open_long = False
            can_open_short = False

        return _remember_session(cache_key, now_monotonic, MT5SessionState(
            symbol=mapping.symbol,
            status=status,
            reason=reason,
            can_quote=True,
            can_open_long=can_open_long,
            can_open_short=can_open_short,
            can_close_long=can_close_long,
            can_close_short=can_close_short,
            seconds_to_open=seconds_to_open,
            seconds_to_close=seconds_to_close,
            trade_mode=trade_mode,
            session_source="mt5_session",
            mt5_leg=mt5_leg,
        ))
    except Exception as exc:
        return _remember_session(cache_key, now_monotonic, _fallback_closed(mapping, f"MT5 交易时段读取失败: {exc}"))


def mt5_action_allowed(state: MT5SessionState, direction: str, action: str) -> tuple[bool, str]:
    mt5_long = _direction_is_mt5_long(direction, state.mt5_leg)
    if action == "open":
        allowed = state.can_open_long if mt5_long else state.can_open_short
        if allowed:
            return True, ""
        return False, f"MT5 当前不允许该方向新开仓: {state.status}，{state.reason}"
    if action == "close":
        allowed = state.can_close_long if mt5_long else state.can_close_short
        if allowed:
            return True, ""
        return False, f"MT5 当前不允许该方向平仓: {state.status}，{state.reason}"
    return False, "未知 MT5 动作"


def as_session_dict(state: MT5SessionState) -> dict[str, Any]:
    return {
        "symbol": state.symbol,
        "status": state.status,
        "reason": state.reason,
        "can_quote": state.can_quote,
        "can_open_long": state.can_open_long,
        "can_open_short": state.can_open_short,
        "can_close_long": state.can_close_long,
        "can_close_short": state.can_close_short,
        "seconds_to_open": state.seconds_to_open,
        "seconds_to_close": state.seconds_to_close,
        "trade_mode": state.trade_mode,
        "session_source": state.session_source,
        "mt5_leg": state.mt5_leg,
    }


def _from_local_schedule(state: LocalScheduleState, mt5_leg: str = "b") -> MT5SessionState:
    return MT5SessionState(
        symbol=state.symbol,
        status=state.status,
        reason=state.reason,
        can_quote=state.can_quote,
        can_open_long=state.can_open_long,
        can_open_short=state.can_open_short,
        can_close_long=state.can_close_long,
        can_close_short=state.can_close_short,
        seconds_to_open=state.seconds_to_open,
        seconds_to_close=state.seconds_to_close,
        trade_mode=state.status,
        session_source=state.source,
        mt5_leg=mt5_leg,
    )


def _read_sessions(mt5: Any, symbol: str, current: datetime, kind: str) -> list[tuple[datetime, datetime]]:
    weekday = current.isoweekday() % 7
    reader = mt5.symbol_info_session_trade if kind == "trade" else mt5.symbol_info_session_quote
    windows: list[tuple[datetime, datetime]] = []
    index = 0
    while index < 32:
        session = reader(symbol, weekday, index)
        if not session:
            break
        start_raw = getattr(session, "from", None) or getattr(session, "from_", None) or session[0]
        end_raw = getattr(session, "to", None) or session[1]
        start_time = _to_time(start_raw)
        end_time = _to_time(end_raw)
        start_dt = datetime.combine(current.date(), start_time)
        end_dt = datetime.combine(current.date(), end_time)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        windows.append((start_dt, end_dt))
        index += 1
    return windows


def _fallback_from_tick(mt5: Any, mapping: SymbolMapping, info: Any, current: datetime) -> MT5SessionState:
    settings = get_settings()
    mt5_leg = _mt5_leg(mapping)
    tick = mt5.symbol_info_tick(mapping.mt5_symbol)
    trade_mode = _trade_mode_name(mt5, int(getattr(info, "trade_mode", -1)))
    can_open_long, can_open_short, can_close_long, can_close_short = _permissions_from_trade_mode(mt5, trade_mode, int(getattr(info, "trade_mode", -1)))
    if not tick or float(getattr(tick, "bid", 0.0) or 0.0) <= 0 or float(getattr(tick, "ask", 0.0) or 0.0) <= 0:
        return MT5SessionState(
            symbol=mapping.symbol,
            status="closed",
            reason="当前 MT5 tick 不可用，按不可交易处理",
            can_quote=False,
            can_open_long=False,
            can_open_short=False,
            can_close_long=False,
            can_close_short=False,
            trade_mode=trade_mode,
            session_source="mt5_tick_trade_mode_fallback",
            mt5_leg=mt5_leg,
        )
    tick_seconds = getattr(tick, "time_msc", 0)
    tick_time = datetime.fromtimestamp(tick_seconds / 1000) if tick_seconds else datetime.fromtimestamp(getattr(tick, "time", 0))
    tick_age = (current - tick_time).total_seconds()
    if tick_age > settings.mt5_session_tick_stale_seconds:
        return MT5SessionState(
            symbol=mapping.symbol,
            status="closed",
            reason=f"MT5 tick 已 {int(tick_age)} 秒未更新，按休市或不可交易处理",
            can_quote=False,
            can_open_long=False,
            can_open_short=False,
            can_close_long=False,
            can_close_short=False,
            trade_mode=trade_mode,
            session_source="mt5_tick_trade_mode_fallback",
            mt5_leg=mt5_leg,
        )
    status = "normal_trade"
    reason = "MT5 Python 包不支持 session API，使用 tick 新鲜度和 trade_mode 兜底"
    if trade_mode == "close_only":
        status = "reduce_only"
        reason = "MT5 当前 trade_mode 为只平仓"
    elif not can_open_long and not can_open_short:
        status = "reduce_only"
        reason = "MT5 当前 trade_mode 不允许新开仓"
    return MT5SessionState(
        symbol=mapping.symbol,
        status=status,
        reason=reason,
        can_quote=True,
        can_open_long=can_open_long,
        can_open_short=can_open_short,
        can_close_long=can_close_long,
        can_close_short=can_close_short,
        trade_mode=trade_mode,
        session_source="mt5_tick_trade_mode_fallback",
        mt5_leg=mt5_leg,
    )


def _to_time(value: Any) -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, datetime):
        return value.time()
    if isinstance(value, int):
        return (datetime.min + timedelta(seconds=value)).time()
    text = str(value)
    if ":" in text:
        parts = [int(part) for part in text.split(":")[:3]]
        while len(parts) < 3:
            parts.append(0)
        return time(parts[0], parts[1], parts[2])
    return time(0, 0)


def _window_state(windows: list[tuple[datetime, datetime]], current: datetime) -> tuple[bool, int | None, int | None]:
    if not windows:
        return True, None, None
    next_open: int | None = None
    for start, end in windows:
        if start <= current <= end:
            return True, int((end - current).total_seconds()), int((current - start).total_seconds())
        if current < start:
            seconds = int((start - current).total_seconds())
            next_open = seconds if next_open is None else min(next_open, seconds)
    if next_open is None:
        first_start = min(start for start, _ in windows) + timedelta(days=1)
        next_open = int((first_start - current).total_seconds())
    return False, None, next_open


def _trade_mode_name(mt5: Any, trade_mode: int) -> str:
    names = {
        getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", -100): "disabled",
        getattr(mt5, "SYMBOL_TRADE_MODE_LONGONLY", -101): "long_only",
        getattr(mt5, "SYMBOL_TRADE_MODE_SHORTONLY", -102): "short_only",
        getattr(mt5, "SYMBOL_TRADE_MODE_CLOSEONLY", -103): "close_only",
        getattr(mt5, "SYMBOL_TRADE_MODE_FULL", -104): "full",
    }
    return names.get(trade_mode, f"unknown:{trade_mode}")


def _permissions_from_trade_mode(mt5: Any, trade_mode_name: str, trade_mode: int) -> tuple[bool, bool, bool, bool]:
    if trade_mode_name == "disabled":
        return False, False, False, False
    if trade_mode_name == "close_only":
        return False, False, True, True
    if trade_mode_name == "long_only":
        return True, False, True, True
    if trade_mode_name == "short_only":
        return False, True, True, True
    if trade_mode_name == "full":
        return True, True, True, True
    if trade_mode >= 0:
        return True, True, True, True
    return False, False, False, False


def _fallback_closed(mapping: SymbolMapping, reason: str) -> MT5SessionState:
    return MT5SessionState(
        symbol=mapping.symbol,
        status="unknown",
        reason=reason,
        can_quote=False,
        can_open_long=False,
        can_open_short=False,
        can_close_long=False,
        can_close_short=False,
        trade_mode="unknown",
        session_source="fallback",
        mt5_leg=_mt5_leg(mapping),
    )


def _remember_session(cache_key: int, cached_at: float, state: MT5SessionState) -> MT5SessionState:
    _session_cache[cache_key] = (cached_at, state)
    return state
