from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime
from datetime import timezone
from typing import Any
from urllib import request

from loguru import logger

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.mt5 import MT5Adapter
from app.config.settings import get_settings
from app.db.session import SessionLocal
from app.market.nautilus_hyperliquid import ORDER_BOOK_OVERRIDE_MS, ORDER_BOOK_SOURCES, NautilusHyperliquidMarketDataBridge, market_symbols_from_mappings
from app.market.quotes import quote_cache
from app.market.symbols import enabled_mappings


class MarketDataManager:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        settings = get_settings()
        if settings.quote_source_mode == "live":
            hyperliquid_source = settings.hyperliquid_market_data_source.strip().lower()
            hyperliquid_fallback = settings.hyperliquid_market_data_fallback.strip().lower()
            if hyperliquid_source == "nautilus":
                self._start_thread("hyperliquid-nautilus", self._hyperliquid_nautilus_loop)
                if settings.hyperliquid_l2book_fast_enabled:
                    self._start_thread("hyperliquid-fast-l2book", self._hyperliquid_fast_l2book_loop)
                if hyperliquid_fallback == "native":
                    self._start_thread("hyperliquid-http-polling", self._hyperliquid_http_polling_loop)
            else:
                self._start_thread("hyperliquid-ws", self._hyperliquid_ws_loop)
                self._start_thread("hyperliquid-http-polling", self._hyperliquid_http_polling_loop)
            self._start_thread("mt5-polling", self._mt5_polling_loop)
        else:
            self._start_thread("paper-quotes", self._paper_loop)

    def stop(self) -> None:
        self._stop.set()
        self._running = False

    def wait_until_seeded(self, timeout_seconds: float = 3.0) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            db = SessionLocal()
            try:
                symbols = [item.symbol for item in enabled_mappings(db)]
            finally:
                db.close()
            if symbols and all(quote_cache.latest("hyperliquid", symbol) and quote_cache.latest("mt5", symbol) for symbol in symbols):
                return
            time.sleep(0.05)

    def _start_thread(self, name: str, target) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _paper_loop(self) -> None:
        settings = get_settings()
        interval = max(settings.paper_quote_interval_ms, 50) / 1000
        hyperliquid = HyperliquidAdapter(live=False)
        mt5 = MT5Adapter(live=False)
        while not self._stop.is_set():
            db = SessionLocal()
            try:
                mappings = enabled_mappings(db)
            finally:
                db.close()
            for mapping in mappings:
                hl = hyperliquid.get_ticker(mapping.hyperliquid_symbol)
                mt = mt5.get_ticker(mapping.mt5_symbol)
                quote_cache.put("hyperliquid", mapping.symbol, hl.bid, hl.ask, hl.depth_notional, "paper", hl.timestamp)
                quote_cache.put("mt5", mapping.symbol, mt.bid, mt.ask, mt.depth_notional, "paper", mt.timestamp)
            time.sleep(interval)

    def _mt5_polling_loop(self) -> None:
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception as exc:
            logger.error(f"MetaTrader5 包不可用: {exc}")
            return
        settings = get_settings()
        interval = max(settings.mt5_quote_poll_interval_ms, 50) / 1000
        if not mt5.initialize():
            logger.error(f"MT5 initialize 失败: {mt5.last_error()}")
            return
        try:
            while not self._stop.is_set():
                db = SessionLocal()
                try:
                    mappings = enabled_mappings(db)
                finally:
                    db.close()
                for mapping in mappings:
                    mt5.symbol_select(mapping.mt5_symbol, True)
                    tick = mt5.symbol_info_tick(mapping.mt5_symbol)
                    if not tick:
                        continue
                    exchange_ts = datetime.utcfromtimestamp(getattr(tick, "time_msc", 0) / 1000) if getattr(tick, "time_msc", 0) else None
                    quote_cache.put("mt5", mapping.symbol, tick.bid, tick.ask, 0.0, "mt5_symbol_info_tick", exchange_ts)
                time.sleep(interval)
        finally:
            mt5.shutdown()

    def _hyperliquid_ws_loop(self) -> None:
        asyncio.run(
            self._hyperliquid_l2book_main(
                fast=get_settings().hyperliquid_l2book_fast_enabled,
                hip3_only=False,
                source="hyperliquid_l2Book_fast" if get_settings().hyperliquid_l2book_fast_enabled else "hyperliquid_l2Book",
            )
        )

    def _hyperliquid_fast_l2book_loop(self) -> None:
        asyncio.run(
            self._hyperliquid_l2book_main(
                fast=True,
                hip3_only=False,
                source="hyperliquid_l2Book_fast",
            )
        )

    def _hyperliquid_nautilus_loop(self) -> None:
        bridge: NautilusHyperliquidMarketDataBridge | None = None
        signature: tuple[tuple[str, str], ...] = ()
        while not self._stop.is_set():
            db = SessionLocal()
            try:
                mappings = enabled_mappings(db)
                next_signature = tuple(sorted((item.symbol, item.hyperliquid_symbol) for item in mappings))
                symbols = market_symbols_from_mappings(mappings)
            finally:
                db.close()
            if next_signature != signature or (bridge is not None and not bridge.is_active()):
                if bridge:
                    bridge.stop()
                signature = next_signature
                bridge = NautilusHyperliquidMarketDataBridge(
                    symbols,
                    on_error=lambda exc: logger.error(f"Nautilus Hyperliquid 行情桥启动失败: {exc}"),
                )
                bridge.start()
            time.sleep(5)
        if bridge:
            bridge.stop()

    def _hyperliquid_http_polling_loop(self) -> None:
        settings = get_settings()
        interval = max(settings.hyperliquid_http_poll_interval_ms, 300) / 1000
        while not self._stop.is_set():
            db = SessionLocal()
            try:
                mappings = enabled_mappings(db)
            finally:
                db.close()
            for mapping in mappings:
                try:
                    if self._has_fresh_hyperliquid_ws_quote(mapping.symbol):
                        continue
                    if ":" in mapping.hyperliquid_symbol:
                        self._write_hyperliquid_dex_quote(mapping.symbol, mapping.hyperliquid_symbol)
                    else:
                        payload = json.dumps({"type": "l2Book", "coin": mapping.hyperliquid_symbol}).encode("utf-8")
                        req = request.Request(
                            settings.hyperliquid_info_url,
                            data=payload,
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        )
                        with request.urlopen(req, timeout=5) as resp:
                            data = json.loads(resp.read().decode("utf-8"))
                        levels = data.get("levels") if isinstance(data, dict) else data
                        self._write_hyperliquid_levels(mapping.symbol, levels, "hyperliquid_http_l2Book")
                except Exception as exc:
                    logger.error(f"Hyperliquid HTTP 行情失败 {mapping.hyperliquid_symbol}: {exc}")
            time.sleep(interval)

    def _has_fresh_hyperliquid_ws_quote(self, symbol: str) -> bool:
        quote = quote_cache.latest("hyperliquid", symbol)
        if not quote or quote.source not in {
            "hyperliquid_l2Book",
            "hyperliquid_l2Book_fast",
            "nautilus_all_dexs_asset_ctxs",
            "nautilus_order_book",
            "nautilus_order_book_depth",
            "nautilus_order_book_deltas",
            "nautilus_quote_tick",
        }:
            return False
        age_ms = (datetime.utcnow() - quote.local_recv_ts).total_seconds() * 1000
        max_age_ms = max(get_settings().quote_stale_ms, 1000)
        if quote.source in ORDER_BOOK_SOURCES:
            max_age_ms = max(max_age_ms, ORDER_BOOK_OVERRIDE_MS)
        return age_ms <= max_age_ms

    async def _hyperliquid_l2book_main(self, *, fast: bool, hip3_only: bool, source: str) -> None:
        try:
            import websockets  # type: ignore
        except Exception as exc:
            logger.error(f"websockets 包不可用: {exc}")
            return
        settings = get_settings()
        while not self._stop.is_set():
            by_hl_symbol = self._load_hyperliquid_symbol_map(hip3_only=hip3_only)
            if not by_hl_symbol:
                await asyncio.sleep(2)
                continue
            try:
                async with websockets.connect(settings.hyperliquid_ws_url, ping_interval=20, ping_timeout=20) as ws:
                    subscribed: set[str] = set()
                    for coin in by_hl_symbol:
                        await ws.send(json.dumps({"method": "subscribe", "subscription": l2book_subscription(coin, fast=fast)}))
                        subscribed.add(coin)
                    mode = "fast" if fast else "default"
                    scope = "HIP-3" if hip3_only else "all"
                    logger.info(f"Hyperliquid l2Book WS 已订阅: {len(by_hl_symbol)} 个品种, mode={mode}, scope={scope}")
                    last_refresh = time.monotonic()
                    while not self._stop.is_set():
                        if time.monotonic() - last_refresh >= 2:
                            next_by_hl_symbol = self._load_hyperliquid_symbol_map(hip3_only=hip3_only)
                            for coin in set(next_by_hl_symbol) - subscribed:
                                await ws.send(json.dumps({"method": "subscribe", "subscription": l2book_subscription(coin, fast=fast)}))
                                subscribed.add(coin)
                                logger.info(f"Hyperliquid l2Book WS 动态订阅: {coin}")
                            removed = subscribed - set(next_by_hl_symbol)
                            for coin in removed:
                                await ws.send(json.dumps({"method": "unsubscribe", "subscription": l2book_subscription(coin, fast=fast)}))
                                subscribed.remove(coin)
                                logger.info(f"Hyperliquid l2Book WS 取消订阅: {coin}")
                            by_hl_symbol = next_by_hl_symbol
                            last_refresh = time.monotonic()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1)
                        except asyncio.TimeoutError:
                            continue
                        if self._stop.is_set():
                            break
                        self._handle_hyperliquid_message(json.loads(raw), by_hl_symbol, source)
            except Exception as exc:
                logger.error(f"Hyperliquid WS 断开，准备重连: {exc}")
                await asyncio.sleep(2)

    def _load_hyperliquid_symbol_map(self, *, hip3_only: bool) -> dict[str, str]:
        db = SessionLocal()
        try:
            mappings = enabled_mappings(db)
            return hyperliquid_symbol_map(mappings, hip3_only=hip3_only)
        finally:
            db.close()

    def _handle_hyperliquid_message(self, payload: dict[str, Any], by_hl_symbol: dict[str, str], source: str = "hyperliquid_l2Book") -> None:
        channel = payload.get("channel")
        data = payload.get("data") or {}
        if channel != "l2Book":
            return
        coin = data.get("coin")
        symbol = by_hl_symbol.get(coin)
        levels = data.get("levels") or []
        if not symbol or len(levels) < 2 or not levels[0] or not levels[1]:
            return
        self._write_hyperliquid_levels(symbol, levels, source, _exchange_time_from_hyperliquid_ms(data.get("time")))

    def _write_hyperliquid_levels(self, symbol: str, levels: Any, source: str, exchange_ts: datetime | None = None) -> None:
        if not levels or len(levels) < 2 or not levels[0] or not levels[1]:
            return
        bid_level = levels[0][0]
        ask_level = levels[1][0]
        bid = float(bid_level.get("px"))
        ask = float(ask_level.get("px"))
        bid_size = float(bid_level.get("sz", 0))
        ask_size = float(ask_level.get("sz", 0))
        depth_notional = min(bid * bid_size, ask * ask_size)
        quote_cache.put("hyperliquid", symbol, bid, ask, depth_notional, source, exchange_ts)

    def _write_hyperliquid_dex_quote(self, internal_symbol: str, hyperliquid_symbol: str) -> None:
        settings = get_settings()
        dex, _ = hyperliquid_symbol.split(":", 1)
        payload = json.dumps({"type": "metaAndAssetCtxs", "dex": dex}).encode("utf-8")
        req = request.Request(
            settings.hyperliquid_info_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=10) as resp:
            meta, contexts = json.loads(resp.read().decode("utf-8"))
        for asset, context in zip(meta.get("universe", []), contexts):
            if asset.get("name") != hyperliquid_symbol:
                continue
            impact = context.get("impactPxs") or []
            if len(impact) >= 2:
                bid = float(impact[0])
                ask = float(impact[1])
            else:
                mid = float(context.get("midPx") or context.get("markPx") or context.get("oraclePx"))
                bid = mid
                ask = mid
            depth_notional = float(context.get("openInterest", 0.0)) * ((bid + ask) / 2)
            quote_cache.put("hyperliquid", internal_symbol, bid, ask, depth_notional, "hyperliquid_metaAndAssetCtxs_dex", None)
            return


def _exchange_time_from_hyperliquid_ms(value: Any) -> datetime | None:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).replace(tzinfo=None)


def l2book_subscription(coin: str, *, fast: bool) -> dict[str, Any]:
    subscription: dict[str, Any] = {"type": "l2Book", "coin": coin}
    if fast:
        subscription["fast"] = True
    return subscription


def hyperliquid_symbol_map(mappings, *, hip3_only: bool) -> dict[str, str]:
    return {
        item.hyperliquid_symbol: item.symbol
        for item in mappings
        if not hip3_only or ":" in item.hyperliquid_symbol
    }


market_data_manager = MarketDataManager()
