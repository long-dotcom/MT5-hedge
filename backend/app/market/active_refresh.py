import json
from datetime import datetime
from urllib import request

from app.adapters.mt5 import _initialize_mt5
from app.config.settings import get_settings
from app.market.orderbook import order_book_cache, parse_l2_levels
from app.market.quotes import quote_cache


def refresh_execution_quotes(mapping, *, refresh_leg_b: bool = True, refresh_mt5: bool | None = None) -> list[str]:
    # refresh_mt5 is a deprecated alias for refresh_leg_b
    if refresh_mt5 is not None:
        refresh_leg_b = refresh_mt5
    refreshed: list[str] = []
    if _refresh_leg_a_quote(mapping):
        refreshed.append("leg_a")
    if refresh_leg_b and _refresh_leg_b_quote(mapping):
        refreshed.append("leg_b")
    return refreshed


def _refresh_leg_a_quote(mapping) -> bool:
    # 中文注释：常规行情只走 WS；执行前只留一次 l2Book HTTP 复核，避免额外请求触发 429。
    return _refresh_leg_a_l2book(mapping)


def _refresh_leg_a_l2book(mapping) -> bool:
    settings = get_settings()
    payload = json.dumps({"type": "l2Book", "coin": mapping.leg_a_venue_symbol}).encode("utf-8")
    req = request.Request(
        settings.hyperliquid_info_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        levels = data.get("levels") if isinstance(data, dict) else data
        _write_leg_a_levels(mapping.symbol, levels, "hyperliquid_http_l2Book_execution_refresh")
        return True
    except Exception:
        return False


def _write_leg_a_levels(symbol: str, levels, source: str) -> None:
    bids, asks = parse_l2_levels(levels)
    if not bids or not asks:
        raise ValueError("l2Book levels 为空")
    bid, bid_size = bids[0]
    ask, ask_size = asks[0]
    depth_notional = min(bid * bid_size, ask * ask_size)
    order_book_cache.put("hyperliquid", symbol, bids, asks, source)
    quote_cache.put("hyperliquid", symbol, bid, ask, depth_notional, source)


def _refresh_leg_b_quote(mapping) -> bool:
    try:
        import MetaTrader5 as mt5  # type: ignore
    except Exception:
        return False
    settings = get_settings()
    if not _initialize_mt5(mt5, settings):
        return False
    try:
        if not mt5.symbol_select(mapping.mt5_symbol, True):
            return False
        tick = mt5.symbol_info_tick(mapping.mt5_symbol)
        if not tick:
            return False
        exchange_ts = datetime.utcfromtimestamp(getattr(tick, "time_msc", 0) / 1000) if getattr(tick, "time_msc", 0) else None
        quote_cache.put("mt5", mapping.symbol, tick.bid, tick.ask, 0.0, "mt5_symbol_info_tick_execution_refresh", exchange_ts)
        return True
    except Exception:
        return False
