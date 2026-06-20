from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class Ticker:
    symbol: str
    bid: float
    ask: float
    depth_notional: float
    timestamp: datetime


@dataclass
class Account:
    platform: str
    equity: float
    available_balance: float
    margin_used: float
    margin_ratio: float
    currency: str = "USD"


@dataclass
class AdapterOrder:
    platform: str
    symbol: str
    side: str
    quantity: float
    price: float | None = None
    order_type: str = "market"
    post_only: bool = False
    ttl_seconds: int = 0
    paper_latency_ms: int = 0


@dataclass
class AdapterOrderResult:
    success: bool
    external_order_id: str
    status: str
    filled_quantity: float
    average_price: float
    fee: float
    error_message: str = ""


class ExchangeAdapter(Protocol):
    platform: str

    def get_symbols(self) -> list[str]:
        ...

    def get_account(self) -> Account:
        ...

    def get_positions(self) -> list[dict]:
        ...

    def get_ticker(self, symbol: str) -> Ticker:
        ...

    def get_orderbook(self, symbol: str, depth: int = 5) -> dict:
        ...

    def place_order(self, order: AdapterOrder) -> AdapterOrderResult:
        ...

    def cancel_order(self, order_id: str) -> bool:
        ...

    def get_order(self, order_id: str) -> dict:
        ...

    def get_trades(self, order_id: str) -> list[dict]:
        ...
