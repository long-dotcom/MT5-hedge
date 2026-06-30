import json
import time
from datetime import datetime, timedelta, timezone
from urllib import request

from sqlalchemy.orm import Session

from app.adapters.venue import mapping_leg
from app.adapters.mt5 import _initialize_mt5
from app.config.settings import get_settings, hyperliquid_execution_info_url
from app.db.models import HedgeGroup, Order, SymbolMapping, SystemLog, WorkerRun
from app.db.retention import prune_table_by_id
from app.execution.hedge_pool import hedge_pool


ACTIVE_COST_STATUSES = {"open", "open_partial", "closing", "manual_intervention"}
_last_sync_at = 0.0


def run_carry_cost_sync(db: Session, *, force: bool = False) -> int:
    global _last_sync_at
    settings = get_settings()
    now = time.time()
    if not force and now - _last_sync_at < max(settings.carry_cost_sync_interval_seconds, 1):
        return 0
    _last_sync_at = now
    started = time.perf_counter()
    changed = 0
    try:
        groups = db.query(HedgeGroup).filter(HedgeGroup.status.in_(ACTIVE_COST_STATUSES)).all()
        for group in groups:
            mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
            if not mapping:
                continue
            old_funding = float(group.funding or 0.0)
            old_swap = float(group.swap or 0.0)
            funding = _hyperliquid_funding_cost(group, mapping) if _venue_leg(mapping, "hyperliquid") else None
            swap = _mt5_swap_cost(db, group, mapping) if _venue_leg(mapping, "mt5") else None
            if funding is not None:
                group.funding = funding
            if swap is not None:
                group.swap = swap
            if abs(float(group.funding or 0.0) - old_funding) > 1e-9 or abs(float(group.swap or 0.0) - old_swap) > 1e-9:
                changed += 1
        db.add(WorkerRun(worker_name="carry_cost_sync", status="success", duration_ms=int((time.perf_counter() - started) * 1000)))
        prune_table_by_id(db, WorkerRun)
        db.commit()
        hedge_pool.load_from_db(db)
        return changed
    except Exception as exc:
        db.rollback()
        db.add(WorkerRun(worker_name="carry_cost_sync", status="failed", duration_ms=int((time.perf_counter() - started) * 1000), error_message=str(exc)))
        db.add(SystemLog(level="warning", category="carry_cost_sync", message="持仓资金费/过夜费同步失败", context=str(exc)))
        prune_table_by_id(db, WorkerRun)
        prune_table_by_id(db, SystemLog)
        db.commit()
        return changed


def _hyperliquid_funding_cost(group: HedgeGroup, mapping: SymbolMapping) -> float | None:
    if not group.opened_at:
        return None
    if group.execution_mode == "live":
        amount = _hyperliquid_user_funding_usdc(group, mapping)
        if amount is not None:
            return -amount
    return _paper_hyperliquid_funding_cost(group, mapping)


def _hyperliquid_user_funding_usdc(group: HedgeGroup, mapping: SymbolMapping) -> float | None:
    settings = get_settings()
    user = settings.hyperliquid_account_address
    if not user:
        return None
    start_ms, end_ms = _group_window_ms(group)
    payload = {"type": "userFunding", "user": user, "startTime": start_ms, "endTime": end_ms}
    try:
        rows = _post_hyperliquid_info(payload)
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    symbols = _hyperliquid_symbol_aliases(mapping)
    if not symbols:
        return None
    total = 0.0
    for row in rows:
        delta = row.get("delta", {}) if isinstance(row, dict) else {}
        coin = str(delta.get("coin") or "")
        if coin not in symbols:
            continue
        total += _float(delta.get("usdc"))
    return total


def _paper_hyperliquid_funding_cost(group: HedgeGroup, mapping: SymbolMapping) -> float | None:
    hyper_leg = _venue_leg(mapping, "hyperliquid")
    if not hyper_leg:
        return None
    _, hyper_symbol = hyper_leg
    start_ms, end_ms = _group_window_ms(group)
    if end_ms <= start_ms:
        return 0.0
    try:
        rows = _post_hyperliquid_info({"type": "fundingHistory", "coin": hyper_symbol, "startTime": start_ms, "endTime": end_ms})
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    side_sign = 1.0 if _direction_is_venue_long(group.direction, hyper_leg[0]) else -1.0
    notional = float(group.notional or 0.0)
    return sum(notional * _float(row.get("fundingRate")) * side_sign for row in rows if isinstance(row, dict))


