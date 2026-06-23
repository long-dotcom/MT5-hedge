from sqlalchemy.orm import Session

from app.db.models import Fill, HedgeGroup, Order


def actual_entry_spread_from_fills(db: Session, group: HedgeGroup) -> float | None:
    return actual_spread_from_fills(db, group, reduce_only=False)


def actual_close_spread_from_fills(db: Session, group: HedgeGroup) -> float | None:
    return actual_spread_from_fills(db, group, reduce_only=True)


def actual_spread_from_fills(db: Session, group: HedgeGroup, *, reduce_only: bool) -> float | None:
    hl_price = weighted_fill_price(db, group.id, "hyperliquid", reduce_only=reduce_only)
    mt5_price = weighted_fill_price(db, group.id, "mt5", reduce_only=reduce_only)
    if hl_price is None or mt5_price is None:
        return None
    if group.direction == "long_hyperliquid_short_mt5":
        return mt5_price - hl_price
    return hl_price - mt5_price


def pnl_from_close_spread(group: HedgeGroup, close_spread: float) -> float:
    quantity = float(group.hyperliquid_quantity or group.quantity or 1.0)
    entry_spread = float(group.entry_spread or group.entry_threshold or 0.0)
    gross = (entry_spread - close_spread) * quantity
    return gross - float(group.fees or 0.0) - float(group.funding or 0.0) - float(group.swap or 0.0)


def realized_pnl_from_fills(db: Session, group: HedgeGroup) -> float | None:
    close_spread = actual_close_spread_from_fills(db, group)
    if close_spread is None:
        return None
    return pnl_from_close_spread(group, close_spread)


def weighted_fill_price(db: Session, group_id: int, platform: str, *, reduce_only: bool) -> float | None:
    rows = (
        db.query(Fill)
        .join(Order, Fill.order_id == Order.id)
        .filter(
            Order.hedge_group_id == group_id,
            Order.platform == platform,
            Order.reduce_only.is_(reduce_only),
            Fill.quantity > 0,
            Fill.price > 0,
        )
        .all()
    )
    quantity = sum(float(row.quantity or 0.0) for row in rows)
    if quantity <= 0:
        return None
    notional = sum(float(row.quantity or 0.0) * float(row.price or 0.0) for row in rows)
    return notional / quantity
