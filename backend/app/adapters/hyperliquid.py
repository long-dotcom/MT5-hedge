import json
from decimal import Decimal, ROUND_CEILING
from urllib import request

from app.adapters.base import AdapterOrder, AdapterOrderResult
from app.adapters.paper import PaperAdapter
from app.config.settings import HYPERLIQUID_MAINNET_API_URL, get_settings, hyperliquid_execution_info_url


class HyperliquidAdapter(PaperAdapter):
    def __init__(self, live: bool = False) -> None:
        super().__init__("hyperliquid", price_bias_bps=-20.0)
        self.live = live
        self.paper_price_probe = False
        self.settings = get_settings()

    def place_order(self, order: AdapterOrder) -> AdapterOrderResult:
        if not self.live:
            return super().place_order(order)
        if self.paper_price_probe:
            return self._place_paper_live_probe(order)
        # 中文注释：实盘默认关闭，避免误触发真实资金交易。
        if not self.settings.hyperliquid_account_address:
            return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, "Hyperliquid 凭证未配置")
        return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, "首版未启用 Hyperliquid 真实下单 SDK 调用")

    def _place_paper_live_probe(self, order: AdapterOrder) -> AdapterOrderResult:
        if not self.settings.hyperliquid_account_address or not getattr(self.settings, "hyperliquid_secret_key", ""):
            return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, "Hyperliquid paper-live 探针需要账户地址和 API 私钥")
        venue_symbol = order.venue_symbol or order.symbol
        try:
            exchange = _load_hyperliquid_exchange(self.settings)
            probe_quantity = self._probe_quantity(venue_symbol)
            is_buy = order.side.lower() == "buy"
            slippage = float(getattr(self.settings, "hyperliquid_paper_live_slippage", 0.01) or 0.01)
            if order.order_type == "limit":
                if order.price is None:
                    return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, "Hyperliquid limit 探针缺少价格")
                response = exchange.order(
                    venue_symbol,
                    is_buy,
                    probe_quantity,
                    order.price,
                    {"limit": {"tif": "Alo" if order.post_only else "Gtc"}},
                    reduce_only=order.reduce_only,
                )
            elif order.reduce_only:
                response = exchange.market_close(venue_symbol, probe_quantity, None, slippage)
            else:
                response = exchange.market_open(venue_symbol, is_buy, probe_quantity, None, slippage)
            if response is None and order.reduce_only:
                return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, f"Hyperliquid reduce-only 探针没有可平仓真实仓位: {venue_symbol}")
            parsed = _parse_order_response(response)
            if not parsed["success"]:
                return AdapterOrderResult(False, parsed["external_order_id"], parsed["status"], 0.0, 0.0, 0.0, parsed["message"])
            fee = abs(order.quantity * parsed["average_price"]) * self._fee_rate(order)
            return AdapterOrderResult(
                True,
                parsed["external_order_id"],
                parsed["status"],
                order.quantity,
                parsed["average_price"],
                fee,
                f"paper-live 探针真实成交量 {probe_quantity:g}，账本成交量 {order.quantity:g}",
            )
        except Exception as exc:
            return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, f"Hyperliquid paper-live 探针失败: {exc}")

    def _probe_quantity(self, symbol: str) -> float:
        dex_payload = {"dex": symbol.split(":", 1)[0]} if ":" in symbol else {}
        meta = self._post_info({"type": "meta", **dex_payload})
        mids = self._post_info({"type": "allMids", **dex_payload})
        decimals = _sz_decimals(meta, symbol)
        step = Decimal(1).scaleb(-decimals)
        mid = Decimal(str(mids.get(symbol) or mids.get(symbol.split(":", 1)[-1]) or 0))
        min_by_notional = Decimal(0)
        min_notional = Decimal(str(getattr(self.settings, "hyperliquid_default_min_notional", 10.0) or 10.0))
        if mid > 0:
            min_by_notional = min_notional / mid
        configured_min = Decimal(str(_configured_hyperliquid_min_base_size(symbol) or 0.0))
        raw_quantity = max(step, min_by_notional, configured_min)
        units = (raw_quantity / step).to_integral_value(rounding=ROUND_CEILING)
        quantity = units * step
        return float(quantity)

    def get_positions(self, dexes: list[str] | None = None) -> list[dict]:
        if not self.live:
            return super().get_positions()
        user = self.settings.hyperliquid_account_address
        if not user:
            return []
        positions = []
        seen: set[tuple[str, str]] = set()
        dex_queries = _position_dex_queries(dexes)
        mark_prices = self._position_mark_prices(dex_queries)
        for dex in dex_queries:
            try:
                payload = {"type": "clearinghouseState", "user": user}
                if dex:
                    payload["dex"] = dex
                data = self._post_info(payload)
            except Exception:
                continue
            for position in _positions_from_clearinghouse_state(data, mark_prices):
                key = (position["symbol"], position["side"])
                if key in seen:
                    continue
                positions.append(position)
                seen.add(key)
        return positions

    def _position_mark_prices(self, dexes: list[str]) -> dict[str, float]:
        mark_prices: dict[str, float] = {}
        for dex in dexes:
            try:
                payload = {"type": "allMids"}
                if dex:
                    payload["dex"] = dex
                data = self._post_info(payload)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            for symbol, value in data.items():
                try:
                    mark_prices[str(symbol)] = float(value)
                except (TypeError, ValueError):
                    continue
        return mark_prices

    def _post_info(self, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(hyperliquid_execution_info_url(self.settings), data=data, headers={"Content-Type": "application/json"}, method="POST")
        with request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))


