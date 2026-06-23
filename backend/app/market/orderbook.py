from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class OrderBook:
    platform: str
    symbol: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    source: str
    local_recv_ts: datetime
    exchange_ts: datetime | None = None


@dataclass(frozen=True)
class SimulatedFill:
    requested_quantity: float
    filled_quantity: float
    average_price: float
    notional: float
    worst_price: float
    slippage_bps: float
    enough_liquidity: bool


class OrderBookCache:
    def __init__(self, max_levels: int = 20) -> None:
        self.max_levels = max_levels
        self._lock = threading.RLock()
        self._books: dict[tuple[str, str], OrderBook] = {}

    def put(
        self,
        platform: str,
        symbol: str,
        bids: list[tuple[float, float]] | tuple[tuple[float, float], ...],
        asks: list[tuple[float, float]] | tuple[tuple[float, float], ...],
        source: str,
        exchange_ts: datetime | None = None,
    ) -> OrderBook:
        book = OrderBook(
            platform=platform,
            symbol=symbol,
            bids=tuple(BookLevel(float(price), float(size)) for price, size in bids[: self.max_levels] if float(price) > 0 and float(size) > 0),
            asks=tuple(BookLevel(float(price), float(size)) for price, size in asks[: self.max_levels] if float(price) > 0 and float(size) > 0),
            source=source,
            local_recv_ts=datetime.utcnow(),
            exchange_ts=exchange_ts,
        )
        with self._lock:
            self._books[(platform, symbol)] = book
        return book

    def latest(self, platform: str, symbol: str) -> OrderBook | None:
        with self._lock:
            return self._books.get((platform, symbol))


def simulate_market_fill(book: OrderBook, side: str, quantity: float) -> SimulatedFill:
    requested = max(float(quantity or 0.0), 0.0)
    if requested <= 0:
        return SimulatedFill(requested, 0.0, 0.0, 0.0, 0.0, 0.0, False)
    levels = book.asks if side.lower() == "buy" else book.bids
    reference_price = levels[0].price if levels else 0.0
    remaining = requested
    filled = 0.0
    notional = 0.0
    worst_price = 0.0
    for level in levels:
        take = min(remaining, level.size)
        if take <= 0:
            continue
        filled += take
        notional += take * level.price
        worst_price = level.price
        remaining -= take
        if remaining <= 1e-12:
            break
    average = notional / filled if filled > 0 else 0.0
    if reference_price > 0 and average > 0:
        if side.lower() == "buy":
            slippage = (average - reference_price) / reference_price * 10_000
        else:
            slippage = (reference_price - average) / reference_price * 10_000
    else:
        slippage = 0.0
    return SimulatedFill(
        requested_quantity=requested,
        filled_quantity=filled,
        average_price=average,
        notional=notional,
        worst_price=worst_price,
        slippage_bps=max(slippage, 0.0),
        enough_liquidity=filled + 1e-12 >= requested,
    )


def parse_hyperliquid_levels(levels: Any) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    if not levels or len(levels) < 2:
        return [], []
    return _parse_side(levels[0]), _parse_side(levels[1])


def _parse_side(rows: Any) -> list[tuple[float, float]]:
    parsed = []
    for row in rows or []:
        if isinstance(row, dict):
            price = row.get("px")
            size = row.get("sz")
        else:
            try:
                price, size = row[0], row[1]
            except Exception:
                continue
        try:
            parsed.append((float(price), float(size)))
        except (TypeError, ValueError):
            continue
    return parsed


order_book_cache = OrderBookCache()
