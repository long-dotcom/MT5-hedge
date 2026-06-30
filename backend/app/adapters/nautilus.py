from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.adapters.base import Account, AdapterOrder, AdapterOrderResult, Ticker
from app.config.settings import get_settings
from app.db.models import ExchangeCredential
from app.db.session import SessionLocal
from app.exchanges.credentials import binance_futures_account, binance_futures_positions, binance_ticker_book


class NautilusReadOnlyAdapter:
    def __init__(self, venue: str) -> None:
        self.platform = venue.strip().lower()
        self.settings = get_settings()
        self._import_error = _nautilus_import_error()

    def _require_available(self) -> None:
        if self._import_error:
            raise RuntimeError(f"NautilusTrader 可选依赖不可用: {self._import_error}")

    def get_symbols(self) -> list[str]:
        self._require_available()
        return []

    def get_account(self) -> Account:
        try:
            self._require_available()
        except RuntimeError as exc:
            return Account(self.platform, 0.0, 0.0, 0.0, 1.0, currency="USD")
        if self.platform == "binance":
            with SessionLocal() as db:
                credential = self._credential(db)
                if credential is not None:
                    account = binance_futures_account(credential)
                    equity = float(account.get("totalMarginBalance", 0.0) or account.get("totalWalletBalance", 0.0) or 0.0)
                    available = float(account.get("availableBalance", 0.0) or 0.0)
                    margin_used = float(account.get("totalInitialMargin", 0.0) or 0.0)
                    return Account(self.platform, equity, available, margin_used, (equity / margin_used) if margin_used > 0 else 1.0, currency="USDT")
        return Account(self.platform, 0.0, 0.0, 0.0, 1.0, currency="USD")

    def get_positions(self) -> list[dict[str, Any]]:
        self._require_available()
        if self.platform == "binance":
            with SessionLocal() as db:
                credential = self._credential(db)
                if credential is not None:
                    return binance_futures_positions(credential)
        return []

    def get_ticker(self, symbol: str) -> Ticker:
        self._require_available()
        if self.platform == "binance":
            with SessionLocal() as db:
                credential = self._credential(db)
                if credential is not None:
                    ticker = binance_ticker_book(credential, symbol)
                    return Ticker(
                        symbol=symbol,
                        bid=ticker["bid"],
                        ask=ticker["ask"],
                        depth_notional=ticker["depth_notional"],
                        timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
        raise RuntimeError(f"Nautilus venue {self.platform} 的行情读取尚未配置: {symbol}")

    def get_orderbook(self, symbol: str, depth: int = 5) -> dict[str, Any]:
        self._require_available()
        return {"bids": [], "asks": []}

    def place_order(self, order: AdapterOrder) -> AdapterOrderResult:
        return AdapterOrderResult(False, "", "rejected", 0.0, 0.0, 0.0, "Nautilus V1 只读模式不支持下单")

    def cancel_order(self, order_id: str) -> bool:
        return False

    def get_order(self, order_id: str) -> dict[str, Any]:
        return {"status": "unsupported", "external_order_id": order_id, "message": "Nautilus V1 只读模式不支持订单查询"}

    def get_trades(self, order_id: str) -> list[dict[str, Any]]:
        return []

    def _credential(self, db) -> ExchangeCredential | None:
        return db.query(ExchangeCredential).filter(ExchangeCredential.venue == self.platform, ExchangeCredential.enabled.is_(True)).first()


def nautilus_account_snapshot(venue: str) -> dict[str, Any]:
    adapter = NautilusReadOnlyAdapter(venue)
    account = adapter.get_account()
    return {
        "platform": account.platform,
        "equity": account.equity,
        "available_balance": account.available_balance,
        "margin_used": account.margin_used,
        "margin_ratio": account.margin_ratio,
        "currency": account.currency,
        "portfolio_value": account.equity,
        "perp_equity": account.equity,
        "withdrawable": account.available_balance,
        "free_collateral": account.available_balance,
        "data_source": "nautilus_read_only",
        "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
    }


def _nautilus_import_error() -> str:
    try:
        import nautilus_trader  # noqa: F401
    except Exception as exc:
        return str(exc)
    return ""
