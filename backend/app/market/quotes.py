from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Literal


SyncMode = Literal["loose", "strict"]


@dataclass(frozen=True)
class Quote:
    platform: str
    symbol: str
    bid: float
    ask: float
    depth_notional: float
    exchange_ts: datetime | None
    local_recv_ts: datetime
    source: str
    sequence: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class SynchronizedQuote:
    symbol: str
    hyperliquid: Quote
    mt5: Quote
    time_diff_ms: float
    max_age_ms: float
    mode: SyncMode


class QuoteCache:
    def __init__(self, max_history: int = 5000) -> None:
        self.max_history = max_history
        self._lock = threading.RLock()
        self._quotes: dict[tuple[str, str], list[Quote]] = {}
        self._sequence = 0

    def put(
        self,
        platform: str,
        symbol: str,
        bid: float,
        ask: float,
        depth_notional: float,
        source: str,
        exchange_ts: datetime | None = None,
    ) -> Quote:
        with self._lock:
            self._sequence += 1
            quote = Quote(
                platform=platform,
                symbol=symbol,
                bid=float(bid),
                ask=float(ask),
                depth_notional=float(depth_notional),
                exchange_ts=exchange_ts,
                local_recv_ts=datetime.utcnow(),
                source=source,
                sequence=self._sequence,
            )
            key = (platform, symbol)
            history = self._quotes.setdefault(key, [])
            history.append(quote)
            if len(history) > self.max_history:
                del history[: len(history) - self.max_history]
            return quote

    def latest(self, platform: str, symbol: str) -> Quote | None:
        with self._lock:
            history = self._quotes.get((platform, symbol), [])
            return history[-1] if history else None

    def history(self, platform: str, symbol: str) -> list[Quote]:
        with self._lock:
            return list(self._quotes.get((platform, symbol), []))

    def symbols(self) -> list[str]:
        with self._lock:
            return sorted({symbol for _, symbol in self._quotes})


quote_cache = QuoteCache()


class QuoteSynchronizer:
    def __init__(self, cache: QuoteCache) -> None:
        self.cache = cache

    def synchronized(
        self,
        symbol: str,
        mode: SyncMode,
        max_time_diff_ms: int,
        max_age_ms: int,
    ) -> tuple[SynchronizedQuote | None, str]:
        hl = self.cache.latest("hyperliquid", symbol)
        mt5 = self.cache.latest("mt5", symbol)
        if not hl or not mt5:
            return None, "缺少实时行情"
        now = datetime.utcnow()
        hl_age = (now - hl.local_recv_ts).total_seconds() * 1000
        mt5_age = (now - mt5.local_recv_ts).total_seconds() * 1000
        max_age = max(hl_age, mt5_age)
        if max_age > max_age_ms:
            return None, f"行情过期，最大延迟 {max_age:.0f}ms"
        time_diff = abs((hl.local_recv_ts - mt5.local_recv_ts).total_seconds() * 1000)
        if time_diff > max_time_diff_ms:
            return None, f"行情未对齐，时间差 {time_diff:.0f}ms"
        if hl.bid <= 0 or hl.ask <= 0 or mt5.bid <= 0 or mt5.ask <= 0:
            return None, "报价异常"
        if hl.bid > hl.ask or mt5.bid > mt5.ask:
            return None, "bid/ask 反转"
        return SynchronizedQuote(symbol=symbol, hyperliquid=hl, mt5=mt5, time_diff_ms=time_diff, max_age_ms=max_age, mode=mode), ""


quote_synchronizer = QuoteSynchronizer(quote_cache)
