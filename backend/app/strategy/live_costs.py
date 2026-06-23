import json
import time
from dataclasses import dataclass
from urllib import request

from loguru import logger

from app.config.settings import get_settings


@dataclass
class HyperliquidCostInputs:
    taker_fee_rate: float
    maker_fee_rate: float
    funding_rate: float
    source: str


@dataclass
class MT5CostInputs:
    commission_rate: float
    swap_cost: float
    swap_long: float
    swap_short: float
    swap_mode: int
    source: str


@dataclass
class HyperliquidMarketData:
    funding_rates: dict[str, float]
    asset_meta: dict[str, dict]


_hl_market_cache: dict[str, tuple[float, HyperliquidMarketData]] = {}
_hl_user_fee_cache: tuple[float, tuple[float, float] | None] = (0.0, None)


def hyperliquid_cost_inputs(symbol: str) -> HyperliquidCostInputs:
    taker, maker = _hyperliquid_user_fee_rates()
    market_data = _hyperliquid_market_data(symbol)
    effective_taker, effective_maker, fee_source = _hyperliquid_effective_fee_rates(symbol, taker, maker, market_data.asset_meta)
    return HyperliquidCostInputs(
        taker_fee_rate=effective_taker,
        maker_fee_rate=effective_maker,
        funding_rate=market_data.funding_rates.get(symbol, 0.00010),
        source=f"{fee_source}+metaAndAssetCtxs",
    )


def mt5_cost_inputs(mt5_symbol: str, mt5_side: str, quantity: float, holding_days: float) -> MT5CostInputs:
    settings = get_settings()
    if settings.mt5_swap_free:
        return MT5CostInputs(settings.mt5_default_commission_rate, 0.0, 0.0, 0.0, 0, "mt5_swap_free")
    try:
        import MetaTrader5 as mt5  # type: ignore
    except Exception as exc:
        logger.warning(f"MetaTrader5 包不可用，使用默认 MT5 成本: {exc}")
        return MT5CostInputs(settings.mt5_default_commission_rate, 0.0, 0.0, 0.0, 0, "mt5_default")

    if not mt5.initialize():
        logger.warning(f"MT5 initialize 失败，使用默认 MT5 成本: {mt5.last_error()}")
        return MT5CostInputs(settings.mt5_default_commission_rate, 0.0, 0.0, 0.0, 0, "mt5_default")
    mt5.symbol_select(mt5_symbol, True)
    info = mt5.symbol_info(mt5_symbol)
    if not info:
        return MT5CostInputs(settings.mt5_default_commission_rate, 0.0, 0.0, 0.0, 0, "mt5_default")
    swap_long = float(getattr(info, "swap_long", 0.0))
    swap_short = float(getattr(info, "swap_short", 0.0))
    swap_mode = int(getattr(info, "swap_mode", 0))
    point = float(getattr(info, "point", 0.0))
    contract_size = float(getattr(info, "trade_contract_size", 1.0))
    selected_swap = swap_long if mt5_side == "buy" else swap_short
    swap_cost = _estimate_mt5_swap_cost(selected_swap, swap_mode, point, contract_size, quantity, holding_days)
    return MT5CostInputs(
        commission_rate=settings.mt5_default_commission_rate,
        swap_cost=swap_cost,
        swap_long=swap_long,
        swap_short=swap_short,
        swap_mode=swap_mode,
        source="mt5_symbol_info",
    )


def _estimate_mt5_swap_cost(swap_value: float, swap_mode: int, point: float, contract_size: float, quantity: float, holding_days: float) -> float:
    # 中文注释：swap_mode=1 表示点数模式，当前券商 BTCUSD/ETHUSD 是这种模式。
    if swap_mode == 0 or holding_days <= 0:
        return 0.0
    if swap_mode == 1:
        swap_pnl = swap_value * point * contract_size * quantity * holding_days
        return -swap_pnl
    # 中文注释：其他模式在不同券商含义差异较大，先按每手金额估算；负值表示支付，正值表示收取。
    swap_pnl = swap_value * quantity * holding_days
    return -swap_pnl


def _hyperliquid_user_fee_rates() -> tuple[float, float]:
    global _hl_user_fee_cache
    settings = get_settings()
    now = time.time()
    cached_at, cached = _hl_user_fee_cache
    if cached and now - cached_at < settings.cost_cache_ttl_seconds:
        return cached
    account_address = settings.hyperliquid_account_address
    if not account_address:
        return settings.hyperliquid_default_taker_fee_rate, settings.hyperliquid_default_maker_fee_rate
    try:
        data = _post_hyperliquid_info({"type": "userFees", "user": account_address})
        taker = float(data.get("userCrossRate", settings.hyperliquid_default_taker_fee_rate))
        maker = float(data.get("userAddRate", settings.hyperliquid_default_maker_fee_rate))
        _hl_user_fee_cache = (now, (taker, maker))
        return taker, maker
    except Exception as exc:
        logger.warning(f"Hyperliquid userFees 读取失败，使用默认费率: {exc}")
        return settings.hyperliquid_default_taker_fee_rate, settings.hyperliquid_default_maker_fee_rate


def _hyperliquid_market_data(symbol: str = "") -> HyperliquidMarketData:
    global _hl_market_cache
    settings = get_settings()
    now = time.time()
    dex = symbol.split(":", 1)[0] if ":" in symbol else ""
    cached = _hl_market_cache.get(dex)
    if cached and now - cached[0] < settings.cost_cache_ttl_seconds:
        return cached[1]
    try:
        payload = {"type": "metaAndAssetCtxs"}
        if dex:
            payload["dex"] = dex
        meta, contexts = _post_hyperliquid_info(payload)
        rates: dict[str, float] = {}
        asset_meta: dict[str, dict] = {}
        for asset, context in zip(meta.get("universe", []), contexts):
            name = asset.get("name", "")
            rates[name] = float(context.get("funding", 0.0))
            asset_meta[name] = asset
        market_data = HyperliquidMarketData(rates, asset_meta)
        _hl_market_cache[dex] = (now, market_data)
        return market_data
    except Exception as exc:
        logger.warning(f"Hyperliquid funding 读取失败，使用默认 funding: {exc}")
        return HyperliquidMarketData({}, {})


def _hyperliquid_effective_fee_rates(symbol: str, taker: float, maker: float, asset_meta: dict[str, dict]) -> tuple[float, float, str]:
    if ":" not in symbol:
        return taker, maker, "hyperliquid_userFees"

    dex = symbol.split(":", 1)[0]
    meta = asset_meta.get(symbol, {})
    growth_mode = str(meta.get("growthMode", "")).lower() == "enabled"

    if dex == "xyz":
        if not meta:
            return taker * 0.2, maker * 0.2, "hyperliquid_userFees+xyz_growth_fee_multiplier_fallback"
        multiplier = 0.2 if growth_mode else 2.0
        mode = "growth" if growth_mode else "standard"
        return taker * multiplier, maker * multiplier, f"hyperliquid_userFees+xyz_{mode}_fee_multiplier"

    if growth_mode:
        return taker * 0.2, maker * 0.2, "hyperliquid_userFees+hip3_growth_conservative_fee_multiplier"
    return taker * 2.0, maker * 2.0, "hyperliquid_userFees+hip3_standard_fee_multiplier"


def _post_hyperliquid_info(payload: dict):
    settings = get_settings()
    req = request.Request(
        settings.hyperliquid_info_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))
