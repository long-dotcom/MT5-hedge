from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Fill, HedgeGroup, HedgeGroupEvent, Order, SystemLog, WorkerRun
from app.db.retention import prune_table_by_id
from app.execution.hedge_pool import CloseResultEvent, hedge_pool


def persist_hedge_pool_events(db: Session, *, limit: int = 100) -> int:
    events = hedge_pool.drain_close_results(limit)
    if not events:
        return 0
    processed = 0
    failed: list[CloseResultEvent] = []
    for event in events:
        try:
            _persist_close_result(db, event)
            db.commit()
            processed += 1
        except Exception as exc:
            db.rollback()
            failed.append(event)
            db.add(SystemLog(level="warning", category="hedge_pool_persistence", message=f"对冲池事件落库失败: #{event.group_id}", context=str(exc)))
            prune_table_by_id(db, SystemLog)
            db.commit()
            break
    if failed:
        hedge_pool.requeue_close_results(failed + events[processed + len(failed) :])
    db.add(WorkerRun(worker_name="hedge_pool_persistence", status="success", duration_ms=0))
    prune_table_by_id(db, WorkerRun)
    db.commit()
    return processed


def _persist_close_result(db: Session, event: CloseResultEvent) -> None:
    group = db.get(HedgeGroup, event.group_id)
    if not group:
        raise ValueError("对冲组不存在，无法持久化内存事件")
    for item in event.orders:
        order = Order(
            hedge_group_id=event.group_id,
            platform=item.platform,
            symbol=item.symbol,
            side=item.side,
            quantity=item.quantity,
            order_type=item.order_type,
            price=item.average_price or item.price,
            post_only=item.post_only,
            reduce_only=item.reduce_only,
            ttl_seconds=item.ttl_seconds,
            status=item.status,
            external_order_id=item.external_order_id,
            error_message=item.error_message,
        )
        db.add(order)
        db.flush()
        for fill in item.fills:
            db.add(
                Fill(
                    order_id=order.id,
                    platform=fill.platform,
                    symbol=fill.symbol,
                    side=fill.side,
                    quantity=fill.quantity,
                    price=fill.price,
                    fee=fill.fee,
                )
            )
    group.status = event.status
    group.close_reason = event.close_reason
    if event.unrealized_pnl is not None:
        group.unrealized_pnl = event.unrealized_pnl
    if event.realized_pnl is not None:
        group.realized_pnl = event.realized_pnl
    if event.fees_delta:
        group.fees = float(group.fees or 0.0) + event.fees_delta
    if event.closed_at:
        group.closed_at = event.closed_at
    elif event.status == "closed":
        group.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(HedgeGroupEvent(hedge_group_id=event.group_id, event_type=event.event_type, detail=event.event_detail))