def _optional_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _position_dex_queries(dexes: list[str] | None) -> list[str]:
    values = [""]
    for dex in dexes or []:
        value = str(dex or "").strip()
        if value and value not in values:
            values.append(value)
    return values


def _positions_from_clearinghouse_state(data, mark_prices: dict[str, float] | None = None) -> list[dict]:
    positions = []
    for item in data.get("assetPositions", []) if isinstance(data, dict) else []:
        position = item.get("position", {}) if isinstance(item, dict) else {}
        quantity = float(position.get("szi", 0.0) or 0.0)
        if abs(quantity) <= 0:
            continue
        symbol = str(position.get("coin") or "")
        mark_price = (mark_prices or {}).get(symbol)
        if mark_price is None:
            mark_price = float(position.get("markPx", position.get("entryPx", 0.0)) or 0.0)
        positions.append(
            {
                "platform": "hyperliquid",
                "symbol": symbol,
                "side": "long" if quantity > 0 else "short",
                "quantity": abs(quantity),
                "entry_price": float(position.get("entryPx", 0.0) or 0.0),
                "mark_price": mark_price,
                "unrealized_pnl": float(position.get("unrealizedPnl", 0.0) or 0.0),
                "margin_used": float(position.get("marginUsed", 0.0) or 0.0),
                "liquidation_price": _optional_float(position.get("liquidationPx")),
            }
        )
    return positions


def _load_hyperliquid_exchange(settings):
    try:
        import eth_account
        from hyperliquid.exchange import Exchange
    except Exception as exc:
        raise RuntimeError(f"缺少 hyperliquid-python-sdk 或 eth_account: {exc}") from exc
    wallet = eth_account.Account.from_key(settings.hyperliquid_secret_key)
    return Exchange(wallet, base_url=_hyperliquid_api_url(settings), account_address=settings.hyperliquid_account_address, perp_dexs=_configured_perp_dexs())


def _hyperliquid_api_url(settings) -> str:
    info_url = str(getattr(settings, "hyperliquid_info_url", "") or "")
    if info_url.endswith("/info"):
        return info_url[: -len("/info")]
    return HYPERLIQUID_MAINNET_API_URL


def _sz_decimals(meta, symbol: str) -> int:
    universe = meta.get("universe", []) if isinstance(meta, dict) else []
    candidates = {symbol, symbol.split(":", 1)[-1]}
    for asset in universe:
        if not isinstance(asset, dict):
            continue
        if str(asset.get("name") or "") in candidates:
            try:
                return max(min(int(asset.get("szDecimals", 0)), 8), 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _parse_order_response(response) -> dict:
    if response is None:
        return {"success": False, "status": "failed", "external_order_id": "", "average_price": 0.0, "message": "Hyperliquid SDK 返回空响应"}
    status = str(response.get("status") if isinstance(response, dict) else "")
    if status != "ok":
        return {"success": False, "status": "failed", "external_order_id": "", "average_price": 0.0, "message": json.dumps(response, ensure_ascii=False)}
    statuses = (((response.get("response") or {}).get("data") or {}).get("statuses") or []) if isinstance(response, dict) else []
    if not statuses:
        return {"success": False, "status": "submitted", "external_order_id": "", "average_price": 0.0, "message": "Hyperliquid 未返回成交状态"}
    first = statuses[0]
    if "filled" in first:
        filled = first["filled"]
        return {
            "success": True,
            "status": "filled",
            "external_order_id": str(filled.get("oid") or ""),
            "average_price": float(filled.get("avgPx") or 0.0),
            "message": "",
        }
    if "resting" in first:
        resting = first["resting"]
        return {"success": False, "status": "open", "external_order_id": str(resting.get("oid") or ""), "average_price": 0.0, "message": "Hyperliquid 探针订单未立即成交"}
    error = first.get("error") if isinstance(first, dict) else str(first)
    return {"success": False, "status": "failed", "external_order_id": "", "average_price": 0.0, "message": str(error)}


def _configured_hyperliquid_min_base_size(symbol: str) -> float:
    try:
        from app.db.models import SymbolMapping
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            row = db.query(SymbolMapping).filter(SymbolMapping.leg_a_venue_symbol == symbol).first()
            return float(getattr(row, "leg_a_min_base_size", 0.0) or 0.0) if row else 0.0
    except Exception:
        return 0.0


def _configured_perp_dexs() -> list[str]:
    values = [""]
    try:
        from app.db.models import SymbolMapping
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            rows = db.query(SymbolMapping.leg_a_venue_symbol).filter(SymbolMapping.leg_a_venue_symbol.like("%:%")).all()
        for (symbol,) in rows:
            dex = str(symbol or "").split(":", 1)[0].strip()
            if dex and dex not in values:
                values.append(dex)
    except Exception:
        pass
    return values
