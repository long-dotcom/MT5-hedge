from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from threading import Thread
from typing import Callable

from loguru import logger

from app.config.settings import Settings, get_settings
from app.execution.nautilus_hyperliquid import hyperliquid_instrument_id
from app.market.quotes import QuoteCache, quote_cache


FAST_L2BOOK_OVERRIDE_MS = 2_000


@dataclass(frozen=True)
class NautilusMarketSymbol:
    internal_symbol: str
    hyperliquid_symbol: str
    instrument_id: str


class NautilusHyperliquidMarketDataBridge:
    def __init__(
        self,
        symbols: list[NautilusMarketSymbol],
        *,
        settings: Settings | None = None,
        cache: QuoteCache | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.cache = cache or quote_cache
        self.symbols = tuple(symbols)
        self.on_error = on_error
        self._node = None
        self._node_thread: Thread | None = None
        self._strategy = None
        self._lock = Lock()

    def start(self) -> None:
        if not self.symbols:
            return
        with self._lock:
            if self._node is not None:
                return
            try:
                node, strategy = self._build_node()
                self._node = node
                self._strategy = strategy
                if not node.is_running():
                    self._node_thread = _start_node(node, "nautilus-hyperliquid-md-node")
                logger.info(f"Nautilus Hyperliquid 行情桥已启动: {len(self.symbols)} 个品种")
            except Exception as exc:
                self._node = None
                self._strategy = None
                if self.on_error:
                    self.on_error(exc)
                else:
                    raise

    def stop(self) -> None:
        with self._lock:
            node = self._node
            node_thread = self._node_thread
            self._node = None
            self._node_thread = None
            self._strategy = None
        if node is None:
            return
        try:
            node.stop()
            if node_thread and node_thread.is_alive():
                node_thread.join(timeout=5)
            node.dispose()
        except Exception as exc:
            logger.warning(f"Nautilus Hyperliquid 行情桥停止失败: {exc}")

    def is_active(self) -> bool:
        node = self._node
        if node is None:
            return False
        try:
            return bool(node.is_running()) or bool(self._node_thread and self._node_thread.is_alive())
        except Exception:
            return False

    def _build_node(self):
        try:
            from nautilus_trader.adapters.hyperliquid import HYPERLIQUID
            from nautilus_trader.adapters.hyperliquid import HyperliquidDataClientConfig
            from nautilus_trader.adapters.hyperliquid import HyperliquidLiveDataClientFactory
            from nautilus_trader.adapters.hyperliquid import HyperliquidProductType
            from nautilus_trader.config import InstrumentProviderConfig
            from nautilus_trader.config import TradingNodeConfig
            from nautilus_trader.core.nautilus_pyo3 import HyperliquidEnvironment
            from nautilus_trader.live.node import TradingNode
            from nautilus_trader.model.identifiers import TraderId
        except Exception as exc:
            raise RuntimeError("未安装 nautilus_trader 或 Hyperliquid adapter 不可导入") from exc

        environment = _nautilus_environment(HyperliquidEnvironment, self.settings.nautilus_hyperliquid_environment)
        product_types = _nautilus_product_types(HyperliquidProductType, self.settings.nautilus_hyperliquid_product_types)
        provider = InstrumentProviderConfig(load_all=True)
        config = TradingNodeConfig(
            trader_id=TraderId(f"{self.settings.nautilus_trader_id}-MD"),
            data_clients={
                HYPERLIQUID: HyperliquidDataClientConfig(
                    environment=environment,
                    instrument_provider=provider,
                    product_types=product_types,
                ),
            },
        )
        node = TradingNode(config=config)
        node.add_data_client_factory(HYPERLIQUID, HyperliquidLiveDataClientFactory)
        strategy = build_nautilus_quote_bridge_strategy(self.symbols, self.cache)
        node.trader.add_strategy(strategy)
        node.build()
        return node, strategy


def market_symbols_from_mappings(mappings) -> list[NautilusMarketSymbol]:
    symbols: list[NautilusMarketSymbol] = []
    for mapping in mappings:
        hl_symbol = str(mapping.hyperliquid_symbol or "").strip()
        if not hl_symbol:
            continue
        symbols.append(
            NautilusMarketSymbol(
                internal_symbol=str(mapping.symbol),
                hyperliquid_symbol=hl_symbol,
                instrument_id=hyperliquid_instrument_id(hl_symbol),
            )
        )
    return symbols


def build_nautilus_quote_bridge_strategy(symbols: tuple[NautilusMarketSymbol, ...], cache: QuoteCache):
    try:
        from nautilus_trader.adapters.hyperliquid import HYPERLIQUID_CLIENT_ID
        from nautilus_trader.adapters.hyperliquid.data import HyperliquidAllDexsAssetCtxs
        from nautilus_trader.model.enums import BookType
        from nautilus_trader.model.data import CustomData
        from nautilus_trader.model.data import DataType
        from nautilus_trader.model.identifiers import InstrumentId
        from nautilus_trader.trading.config import StrategyConfig
        from nautilus_trader.trading.strategy import Strategy
    except Exception as exc:
        raise RuntimeError("未安装 nautilus_trader 或基础 Strategy 类型不可导入") from exc

    class NautilusQuoteBridgeStrategy(Strategy):
        def __init__(self) -> None:
            super().__init__(StrategyConfig(strategy_id="MT5-HEDGE-HL-MD-BRIDGE-001"))
            self._cache = cache
            self._by_instrument_id = {item.instrument_id: item.internal_symbol for item in symbols}
            self._book_instrument_ids = [InstrumentId.from_str(item.instrument_id) for item in symbols if ":" not in item.hyperliquid_symbol]
            self._has_dex_symbols = any(":" in item.hyperliquid_symbol for item in symbols)
            self._dex_data_type = DataType(HyperliquidAllDexsAssetCtxs) if self._has_dex_symbols else None

        def on_start(self) -> None:
            for instrument_id in self._book_instrument_ids:
                # 中文注释：managed=True 让 Nautilus 维护本地订单簿状态，扫描器只消费桥接后的顶层报价。
                self.subscribe_order_book_deltas(
                    instrument_id,
                    book_type=BookType.L2_MBP,
                    depth=0,
                    managed=True,
                )
                self.subscribe_quote_ticks(instrument_id)
            if self._dex_data_type is not None:
                # 中文注释：HIP-3/DEX 行情通过 Hyperliquid allDexsAssetCtxs 聚合流进入 Nautilus。
                self.subscribe_data(self._dex_data_type, client_id=HYPERLIQUID_CLIENT_ID)

        def on_stop(self) -> None:
            for instrument_id in self._book_instrument_ids:
                try:
                    self.unsubscribe_order_book_deltas(instrument_id)
                    self.unsubscribe_quote_ticks(instrument_id)
                except Exception:
                    pass
            if self._dex_data_type is not None:
                try:
                    self.unsubscribe_data(self._dex_data_type, client_id=HYPERLIQUID_CLIENT_ID)
                except Exception:
                    pass

        def on_data(self, data) -> None:
            payload = data.data if isinstance(data, CustomData) else data
            if isinstance(payload, HyperliquidAllDexsAssetCtxs):
                write_all_dexs_asset_ctxs_to_quote_cache(payload, self._by_instrument_id, self._cache)

        def on_order_book_deltas(self, deltas) -> None:
            write_cached_order_book_to_quote_cache(deltas, self._by_instrument_id, self.cache, self._cache)

        def on_order_book(self, order_book) -> None:
            write_order_book_to_quote_cache(order_book, self._by_instrument_id, self._cache)

        def on_order_book_depth(self, depth) -> None:
            write_depth_to_quote_cache(depth, self._by_instrument_id, self._cache)

        def on_quote_tick(self, tick) -> None:
            if write_cached_order_book_to_quote_cache(tick, self._by_instrument_id, self.cache, self._cache):
                return
            write_quote_tick_to_quote_cache(tick, self._by_instrument_id, self._cache)

    return NautilusQuoteBridgeStrategy()


def write_depth_to_quote_cache(depth, by_instrument_id: dict[str, str], cache: QuoteCache) -> bool:
    instrument_id = str(getattr(depth, "instrument_id", ""))
    symbol = by_instrument_id.get(instrument_id)
    if not symbol:
        return False
    bids = list(getattr(depth, "bids", []) or [])
    asks = list(getattr(depth, "asks", []) or [])
    if not bids or not asks:
        return False
    bid_price, bid_size = _level_price_size(bids[0])
    ask_price, ask_size = _level_price_size(asks[0])
    if bid_price <= 0 or ask_price <= 0:
        return False
    depth_notional = min(bid_price * bid_size, ask_price * ask_size)
    cache.put("hyperliquid", symbol, bid_price, ask_price, depth_notional, "nautilus_order_book_depth", _event_time(depth))
    return True


def write_cached_order_book_to_quote_cache(event, by_instrument_id: dict[str, str], nautilus_cache, cache: QuoteCache) -> bool:
    instrument_id = str(getattr(event, "instrument_id", ""))
    symbol = by_instrument_id.get(instrument_id)
    if not symbol:
        return False
    try:
        book = nautilus_cache.order_book(getattr(event, "instrument_id"))
    except Exception:
        return False
    if book is None:
        return False
    bid = _to_float(book.best_bid_price())
    ask = _to_float(book.best_ask_price())
    bid_size = _to_float(book.best_bid_size())
    ask_size = _to_float(book.best_ask_size())
    if bid <= 0 or ask <= 0:
        return False
    depth_notional = min(bid * bid_size, ask * ask_size) if bid_size > 0 and ask_size > 0 else 0.0
    cache.put("hyperliquid", symbol, bid, ask, depth_notional, "nautilus_order_book_deltas", _event_time(event))
    return True


def write_order_book_to_quote_cache(order_book, by_instrument_id: dict[str, str], cache: QuoteCache) -> bool:
    instrument_id = str(getattr(order_book, "instrument_id", ""))
    symbol = by_instrument_id.get(instrument_id)
    if not symbol:
        return False
    bid = _to_float(order_book.best_bid_price())
    ask = _to_float(order_book.best_ask_price())
    bid_size = _to_float(order_book.best_bid_size())
    ask_size = _to_float(order_book.best_ask_size())
    if bid <= 0 or ask <= 0:
        return False
    depth_notional = min(bid * bid_size, ask * ask_size) if bid_size > 0 and ask_size > 0 else 0.0
    cache.put("hyperliquid", symbol, bid, ask, depth_notional, "nautilus_order_book", None)
    return True


def write_all_dexs_asset_ctxs_to_quote_cache(payload, by_instrument_id: dict[str, str], cache: QuoteCache) -> int:
    updated = 0
    entries = list(getattr(payload, "entries", []) or [])
    for entry in entries:
        instrument_id = str(getattr(entry, "instrument_id", ""))
        symbol = by_instrument_id.get(instrument_id)
        if not symbol:
            continue
        impact = getattr(entry, "impact_prices", None)
        if impact is not None:
            bid = _to_float(getattr(impact, "bid", 0.0))
            ask = _to_float(getattr(impact, "ask", 0.0))
        else:
            mid = _to_float(getattr(entry, "mid_price", None) or getattr(entry, "mark_price", 0.0))
            bid = mid
            ask = mid
        if bid <= 0 or ask <= 0:
            continue
        if _has_fresh_fast_l2book_quote(cache, symbol):
            continue
        open_interest = _to_float(getattr(entry, "open_interest", 0.0))
        depth_notional = open_interest * ((bid + ask) / 2) if open_interest > 0 else 0.0
        cache.put("hyperliquid", symbol, bid, ask, depth_notional, "nautilus_all_dexs_asset_ctxs", _event_time(payload))
        updated += 1
    return updated


def write_quote_tick_to_quote_cache(tick, by_instrument_id: dict[str, str], cache: QuoteCache) -> bool:
    instrument_id = str(getattr(tick, "instrument_id", ""))
    symbol = by_instrument_id.get(instrument_id)
    if not symbol:
        return False
    bid = _to_float(getattr(tick, "bid_price", 0.0))
    ask = _to_float(getattr(tick, "ask_price", 0.0))
    bid_size = _to_float(getattr(tick, "bid_size", 0.0))
    ask_size = _to_float(getattr(tick, "ask_size", 0.0))
    if bid <= 0 or ask <= 0:
        return False
    depth_notional = min(bid * bid_size, ask * ask_size) if bid_size > 0 and ask_size > 0 else 0.0
    cache.put("hyperliquid", symbol, bid, ask, depth_notional, "nautilus_quote_tick", _event_time(tick))
    return True


def _level_price_size(level) -> tuple[float, float]:
    if isinstance(level, (tuple, list)) and len(level) >= 2:
        return _to_float(level[0]), _to_float(level[1])
    return _to_float(getattr(level, "price", 0.0)), _to_float(getattr(level, "size", 0.0))


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        for name in ("as_double", "as_decimal"):
            raw = getattr(value, name, None)
            if callable(raw):
                return float(raw())
    return 0.0


def _event_time(event) -> datetime | None:
    ts = getattr(event, "ts_event", 0) or getattr(event, "ts_init", 0) or 0
    try:
        value = int(ts)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime.utcfromtimestamp(value / 1_000_000_000)


def _has_fresh_fast_l2book_quote(cache: QuoteCache, symbol: str) -> bool:
    latest = cache.latest("hyperliquid", symbol)
    if latest is None or latest.source != "hyperliquid_l2Book_fast":
        return False
    age_ms = (datetime.utcnow() - latest.local_recv_ts).total_seconds() * 1000
    return age_ms <= FAST_L2BOOK_OVERRIDE_MS


def _nautilus_environment(enum_cls, value: str):
    normalized = value.strip().upper()
    return getattr(enum_cls, "MAINNET") if normalized == "MAINNET" else getattr(enum_cls, "TESTNET")


def _nautilus_product_types(enum_cls, raw_value: str):
    names = [item.strip().upper() for item in raw_value.split(",") if item.strip()]
    if not names:
        names = ["PERP", "PERP_HIP3"]
    return tuple(getattr(enum_cls, name) for name in names)


def _start_node(node, name: str) -> Thread | None:
    run = getattr(node, "run", None)
    if callable(run):
        thread = Thread(target=run, name=name, daemon=True)
        thread.start()
        return thread
    run_async = getattr(node, "run_async", None)
    if callable(run_async):
        run_async()
    return None
