from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.adapters.base import AdapterOrder, AdapterOrderResult, ExchangeAdapter
from app.config.settings import get_settings


@dataclass(frozen=True)
class LegOrderIntent:
    platform: str
    symbol: str
    side: str
    quantity: float
    venue_symbol: str | None = None
    order_type: str = "market"
    price: float | None = None
    post_only: bool = False
    reduce_only: bool = False
    ttl_seconds: int = 0
    hedge_group_id: int | None = None
    client_order_id: str = ""


@dataclass(frozen=True)
class ExecutionIntent:
    hedge_group_id: int
    symbol: str
    mode: str
    legs: tuple[LegOrderIntent, ...]


@dataclass(frozen=True)
class OrderEvent:
    platform: str
    symbol: str
    side: str
    status: str
    external_order_id: str
    requested_quantity: float
    filled_quantity: float
    average_price: float
    fee: float
    message: str = ""
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class FillEvent:
    platform: str
    symbol: str
    side: str
    quantity: float
    price: float
    fee: float
    external_order_id: str
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class PositionEvent:
    platform: str
    symbol: str
    quantity: float
    average_price: float
    source: str
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class HedgeGroupState:
    hedge_group_id: int
    status: str
    order_events: tuple[OrderEvent, ...] = ()
    fill_events: tuple[FillEvent, ...] = ()


@dataclass(frozen=True)
class GatewayOrderResult:
    success: bool
    order_event: OrderEvent
    fill_events: tuple[FillEvent, ...]
    adapter_result: AdapterOrderResult


class ExecutionGateway(Protocol):
    def submit_order(self, intent: LegOrderIntent, *, paper_latency_ms: int = 0) -> GatewayOrderResult:
        ...

    def cancel_order(self, platform: str, external_order_id: str) -> bool:
        ...

    def query_order(self, platform: str, external_order_id: str) -> dict:
        ...

    def reconcile(self, hedge_group_id: int) -> HedgeGroupState:
        ...


class AdapterExecutionGateway:
    def __init__(self, adapter: ExchangeAdapter) -> None:
        self.adapter = adapter

    def submit_order(self, intent: LegOrderIntent, *, paper_latency_ms: int = 0) -> GatewayOrderResult:
        result = self.adapter.place_order(
            AdapterOrder(
                platform=intent.platform,
                symbol=intent.symbol,
                side=intent.side,
                quantity=intent.quantity,
                venue_symbol=intent.venue_symbol,
                price=intent.price,
                order_type=intent.order_type,
                post_only=intent.post_only,
                reduce_only=intent.reduce_only,
                ttl_seconds=intent.ttl_seconds,
                paper_latency_ms=paper_latency_ms,
            )
        )
        occurred_at = datetime.utcnow()
        order_event = OrderEvent(
            platform=intent.platform,
            symbol=intent.symbol,
            side=intent.side,
            status=result.status,
            external_order_id=result.external_order_id,
            requested_quantity=intent.quantity,
            filled_quantity=result.filled_quantity,
            average_price=result.average_price,
            fee=result.fee,
            message=result.error_message,
            occurred_at=occurred_at,
        )
        fill_events = ()
        if result.success and result.filled_quantity > 0:
            fill_events = (
                FillEvent(
                    platform=intent.platform,
                    symbol=intent.symbol,
                    side=intent.side,
                    quantity=result.filled_quantity,
                    price=result.average_price,
                    fee=result.fee,
                    external_order_id=result.external_order_id,
                    occurred_at=occurred_at,
                ),
            )
        return GatewayOrderResult(result.success, order_event, fill_events, result)

    def cancel_order(self, platform: str, external_order_id: str) -> bool:
        return self.adapter.cancel_order(external_order_id)

    def query_order(self, platform: str, external_order_id: str) -> dict:
        return self.adapter.get_order(external_order_id)

    def reconcile(self, hedge_group_id: int) -> HedgeGroupState:
        return HedgeGroupState(hedge_group_id=hedge_group_id, status="unknown")


def build_execution_gateway(adapter: ExchangeAdapter) -> ExecutionGateway:
    settings = get_settings()
    if getattr(adapter, "platform", "") == "hyperliquid" and getattr(adapter, "simulated", False):
        from app.execution.nautilus_hyperliquid import NautilusHyperliquidSandboxGateway

        return NautilusHyperliquidSandboxGateway(settings=settings)
    if getattr(adapter, "platform", "") == "hyperliquid" and getattr(adapter, "live", False) and settings.nautilus_hyperliquid_enabled:
        from app.execution.nautilus_hyperliquid import NautilusHyperliquidGateway

        return NautilusHyperliquidGateway(settings=settings)
    return AdapterExecutionGateway(adapter)
