import json
import time
from dataclasses import dataclass

from app.config.settings import get_settings


@dataclass
class FxRate:
    currency: str
    rate_to_usd: float
    source: str


_cache: dict[str, tuple[float, FxRate]] = {}


def fx_to_usd(currency: str, ttl_seconds: int = 5) -> FxRate:
    normalized = (currency or "USD").upper()
    if normalized in {"USD", "USDC"}:
        return FxRate(normalized, 1.0, "identity")
    now = time.time()
    cached = _cache.get(normalized)
    if cached and now - cached[0] < ttl_seconds:
        return cached[1]
    rate = _mt5_fx_to_usd(normalized)
    if rate:
        result = FxRate(normalized, rate, "mt5_tick")
        _cache[normalized] = (now, result)
        return result
    fallback = _fallback_rates().get(normalized)
    if fallback:
        result = FxRate(normalized, float(fallback), "fallback")
        _cache[normalized] = (now, result)
        return result
    raise ValueError(f"缺少 {normalized}->USD 汇率")


def _mt5_fx_to_usd(currency: str) -> float | None:
    try:
        import MetaTrader5 as mt5  # type: ignore
    except Exception:
        return None
    if not mt5.initialize():
        return None
    direct = f"{currency}USD"
    inverse = f"USD{currency}"
    for symbol, invert in ((direct, False), (inverse, True)):
        if not mt5.symbol_select(symbol, True):
            continue
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            continue
        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else max(bid, ask)
        if mid <= 0:
            continue
        return 1 / mid if invert else mid
    return None


def _fallback_rates() -> dict[str, float]:
    try:
        data = json.loads(get_settings().fx_fallback_rates or "{}")
    except Exception:
        return {}
    return {str(key).upper(): float(value) for key, value in data.items()}
