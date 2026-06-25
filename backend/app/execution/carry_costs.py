import json
import time
from datetime import datetime, timedelta, timezone
from urllib import request

from sqlalchemy.orm import Session

from app.adapters.mt5 import _initialize_mt5
from app.config.settings import get_settings, hyperliquid_execution_info_url
from app.db.models import HedgeGroup, Order, SymbolMapping, SystemLog, WorkerRun
from app.db.retention import prune_table_by_id


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
            funding = _hyperliquid_funding_cost(group, mapping)
            swap = _mt5_swap_cost(db, group, mapping)
            if funding is not None:
                group.funding = funding
            if swap is not None:
                group.swap = swap
            if abs(float(group.funding or 0.0) - old_funding) > 1e-9 or abs(float(group.swap or 0.0) - old_swap) > 1e-9:
                changed += 1
        db.add(WorkerRun(worker_name="carry_cost_sync", status="success", duration_ms=int((time.perf_counter() - started) * 1000)))
        prune_table_by_id(db, WorkerRun)
        db.commit()
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
    total = 0.0
    for row in rows:
        delta = row.get("delta", {}) if isinstance(row, dict) else {}
        coin = str(delta.get("coin") or "")
        if coin not in symbols:
            continue
        total += _float(delta.get("usdc"))
    return total


def _paper_hyperliquid_funding_cost(group: HedgeGroup, mapping: SymbolMapping) -> float | None:
    start_ms, end_ms = _group_window_ms(group)
    if end_ms <= start_ms:
        return 0.0
    try:
        rows = _post_hyperliquid_info({"type": "fundingHistory", "coin": mapping.hyperliquid_symbol, "startTime": start_ms, "endTime": end_ms})
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    side_sign = 1.0 if group.direction == "long_hyperliquid_short_mt5" else -1.0
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
    try:
        positions = mt5.positions_get(symbol=mapping.mt5_symbol)
    except TypeError:
        positions = mt5.positions_get()
    except Exception:
        return None
    target_type = getattr(mt5, "POSITION_TYPE_SELL", 1) if group.direction == "long_hyperliquid_short_mt5" else getattr(mt5, "POSITION_TYPE_BUY", 0)
    candidates = [
        position
        for position in positions or []
        if str(getattr(position, "symbol", "")) == mapping.mt5_symbol
        and int(getattr(position, "type", -1)) == int(target_type)
        and float(getattr(position, "volume", 0.0) or 0.0) > 0
    ]
    if not candidates:
        return None
    total_volume = sum(float(getattr(position, "volume", 0.0) or 0.0) for position in candidates)
    total_swap = sum(float(getattr(position, "swap", 0.0) or 0.0) for position in candidates)
    if total_volume <= 0:
        return None
    expected = float(group.mt5_quantity or group.quantity or 0.0)
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
    value = str(mapping.hyperliquid_symbol or "")
    aliases = {value}
    if ":" in value:
        aliases.add(value.split(":", 1)[1])
    return aliases


def _float(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
