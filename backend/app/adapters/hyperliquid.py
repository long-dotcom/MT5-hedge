import json
from urllib import request

from app.adapters.base import AdapterOrder, AdapterOrderResult
from app.adapters.paper import PaperAdapter
from app.config.settings import get_settings, hyperliquid_execution_info_url


class HyperliquidAdapter(PaperAdapter):
    def __init__(self, live: bool = False) -> None:
        super().__init__("hyperliquid", price_bias_bps=-20.0)
        self.live = live
        self.settings = get_settings()

    def place_order(self, order: AdapterOrder) -> AdapterOrderResult:
        # 中文注释：实盘默认关闭，避免误触发真实资金交易。
        if not self.live:
            return super().place_order(order)
        if not self.settings.nautilus_hyperliquid_private_key or not self.settings.hyperliquid_account_address:
            return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, "Hyperliquid 凭证未配置")
        return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, "首版未启用 Hyperliquid 真实下单 SDK 调用")

    def get_positions(self, dexes: list[str] | None = None) -> list[dict]:
        if not self.live:
            return super().get_positions()
        user = self.settings.hyperliquid_account_address or self.settings.nautilus_hyperliquid_vault_address
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
