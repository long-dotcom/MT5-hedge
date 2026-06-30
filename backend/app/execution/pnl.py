from sqlalchemy.orm import Session

from app.db.models import Fill, HedgeGroup, Order, SymbolMapping


def actual_entry_spread_from_fills(db: Session, group: HedgeGroup, *, mapping: SymbolMapping | None = None) -> float | None:
    return actual_spread_from_fills(db, group, reduce_only=False, mapping=mapping)


def actual_close_spread_from_fills(db: Session, group: HedgeGroup, *, mapping: SymbolMapping | None = None) -> float | None:
    return actual_spread_from_fills(db, group, reduce_only=True, mapping=mapping)


def actual_spread_from_fills(db: Session, group: HedgeGroup, *, reduce_only: bool, mapping: SymbolMapping | None = None) -> float | None:
    if mapping is None:
        mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
    leg_a_venue = mapping.leg_a_venue if mapping else "hyperliquid"
    leg_b_venue = mapping.leg_b_venue if mapping else "mt5"
    leg_a_price = weighted_fill_price(db, group.id, leg_a_venue, reduce_only=reduce_only)
    leg_b_price = weighted_fill_price(db, group.id, leg_b_venue, reduce_only=reduce_only)
    if leg_a_price is None or leg_b_price is None:
        return None
    if group.direction == "long_leg_a_short_leg_b":
        return leg_b_price - leg_a_price
    return leg_a_price - leg_b_price


def pnl_from_close_spread(group: HedgeGroup, close_spread: float) -> float:
    quantity = float(group.leg_a_quantity or group.quantity or 1.0)
    entry_spread = float(group.entry_spread or group.entry_threshold or 0.0)
    gross = (entry_spread - close_spread) * quantity
    return gross - float(group.fees or 0.0) - float(group.funding or 0.0) - float(group.swap or 0.0)


def realized_pnl_from_fills(db: Session, group: HedgeGroup, *, mapping: SymbolMapping | None = None) -> float | None:
    close_spread = actual_close_spread_from_fills(db, group, mapping=mapping)
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
