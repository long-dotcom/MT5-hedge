import hashlib
import random
import time
from datetime import datetime
from typing import Callable

from app.adapters.base import Account, AdapterOrder, AdapterOrderResult, Ticker
from app.config.settings import get_settings
from app.market.orderbook import order_book_cache, simulate_market_fill
from app.market.quotes import quote_cache
from app.strategy.live_costs import HyperliquidCostInputs, hyperliquid_cost_inputs


class PaperAdapter:
    def __init__(self, platform: str, price_bias_bps: float = 0.0, fee_rate_provider: Callable[[str], HyperliquidCostInputs] | None = None) -> None:
        self.platform = platform
        self.price_bias_bps = price_bias_bps
        self._fee_rate_provider = fee_rate_provider or hyperliquid_cost_inputs
        self._orders: dict[str, AdapterOrderResult] = {}

    def get_symbols(self) -> list[str]:
        return ["BTC", "ETH", "SOL"]

    def get_account(self) -> Account:
        return Account(
            platform=self.platform,
            equity=50_000.0,
            available_balance=35_000.0,
            margin_used=5_000.0,
            margin_ratio=0.82,
        )

    def get_positions(self) -> list[dict]:
        return []

    def get_ticker(self, symbol: str) -> Ticker:
        base = {"BTC": 65000.0, "ETH": 3400.0, "SOL": 145.0}.get(symbol.upper().replace("USD", ""), 100.0)
        seed = int(hashlib.sha256(f"{self.platform}:{symbol}:{datetime.utcnow().minute}".encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        drift_bps = rng.uniform(-10, 10) + self.price_bias_bps
        mid = base * (1 + drift_bps / 10_000)
        spread = mid * (4 + rng.uniform(0, 4)) / 10_000
        return Ticker(
            symbol=symbol,
            bid=round(mid - spread / 2, 4),
            ask=round(mid + spread / 2, 4),
            depth_notional=100_000.0,
            timestamp=datetime.utcnow(),
        )

    def get_orderbook(self, symbol: str, depth: int = 5) -> dict:
        ticker = self.get_ticker(symbol)
        return {
            "bids": [[ticker.bid, ticker.depth_notional / depth]],
            "asks": [[ticker.ask, ticker.depth_notional / depth]],
        }

    def place_order(self, order: AdapterOrder) -> AdapterOrderResult:
        if order.paper_latency_ms > 0:
            time.sleep(order.paper_latency_ms / 1000)
        ticker = self._execution_ticker(order.symbol)
        if order.order_type == "limit" and order.post_only:
            marketable = (order.side.lower() == "buy" and order.price is not None and order.price >= ticker.ask) or (
                order.side.lower() == "sell" and order.price is not None and order.price <= ticker.bid
            )
            if marketable:
                return AdapterOrderResult(False, "", "rejected", 0.0, 0.0, 0.0, "post-only 价格会吃单")
            # 中文注释：Paper 模拟 maker 用确定性规则，TTL 足够时成交，否则保持未成交。
            if order.ttl_seconds < 1:
                return AdapterOrderResult(False, "", "unfilled", 0.0, 0.0, 0.0, "maker 挂单超时未成交")
            price = order.price or (ticker.bid if order.side.lower() == "buy" else ticker.ask)
        else:
            if self.platform == "hyperliquid":
                book = order_book_cache.latest(self.platform, order.symbol)
                if book:
                    fill = simulate_market_fill(book, order.side, order.quantity)
                    if not fill.enough_liquidity:
                        return AdapterOrderResult(False, "", "unfilled", fill.filled_quantity, fill.average_price, 0.0, f"L2 深度不足: 目标 {order.quantity:.8f}，可成交 {fill.filled_quantity:.8f}")
                    price = fill.average_price
                else:
                    price = ticker.ask if order.side.lower() == "buy" else ticker.bid
            else:
                price = ticker.ask if order.side.lower() == "buy" else ticker.bid
        fee = abs(order.quantity * price) * self._fee_rate(order)
        external_id = f"paper-{self.platform}-{len(self._orders) + 1}"
        result = AdapterOrderResult(
            success=True,
            external_order_id=external_id,
            status="filled",
            filled_quantity=order.quantity,
            average_price=price,
            fee=fee,
        )
        self._orders[external_id] = result
        return result

    def _execution_ticker(self, symbol: str) -> Ticker:
        quote = quote_cache.latest(self.platform, symbol)
        if quote:
            return Ticker(symbol=symbol, bid=quote.bid, ask=quote.ask, depth_notional=quote.depth_notional, timestamp=quote.local_recv_ts)
        return self.get_ticker(symbol)

    def _fee_rate(self, order: AdapterOrder) -> float:
        if self.platform != "hyperliquid":
            return 0.0
        symbol = order.venue_symbol or order.symbol
        if ":" not in symbol:
            settings = get_settings()
            return settings.hyperliquid_default_maker_fee_rate if _is_maker_order(order) else settings.hyperliquid_default_taker_fee_rate
        try:
            costs = self._fee_rate_provider(symbol)
            return costs.maker_fee_rate if _is_maker_order(order) else costs.taker_fee_rate
        except Exception:
            settings = get_settings()
            fallback = settings.hyperliquid_default_maker_fee_rate if _is_maker_order(order) else settings.hyperliquid_default_taker_fee_rate
            return fallback * 0.2 if symbol.startswith("xyz:") else fallback

    def cancel_order(self, order_id: str) -> bool:
        return order_id in self._orders

    def get_order(self, order_id: str) -> dict:
        result = self._orders.get(order_id)
        return result.__dict__ if result else {"status": "not_found"}

    def get_trades(self, order_id: str) -> list[dict]:
        result = self._orders.get(order_id)
        if not result:
            return []
        return [{"order_id": order_id, "quantity": result.filled_quantity, "price": result.average_price, "fee": result.fee}]


def _is_maker_order(order: AdapterOrder) -> bool:
    return order.order_type == "limit" and bool(order.post_only)
