from __future__ import annotations

from sqlalchemy.orm import Session

from app.adapters.base import AdapterOrder, AdapterOrderResult
from app.db.models import ExchangeCredential, SymbolMapping
from app.exchanges.credentials import binance_futures_probe_order


NAUTILUS_PROBE_SUPPORTED_VENUES = {"binance"}


def nautilus_probe_supported(venue: str) -> bool:
    return _venue(venue) in NAUTILUS_PROBE_SUPPORTED_VENUES


def place_nautilus_probe_order(db: Session, credential: ExchangeCredential, order: AdapterOrder) -> AdapterOrderResult:
    venue = _venue(credential.venue or order.platform)
    if venue == "binance":
        return binance_futures_probe_order(credential, order, configured_min_base_size=_configured_min_base_size(db, venue, order))
    return AdapterOrderResult(False, "", "rejected", 0.0, 0.0, 0.0, f"Nautilus venue {venue} 尚未实现 paper-live 探针下单")


def _configured_min_base_size(db: Session, venue: str, order: AdapterOrder) -> float:
    symbols = {
        str(order.symbol or "").strip().upper(),
        str(order.venue_symbol or "").strip().upper(),
    }
    rows = db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True)).all()
    for row in rows:
        if _venue(row.leg_a_venue) == venue:
            candidates = {row.symbol, row.leg_a_symbol, row.leg_a_venue_symbol}
            if symbols & {str(item or "").strip().upper() for item in candidates}:
                return float(row.leg_a_min_base_size or row.min_order_size or 0.0)
        if _venue(row.leg_b_venue) == venue:
            candidates = {row.symbol, row.leg_b_symbol}
            if symbols & {str(item or "").strip().upper() for item in candidates}:
                return float(row.min_order_size or 0.0)
    return 0.0


def _venue(value: str) -> str:
    return str(value or "").strip().lower()