def _mt5_swap_cost(db: Session, group: HedgeGroup, mapping: SymbolMapping) -> float | None:
    if not group.opened_at:
        return None
    try:
        import MetaTrader5 as mt5  # type: ignore
    except Exception:
        return None
    if not _initialize_mt5(mt5, get_settings()):
        return None
    if group.status in ACTIVE_COST_STATUSES:
        position_swap = _open_position_swap(mt5, group, mapping)
        if position_swap is not None:
            return -position_swap
    deal_swap = _deal_swap(db, mt5, group)
    if deal_swap is not None:
        return -deal_swap
    return None


def _open_position_swap(mt5, group: HedgeGroup, mapping: SymbolMapping) -> float | None:
    mt5_leg = _venue_leg(mapping, "mt5")
    if not mt5_leg:
        return None
    mt5_leg_name, mt5_symbol = mt5_leg
    try:
        positions = mt5.positions_get(symbol=mt5_symbol)
    except TypeError:
        positions = mt5.positions_get()
    except Exception:
        return None
    target_type = getattr(mt5, "POSITION_TYPE_BUY", 0) if _direction_is_venue_long(group.direction, mt5_leg_name) else getattr(mt5, "POSITION_TYPE_SELL", 1)
    candidates = [
        position
        for position in positions or []
        if str(getattr(position, "symbol", "")) == mt5_symbol
        and int(getattr(position, "type", -1)) == int(target_type)
        and float(getattr(position, "volume", 0.0) or 0.0) > 0
    ]
    if not candidates:
        return None
    total_volume = sum(float(getattr(position, "volume", 0.0) or 0.0) for position in candidates)
    total_swap = sum(float(getattr(position, "swap", 0.0) or 0.0) for position in candidates)
    if total_volume <= 0:
        return None
    expected_quantity = group.leg_a_quantity if mt5_leg_name == "a" else group.leg_b_quantity
    expected = float(expected_quantity or group.quantity or 0.0)
    ratio = min(max(expected / total_volume, 0.0), 1.0) if expected > 0 else 1.0
    return total_swap * ratio


def _deal_swap(db: Session, mt5, group: HedgeGroup) -> float | None:
    orders = db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "mt5", Order.external_order_id != "").all()
    total = 0.0
    found = False
    for order in orders:
        try:
            ticket = int(str(order.external_order_id).strip())
        except (TypeError, ValueError):
            continue
        for reader in (lambda: mt5.history_deals_get(order=ticket), lambda: mt5.history_deals_get(ticket=ticket)):
            try:
                rows = reader()
            except Exception:
                rows = None
            for deal in rows or []:
                found = True
                total += float(getattr(deal, "swap", 0.0) or 0.0)
            if rows:
                break
    return total if found else None


def _group_window_ms(group: HedgeGroup) -> tuple[int, int]:
    start = group.opened_at or group.created_at
    end = group.closed_at or datetime.now(timezone.utc).replace(tzinfo=None)
    return int(start.timestamp() * 1000), int((end + timedelta(seconds=1)).timestamp() * 1000)


def _post_hyperliquid_info(payload: dict):
    settings = get_settings()
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(hyperliquid_execution_info_url(settings), data=data, headers={"Content-Type": "application/json"}, method="POST")
    with request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _hyperliquid_symbol_aliases(mapping: SymbolMapping) -> set[str]:
    hyper_leg = _venue_leg(mapping, "hyperliquid")
    if not hyper_leg:
        return set()
    value = str(hyper_leg[1] or "")
    aliases = {value}
    if ":" in value:
        aliases.add(value.split(":", 1)[1])
    return aliases


def _venue_leg(mapping: SymbolMapping, venue: str) -> tuple[str, str] | None:
    for leg in ("a", "b"):
        leg_venue, leg_symbol = mapping_leg(mapping, leg)
        if leg_venue == venue:
            return leg, leg_symbol
    return None


def _direction_is_venue_long(direction: str, leg: str) -> bool:
    if direction == "long_leg_a_short_leg_b":
        return leg == "a"
    if direction == "long_leg_b_short_leg_a":
        return leg == "b"
    if direction == "long_mt5_short_hyperliquid":
        return leg == "b"
    return leg == "a"


def _float(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
