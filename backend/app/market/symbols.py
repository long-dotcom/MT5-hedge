from pathlib import Path
from time import monotonic
from types import SimpleNamespace

import yaml
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.db.models import SymbolMapping

_mapping_cache: tuple[int, float, list[SimpleNamespace]] = (0, 0.0, [])
_MAPPING_CACHE_TTL_SECONDS = 2.0


def load_symbol_mapping_file() -> list[dict]:
    path = Path(get_settings().symbol_mapping_path)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("symbols", [])


def seed_symbol_mappings_from_file(db: Session) -> int:
    seeded = 0
    for item in load_symbol_mapping_file():
        symbol = item["symbol"]
        row = db.query(SymbolMapping).filter(SymbolMapping.symbol == symbol).first()
        if row:
            continue
        payload = {
            "hyperliquid_symbol": item.get("hyperliquid_symbol", symbol),
            "mt5_symbol": item.get("mt5_symbol", symbol),
            "base_asset": item.get("base_asset", symbol),
            "quote_asset": item.get("quote_asset", "USD"),
            "contract_multiplier": float(item.get("contract_multiplier", 1.0)),
            "min_order_size": float(item.get("min_order_size", 0.001)),
            "min_entry_spread": float(item.get("min_entry_spread", 0.0)),
            "max_close_spread": float(item.get("max_close_spread", 0.0)),
            "mt5_min_lot": float(item.get("mt5_min_lot", 0.0)),
            "mt5_volume_step": float(item.get("mt5_volume_step", 0.0)),
            "mt5_contract_size": float(item.get("mt5_contract_size", item.get("contract_multiplier", 1.0))),
            "mt5_currency_base": item.get("mt5_currency_base", ""),
            "mt5_currency_profit": item.get("mt5_currency_profit", item.get("quote_asset", "USD")),
            "mt5_currency_margin": item.get("mt5_currency_margin", item.get("quote_asset", "USD")),
            "mt5_calc_mode": int(item.get("mt5_calc_mode", 0)),
            "mt5_min_base_size": float(item.get("mt5_min_base_size", 0.0)),
            "hyperliquid_min_base_size": float(item.get("hyperliquid_min_base_size", 0.0)),
            "hyperliquid_min_notional": float(item.get("hyperliquid_min_notional", 10.0)),
            "execution_style": item.get("execution_style", "taker_taker"),
            "hl_open_order_type": item.get("hl_open_order_type", "market"),
            "hl_close_order_type": item.get("hl_close_order_type", "market"),
            "hl_post_only": bool(item.get("hl_post_only", False)),
            "hl_maker_offset_bps": float(item.get("hl_maker_offset_bps", 1.0)),
            "hl_order_ttl_seconds": int(item.get("hl_order_ttl_seconds", 3)),
            "hl_unfilled_action": item.get("hl_unfilled_action", "cancel"),
            "single_leg_action": item.get("single_leg_action", "manual_intervention"),
            "mt5_open_order_type": item.get("mt5_open_order_type", "market"),
            "mt5_close_order_type": item.get("mt5_close_order_type", "market"),
            "mt5_pre_close_no_open_minutes": int(item.get("mt5_pre_close_no_open_minutes", 15)),
            "mt5_post_open_cooldown_minutes": int(item.get("mt5_post_open_cooldown_minutes", 10)),
            "allow_hold_through_mt5_close": bool(item.get("allow_hold_through_mt5_close", False)),
            "quantity_precision": int(item.get("quantity_precision", 4)),
            "price_precision": int(item.get("price_precision", 2)),
            "min_tick": float(item.get("min_tick", 0.01)),
            "max_slippage_bps": float(item.get("max_slippage_bps", 8.0)),
            "enabled": bool(item.get("enabled", True)),
        }
        db.add(SymbolMapping(symbol=symbol, **payload))
        seeded += 1
    db.commit()
    return seeded


def clear_symbol_mapping_cache() -> None:
    global _mapping_cache
    _mapping_cache = (0, 0.0, [])


def enabled_mappings(db: Session) -> list[SimpleNamespace]:
    global _mapping_cache
    now = monotonic()
    bind_id = id(db.get_bind())
    cached_bind_id, cached_at, cached = _mapping_cache
    if cached and cached_bind_id == bind_id and now - cached_at < _MAPPING_CACHE_TTL_SECONDS:
        return cached
    rows = db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True)).order_by(SymbolMapping.symbol).all()
    cached = [_snapshot_mapping(row) for row in rows]
    _mapping_cache = (bind_id, now, cached)
    return cached


def _snapshot_mapping(row: SymbolMapping) -> SimpleNamespace:
    return SimpleNamespace(**{column.name: getattr(row, column.name) for column in row.__table__.columns})
