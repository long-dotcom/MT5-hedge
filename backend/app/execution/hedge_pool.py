from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import RLock
from typing import Deque, Iterable

from sqlalchemy.orm import Session

from app.db.models import HedgeGroup


POOL_GROUP_STATUSES = {"opening", "open", "open_partial", "closing", "manual_intervention"}
AUTO_CLOSE_STATUSES = {"open", "open_partial"}


@dataclass(frozen=True)
class HedgeGroupSnapshot:
    id: int
    symbol: str
    direction: str
    status: str
    execution_mode: str
    notional: float
    quantity: float
    leg_b_quantity: float
    leg_a_quantity: float
    open_cost: float
    fees: float
    funding: float
    swap: float
    realized_pnl: float
    unrealized_pnl: float
    trigger_spread: float
    entry_spread: float
    entry_threshold: float
    exit_target: float
    overheat_threshold: float
    close_reason: str
    opened_at: datetime | None
    closed_at: datetime | None
    source: str

    @classmethod
    def from_row(cls, row: HedgeGroup) -> "HedgeGroupSnapshot":
        return cls(
            id=int(row.id),
            symbol=str(row.symbol),
            direction=str(row.direction),
            status=str(row.status),
            execution_mode=str(row.execution_mode),
            notional=float(row.notional or 0.0),
            quantity=float(row.quantity or 0.0),
            leg_b_quantity=float(row.leg_b_quantity if row.leg_b_quantity is not None else row.quantity or 0.0),
            leg_a_quantity=float(row.leg_a_quantity if row.leg_a_quantity is not None else row.quantity or 0.0),
            open_cost=float(row.open_cost or 0.0),
            fees=float(row.fees or 0.0),
            funding=float(row.funding or 0.0),
            swap=float(row.swap or 0.0),
            realized_pnl=float(row.realized_pnl or 0.0),
            unrealized_pnl=float(row.unrealized_pnl or 0.0),
            trigger_spread=float(row.trigger_spread or 0.0),
            entry_spread=float(row.entry_spread or 0.0),
            entry_threshold=float(row.entry_threshold or 0.0),
            exit_target=float(row.exit_target or 0.0),
            overheat_threshold=float(row.overheat_threshold or 0.0),
            close_reason=str(row.close_reason or ""),
            opened_at=row.opened_at,
            closed_at=row.closed_at,
            source=str(row.source or ""),
        )

    def with_updates(self, **kwargs) -> "HedgeGroupSnapshot":
        return replace(self, **kwargs)


@dataclass(frozen=True)
class CloseFillSnapshot:
    platform: str
    symbol: str
    side: str
    quantity: float
    price: float
    fee: float
    external_order_id: str


@dataclass(frozen=True)
class CloseOrderSnapshot:
    platform: str
    symbol: str
    side: str
    quantity: float
    order_type: str
    price: float | None
    post_only: bool
    reduce_only: bool
    ttl_seconds: int
    status: str
    external_order_id: str
    average_price: float | None
    error_message: str
    filled_quantity: float
    fee: float
    fills: tuple[CloseFillSnapshot, ...] = ()


@dataclass(frozen=True)
class CloseResultEvent:
    group_id: int
    status: str
    close_reason: str
    event_type: str
    event_detail: str
    realized_pnl: float | None
    unrealized_pnl: float | None
    fees_delta: float
    closed_at: datetime | None
    orders: tuple[CloseOrderSnapshot, ...] = ()


class HedgePoolStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._groups: dict[int, HedgeGroupSnapshot] = {}
        self._pending_close_results: Deque[CloseResultEvent] = deque()

    def load_from_db(self, db: Session) -> int:
        rows = db.query(HedgeGroup).filter(HedgeGroup.status.in_(POOL_GROUP_STATUSES)).all()
        with self._lock:
            current = dict(self._groups)
            snapshots = {}
            for row in rows:
                snapshot = HedgeGroupSnapshot.from_row(row)
                existing = current.get(snapshot.id)
                if existing and snapshot.status in AUTO_CLOSE_STATUSES and existing.status in AUTO_CLOSE_STATUSES:
                    snapshot = snapshot.with_updates(unrealized_pnl=existing.unrealized_pnl)
                snapshots[snapshot.id] = snapshot
            self._groups = snapshots
        return len(snapshots)

    def snapshot_groups(self) -> list[HedgeGroupSnapshot]:
        with self._lock:
            rows = list(self._groups.values())
        return sorted(rows, key=lambda item: (item.symbol, item.id))

    def snapshot_open_groups(self, modes: Iterable[str] | None = None) -> list[HedgeGroupSnapshot]:
        allowed_modes = set(modes or [])
        with self._lock:
            rows = [
                group
                for group in self._groups.values()
                if group.status in AUTO_CLOSE_STATUSES and (not allowed_modes or group.execution_mode in allowed_modes)
            ]
        return sorted(rows, key=lambda item: item.opened_at or datetime.min)

    def get(self, group_id: int) -> HedgeGroupSnapshot | None:
        with self._lock:
            return self._groups.get(int(group_id))

    def upsert_group(self, group: HedgeGroup | HedgeGroupSnapshot) -> HedgeGroupSnapshot:
        snapshot = group if isinstance(group, HedgeGroupSnapshot) else HedgeGroupSnapshot.from_row(group)
        with self._lock:
            if snapshot.status in POOL_GROUP_STATUSES:
                self._groups[snapshot.id] = snapshot
            else:
                self._groups.pop(snapshot.id, None)
        return snapshot

    def try_mark_closing(self, group_id: int, reason: str = "", estimated_pnl: float | None = None) -> HedgeGroupSnapshot | None:
        with self._lock:
            current = self._groups.get(int(group_id))
            if not current or current.status not in AUTO_CLOSE_STATUSES:
                return None
            updated = current.with_updates(
                status="closing",
                close_reason=reason or current.close_reason,
                unrealized_pnl=current.unrealized_pnl if estimated_pnl is None else float(estimated_pnl),
            )
            self._groups[current.id] = updated
            return updated

    def restore_status(self, snapshot: HedgeGroupSnapshot, status: str | None = None, reason: str = "") -> HedgeGroupSnapshot:
        restored = snapshot.with_updates(status=status or snapshot.status, close_reason=reason or snapshot.close_reason)
        return self.upsert_group(restored)

    def mark_closed(
        self,
        group_id: int,
        *,
        realized_pnl: float | None = None,
        fees_delta: float = 0.0,
        reason: str = "",
        status: str = "closed",
    ) -> HedgeGroupSnapshot | None:
        with self._lock:
            current = self._groups.get(int(group_id))
            if not current:
                return None
            now = datetime.now(timezone.utc).replace(tzinfo=None) if status == "closed" else current.closed_at
            updated = current.with_updates(
                status=status,
                closed_at=now,
                realized_pnl=current.realized_pnl if realized_pnl is None else float(realized_pnl),
                unrealized_pnl=0.0 if status == "closed" else current.unrealized_pnl,
                fees=current.fees + float(fees_delta or 0.0),
                close_reason=reason or current.close_reason,
            )
            if updated.status in POOL_GROUP_STATUSES:
                self._groups[updated.id] = updated
            else:
                self._groups.pop(updated.id, None)
            return updated

    def mark_manual_intervention(self, group_id: int, reason: str = "") -> HedgeGroupSnapshot | None:
        with self._lock:
            current = self._groups.get(int(group_id))
            if not current:
                return None
            updated = current.with_updates(status="manual_intervention", close_reason=reason or current.close_reason)
            self._groups[updated.id] = updated
            return updated

    def remove_closed(self, group_id: int) -> None:
        with self._lock:
            current = self._groups.get(int(group_id))
            if current and current.status == "closed":
                self._groups.pop(int(group_id), None)

    def enqueue_close_result(self, event: CloseResultEvent) -> None:
        with self._lock:
            self._pending_close_results.append(event)

    def drain_close_results(self, limit: int = 100) -> list[CloseResultEvent]:
        drained: list[CloseResultEvent] = []
        with self._lock:
            while self._pending_close_results and len(drained) < limit:
                drained.append(self._pending_close_results.popleft())
        return drained

    def requeue_close_results(self, events: Iterable[CloseResultEvent]) -> None:
        items = list(events)
        if not items:
            return
        with self._lock:
            for event in reversed(items):
                self._pending_close_results.appendleft(event)


hedge_pool = HedgePoolStore()
