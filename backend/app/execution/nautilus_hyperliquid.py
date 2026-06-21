from dataclasses import dataclass
from datetime import datetime
import json
from threading import Event
from threading import Lock
from threading import Thread
from time import monotonic
from typing import Protocol
from urllib import request
from uuid import uuid4

from app.adapters.base import AdapterOrderResult
from app.config.settings import Settings, get_settings, hyperliquid_execution_info_url
from app.execution.gateway import FillEvent, GatewayOrderResult, HedgeGroupState, LegOrderIntent, OrderEvent


class NautilusSubmitter(Protocol):
    def submit_order(self, intent: LegOrderIntent, instrument_id: str) -> "NautilusSubmitResult":
        ...

    def cancel_order(self, external_order_id: str) -> bool:
        ...

    def query_order(self, external_order_id: str) -> dict:
        ...


@dataclass(frozen=True)
class NautilusSubmitResult:
    status: str
    external_order_id: str
    filled_quantity: float = 0.0
    average_price: float = 0.0
    fee: float = 0.0
    message: str = ""
    occurred_at: datetime | None = None

    @property
    def success(self) -> bool:
        return self.status in {"filled", "accepted", "partially_filled"}


class NautilusHyperliquidGateway:
    def __init__(self, settings: Settings | None = None, submitter: NautilusSubmitter | None = None) -> None:
        self.settings = settings or get_settings()
        self.submitter = submitter or _shared_submitter(self.settings)

    def submit_order(self, intent: LegOrderIntent, *, paper_latency_ms: int = 0) -> GatewayOrderResult:
        instrument_id = hyperliquid_instrument_id(intent.symbol)
        submitted = self.submitter.submit_order(intent, instrument_id)
        occurred_at = submitted.occurred_at or datetime.utcnow()
        order_event = OrderEvent(
            platform=intent.platform,
            symbol=intent.symbol,
            side=intent.side,
            status=submitted.status,
            external_order_id=submitted.external_order_id,
            requested_quantity=intent.quantity,
            filled_quantity=submitted.filled_quantity,
            average_price=submitted.average_price,
            fee=submitted.fee,
            message=submitted.message,
            occurred_at=occurred_at,
        )
        fill_events = ()
        if submitted.filled_quantity > 0:
            fill_events = (
                FillEvent(
                    platform=intent.platform,
                    symbol=intent.symbol,
                    side=intent.side,
                    quantity=submitted.filled_quantity,
                    price=submitted.average_price,
                    fee=submitted.fee,
                    external_order_id=submitted.external_order_id,
                    occurred_at=occurred_at,
                ),
            )
        adapter_result = AdapterOrderResult(
            success=submitted.success,
            external_order_id=submitted.external_order_id,
            status=submitted.status,
            filled_quantity=submitted.filled_quantity,
            average_price=submitted.average_price,
            fee=submitted.fee,
            error_message=submitted.message,
        )
        return GatewayOrderResult(adapter_result.success, order_event, fill_events, adapter_result)

    def cancel_order(self, platform: str, external_order_id: str) -> bool:
        return self.submitter.cancel_order(external_order_id)

    def query_order(self, platform: str, external_order_id: str) -> dict:
        return self.submitter.query_order(external_order_id)

    def query_account_orders(self, platform: str) -> list[dict]:
        return _query_hyperliquid_account_order_snapshots(self.settings)

    def reconcile(self, hedge_group_id: int) -> HedgeGroupState:
        return HedgeGroupState(hedge_group_id=hedge_group_id, status="external_reconcile_required")


_SUBMITTER_LOCK = Lock()
_SUBMITTER: "NautilusTradingNodeSubmitter | None" = None


def _shared_submitter(settings: Settings) -> "NautilusTradingNodeSubmitter":
    global _SUBMITTER
    with _SUBMITTER_LOCK:
        if _SUBMITTER is None:
            _SUBMITTER = NautilusTradingNodeSubmitter(settings)
        return _SUBMITTER


class NautilusTradingNodeSubmitter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._node = None
        self._node_thread: Thread | None = None
        self._strategy = None
        self._lock = Lock()

    def submit_order(self, intent: LegOrderIntent, instrument_id: str) -> NautilusSubmitResult:
        if not self.settings.nautilus_hyperliquid_submit_enabled:
            return NautilusSubmitResult(
                status="failed",
                external_order_id="",
                message="NautilusTrader Hyperliquid 实盘提交开关未开启",
            )
        if intent.order_type not in {"market", "limit"}:
            return NautilusSubmitResult(
                status="failed",
                external_order_id="",
                message=f"NautilusTrader Hyperliquid 暂不支持订单类型: {intent.order_type}",
            )
        if intent.order_type == "limit" and intent.price is None:
            return NautilusSubmitResult(
                status="failed",
                external_order_id="",
                message="NautilusTrader Hyperliquid limit 订单缺少价格",
            )
        try:
            self._ensure_node()
        except Exception as exc:
            return NautilusSubmitResult(
                status="failed",
                external_order_id="",
                message=f"NautilusTrader Hyperliquid 未就绪: {exc}",
            )
        strategy = self._strategy
        if strategy is None:
            return NautilusSubmitResult(status="failed", external_order_id="", message="NautilusTrader bridge strategy 未注册")
        return strategy.submit_intent(intent, instrument_id, timeout_seconds=self.settings.nautilus_hyperliquid_order_timeout_seconds)

    def cancel_order(self, external_order_id: str) -> bool:
        strategy = self._strategy
        if strategy is None:
            return False
        return strategy.cancel_external_order(external_order_id)

    def query_order(self, external_order_id: str) -> dict:
        strategy = self._strategy
        if strategy is not None:
            result = strategy.query_external_order(external_order_id)
            if result.get("status") not in {"not_ready", "not_found", "unknown"}:
                return result
        native = _query_hyperliquid_order_status(self.settings, external_order_id)
        if native.get("status") != "not_supported":
            return native
        if strategy is None:
            return {"status": "not_ready", "external_order_id": external_order_id}
        return result

    def _ensure_node(self):
        with self._lock:
            if self._node is not None:
                return self._node
            node, strategy = self._build_node()
            self._node = node
            self._strategy = strategy
            return node

    def _build_node(self):
        try:
            from nautilus_trader.adapters.hyperliquid import HYPERLIQUID
            from nautilus_trader.adapters.hyperliquid import HyperliquidDataClientConfig
            from nautilus_trader.adapters.hyperliquid import HyperliquidExecClientConfig
            from nautilus_trader.adapters.hyperliquid import HyperliquidLiveDataClientFactory
            from nautilus_trader.adapters.hyperliquid import HyperliquidLiveExecClientFactory
            from nautilus_trader.adapters.hyperliquid import HyperliquidProductType
            from nautilus_trader.config import InstrumentProviderConfig
            from nautilus_trader.config import TradingNodeConfig
            from nautilus_trader.core.nautilus_pyo3 import HyperliquidEnvironment
            from nautilus_trader.live.node import TradingNode
            from nautilus_trader.model.identifiers import TraderId
        except Exception as exc:
            raise RuntimeError("未安装 nautilus_trader 或 Hyperliquid adapter 不可导入") from exc

        environment = _nautilus_environment(HyperliquidEnvironment, self.settings.nautilus_hyperliquid_environment)
        product_types = _nautilus_product_types(HyperliquidProductType, self.settings.nautilus_hyperliquid_product_types)
        provider = InstrumentProviderConfig(load_all=True)
        config = TradingNodeConfig(
            trader_id=TraderId(self.settings.nautilus_trader_id),
            data_clients={
                HYPERLIQUID: HyperliquidDataClientConfig(
                    environment=environment,
                    instrument_provider=provider,
                    product_types=product_types,
                ),
            },
            exec_clients={
                HYPERLIQUID: HyperliquidExecClientConfig(
                    private_key=self.settings.nautilus_hyperliquid_private_key or None,
                    vault_address=self.settings.nautilus_hyperliquid_vault_address or None,
                    account_address=_nautilus_account_address(self.settings),
                    environment=environment,
                    instrument_provider=provider,
                    product_types=product_types,
                    normalize_prices=True,
                ),
            },
        )
        node = TradingNode(config=config)
        node.add_data_client_factory(HYPERLIQUID, HyperliquidLiveDataClientFactory)
        node.add_exec_client_factory(HYPERLIQUID, HyperliquidLiveExecClientFactory)
        strategy = NautilusHedgeBridgeStrategy()
        node.trader.add_strategy(strategy)
        node.build()
        if not node.is_running():
            self._node_thread = _start_node(node, "nautilus-hyperliquid-exec-node")
        return node, strategy


class NautilusHedgeBridgeStrategy:
    def __new__(cls):
        try:
            from nautilus_trader.trading.config import StrategyConfig
            from nautilus_trader.trading.strategy import Strategy
        except Exception:
            return super().__new__(cls)

        class _BridgeStrategy(Strategy):
            def __init__(self) -> None:
                super().__init__(StrategyConfig(strategy_id="MT5-HEDGE-BRIDGE-001"))
                self._pending = {}
                self._completed = {}

            def submit_intent(self, intent: LegOrderIntent, instrument_id: str, timeout_seconds: float) -> NautilusSubmitResult:
                from nautilus_trader.model.enums import OrderSide
                from nautilus_trader.model.enums import TimeInForce
                from nautilus_trader.model.identifiers import ClientOrderId
                from nautilus_trader.model.identifiers import InstrumentId

                iid = InstrumentId.from_str(instrument_id)
                order_side = OrderSide.BUY if intent.side.lower() == "buy" else OrderSide.SELL
                quantity = self._quantity(iid, intent.quantity)
                client_order_id = str(uuid4())
                nautilus_client_order_id = ClientOrderId(client_order_id)
                done = Event()
                self._pending[client_order_id] = {
                    "event": done,
                    "intent": intent,
                    "result": NautilusSubmitResult(status="submitted", external_order_id=client_order_id),
                    "filled_quantity": 0.0,
                    "average_price": 0.0,
                    "fee": 0.0,
                }
                try:
                    if intent.order_type == "limit":
                        reduce_kwargs = {"reduce_only": True} if intent.reduce_only else {}
                        order = self.order_factory.limit(
                            instrument_id=iid,
                            order_side=order_side,
                            quantity=quantity,
                            price=self._price(iid, float(intent.price)),
                            time_in_force=TimeInForce.GTC,
                            post_only=bool(intent.post_only),
                            client_order_id=nautilus_client_order_id,
                            **reduce_kwargs,
                        )
                    else:
                        reduce_kwargs = {"reduce_only": True} if intent.reduce_only else {}
                        order = self.order_factory.market(
                            instrument_id=iid,
                            order_side=order_side,
                            quantity=quantity,
                            time_in_force=TimeInForce.GTC,
                            client_order_id=nautilus_client_order_id,
                            **reduce_kwargs,
                        )
                    self.submit_order(order)
                except TypeError as exc:
                    self._pending.pop(client_order_id, None)
                    if intent.reduce_only and _type_error_mentions_keyword(exc, "reduce_only"):
                        return NautilusSubmitResult(status="failed", external_order_id=client_order_id, message="NautilusTrader order_factory 不支持 reduce_only 参数")
                    return self._submit_without_explicit_client_id(intent, iid, order_side, quantity, timeout_seconds)
                except Exception as exc:
                    self._pending.pop(client_order_id, None)
                    return NautilusSubmitResult(status="failed", external_order_id=client_order_id, message=f"NautilusTrader submit_order 失败: {exc}")

                return self._wait_result(client_order_id, timeout_seconds)

            def cancel_external_order(self, external_order_id: str) -> bool:
                order = self.cache.order(external_order_id) if hasattr(self.cache, "order") else None
                if not order:
                    return False
                self.cancel_order(order)
                return True

            def query_external_order(self, external_order_id: str) -> dict:
                result = self._completed.get(external_order_id) or self._pending.get(external_order_id)
                if result:
                    snapshot = result.get("result")
                    return {
                        "status": getattr(snapshot, "status", "unknown"),
                        "external_order_id": external_order_id,
                        "filled_quantity": getattr(snapshot, "filled_quantity", 0.0),
                        "average_price": getattr(snapshot, "average_price", 0.0),
                    }
                order = self.cache.order(external_order_id) if hasattr(self.cache, "order") else None
                return {"status": str(getattr(order, "status", "unknown")), "external_order_id": external_order_id}

            def on_order_accepted(self, event) -> None:
                self._record_order_event(event, "accepted")

            def on_order_rejected(self, event) -> None:
                self._record_order_event(event, "rejected")

            def on_order_denied(self, event) -> None:
                self._record_order_event(event, "rejected")

            def on_order_filled(self, event) -> None:
                client_order_id = _event_client_order_id(event)
                state = self._pending.get(client_order_id)
                if not state:
                    return
                quantity = _event_float(event, "last_qty", "quantity", "filled_qty")
                price = _event_float(event, "last_px", "price", "avg_px")
                state["filled_quantity"] += quantity
                if price > 0:
                    state["average_price"] = price
                state["fee"] += _event_float(event, "commission", "fee")
                state["result"] = NautilusSubmitResult(
                    status="filled",
                    external_order_id=_event_external_order_id(event) or client_order_id,
                    filled_quantity=state["filled_quantity"],
                    average_price=state["average_price"],
                    fee=state["fee"],
                    occurred_at=datetime.utcnow(),
                )
                state["event"].set()

            def _record_order_event(self, event, status: str) -> None:
                client_order_id = _event_client_order_id(event)
                state = self._pending.get(client_order_id)
                if not state:
                    return
                state["result"] = NautilusSubmitResult(
                    status=status,
                    external_order_id=_event_external_order_id(event) or client_order_id,
                    filled_quantity=state["filled_quantity"],
                    average_price=state["average_price"],
                    fee=state["fee"],
                    message=_event_message(event),
                    occurred_at=datetime.utcnow(),
                )
                if status in {"rejected", "failed"}:
                    state["event"].set()

            def _wait_result(self, client_order_id: str, timeout_seconds: float) -> NautilusSubmitResult:
                deadline = monotonic() + max(float(timeout_seconds), 0.1)
                state = self._pending[client_order_id]
                while monotonic() < deadline:
                    remaining = deadline - monotonic()
                    state["event"].wait(min(0.1, max(remaining, 0.0)))
                    result = state["result"]
                    if result.status in {"filled", "partially_filled", "rejected", "failed"}:
                        self._completed[client_order_id] = self._pending.pop(client_order_id)
                        return result
                    if result.status == "accepted":
                        return result
                result = state["result"]
                return NautilusSubmitResult(
                    status="accepted" if result.status in {"submitted", "accepted"} else result.status,
                    external_order_id=result.external_order_id,
                    filled_quantity=result.filled_quantity,
                    average_price=result.average_price,
                    fee=result.fee,
                    message="NautilusTrader 订单已提交，等待最终成交事件超时",
                    occurred_at=datetime.utcnow(),
                )

            def _submit_without_explicit_client_id(self, intent, iid, order_side, quantity, timeout_seconds):
                from nautilus_trader.model.enums import TimeInForce

                try:
                    if intent.order_type == "limit":
                        reduce_kwargs = {"reduce_only": True} if intent.reduce_only else {}
                        order = self.order_factory.limit(
                            instrument_id=iid,
                            order_side=order_side,
                            quantity=quantity,
                            price=self._price(iid, float(intent.price)),
                            time_in_force=TimeInForce.GTC,
                            post_only=bool(intent.post_only),
                            **reduce_kwargs,
                        )
                    else:
                        reduce_kwargs = {"reduce_only": True} if intent.reduce_only else {}
                        order = self.order_factory.market(
                            instrument_id=iid,
                            order_side=order_side,
                            quantity=quantity,
                            time_in_force=TimeInForce.GTC,
                            **reduce_kwargs,
                        )
                    client_order_id = str(order.client_order_id)
                    done = Event()
                    self._pending[client_order_id] = {
                        "event": done,
                        "intent": intent,
                        "result": NautilusSubmitResult(status="submitted", external_order_id=client_order_id),
                        "filled_quantity": 0.0,
                        "average_price": 0.0,
                        "fee": 0.0,
                    }
                    self.submit_order(order)
                    return self._wait_result(client_order_id, timeout_seconds)
                except TypeError as exc:
                    message = "NautilusTrader order_factory 不支持 reduce_only 参数" if intent.reduce_only and _type_error_mentions_keyword(exc, "reduce_only") else f"NautilusTrader submit_order 失败: {exc}"
                    return NautilusSubmitResult(status="failed", external_order_id="", message=message)
                except Exception as exc:
                    return NautilusSubmitResult(status="failed", external_order_id="", message=f"NautilusTrader submit_order 失败: {exc}")

            def _instrument(self, instrument_id):
                try:
                    return self.cache.instrument(instrument_id)
                except Exception:
                    return None

            def _quantity(self, instrument_id, value: float):
                from nautilus_trader.model.objects import Quantity

                instrument = self._instrument(instrument_id)
                if instrument and hasattr(instrument, "make_qty"):
                    return instrument.make_qty(value)
                return Quantity.from_str(str(value))

            def _price(self, instrument_id, value: float):
                from nautilus_trader.model.objects import Price

                instrument = self._instrument(instrument_id)
                if instrument and hasattr(instrument, "make_price"):
                    return instrument.make_price(value)
                return Price.from_str(str(value))

        return _BridgeStrategy()


def hyperliquid_instrument_id(symbol: str) -> str:
    value = symbol.strip()
    if not value:
        return "UNKNOWN-USD-PERP.HYPERLIQUID"
    if value.endswith(".HYPERLIQUID"):
        return value
    if value.endswith("-SPOT") or value.endswith("-PERP") or value.endswith("-OUTCOME"):
        return f"{value}.HYPERLIQUID"
    if ":" in value:
        dex, asset = value.split(":", 1)
        return f"{dex}:{asset}-USD-PERP.HYPERLIQUID"
    return f"{value}-USD-PERP.HYPERLIQUID"


def _nautilus_environment(enum_cls, value: str):
    normalized = value.strip().upper()
    return getattr(enum_cls, "MAINNET") if normalized == "MAINNET" else getattr(enum_cls, "TESTNET")


def _nautilus_product_types(enum_cls, raw_value: str):
    names = [item.strip().upper() for item in raw_value.split(",") if item.strip()]
    if not names:
        names = ["PERP", "PERP_HIP3"]
    return tuple(getattr(enum_cls, name) for name in names)


def _nautilus_account_address(settings: Settings) -> str | None:
    return settings.hyperliquid_account_address or None


def _event_client_order_id(event) -> str:
    for name in ("client_order_id", "order_id"):
        value = getattr(event, name, None)
        if value:
            return str(value)
    order = getattr(event, "order", None)
    value = getattr(order, "client_order_id", None) if order else None
    return str(value) if value else ""


def _type_error_mentions_keyword(exc: TypeError, keyword: str) -> bool:
    return keyword in str(exc)


def _event_external_order_id(event) -> str:
    for name in ("venue_order_id", "exchange_order_id", "order_id"):
        value = getattr(event, name, None)
        if value:
            return str(value)
    return _event_client_order_id(event)


def _event_float(event, *names: str) -> float:
    for name in names:
        value = getattr(event, name, None)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            raw = getattr(value, "as_double", None)
            if callable(raw):
                return float(raw())
    return 0.0


def _event_message(event) -> str:
    for name in ("reason", "message", "error", "comment"):
        value = getattr(event, name, None)
        if value:
            return str(value)
    return ""


def _query_hyperliquid_order_status(settings: Settings, external_order_id: str) -> dict:
    user = settings.hyperliquid_account_address or settings.nautilus_hyperliquid_vault_address
    if not user:
        return {"status": "not_supported", "external_order_id": external_order_id, "message": "未配置 Hyperliquid 用户地址，无法主动查询外部订单"}
    oid = _hyperliquid_oid(external_order_id)
    if oid is None:
        return {"status": "not_supported", "external_order_id": external_order_id, "message": "external_order_id 不能转换为 Hyperliquid oid/cloid"}
    try:
        payload = _hyperliquid_info(hyperliquid_execution_info_url(settings), {"type": "orderStatus", "user": user, "oid": oid})
    except Exception as exc:
        return {"status": "not_ready", "external_order_id": external_order_id, "message": f"Hyperliquid orderStatus 查询失败: {exc}"}

    mapped = _map_hyperliquid_order_status(payload, external_order_id)
    if mapped["status"] in {"filled", "partially_filled"}:
        fills = _query_hyperliquid_fills(settings, user, oid)
        if fills:
            mapped.update(fills)
    return mapped


def _query_hyperliquid_fills(settings: Settings, user: str, oid) -> dict:
    try:
        payload = _hyperliquid_info(hyperliquid_execution_info_url(settings), {"type": "userFills", "user": user, "aggregateByTime": False})
    except Exception:
        return {}
    fills = payload if isinstance(payload, list) else []
    matched = [fill for fill in fills if str(fill.get("oid", "")) == str(oid)]
    if not matched:
        return {}
    quantity = sum(float(fill.get("sz", 0.0) or 0.0) for fill in matched)
    notional = sum(float(fill.get("sz", 0.0) or 0.0) * float(fill.get("px", 0.0) or 0.0) for fill in matched)
    fee = sum(float(fill.get("fee", 0.0) or 0.0) for fill in matched)
    return {
        "filled_quantity": quantity,
        "average_price": notional / quantity if quantity > 0 else 0.0,
        "fee": fee,
    }


def _query_hyperliquid_account_order_snapshots(settings: Settings) -> list[dict]:
    user = settings.hyperliquid_account_address or settings.nautilus_hyperliquid_vault_address
    if not user:
        return []
    snapshots: dict[str, dict] = {}
    try:
        open_orders = _hyperliquid_info(hyperliquid_execution_info_url(settings), {"type": "openOrders", "user": user})
    except Exception:
        open_orders = []
    for item in open_orders if isinstance(open_orders, list) else []:
        snapshot = _map_hyperliquid_open_order_snapshot(item)
        external_order_id = snapshot.get("external_order_id")
        if external_order_id:
            snapshots[str(external_order_id)] = snapshot

    try:
        fills = _hyperliquid_info(hyperliquid_execution_info_url(settings), {"type": "userFills", "user": user, "aggregateByTime": False})
    except Exception:
        fills = []
    for item in fills if isinstance(fills, list) else []:
        oid = str(item.get("oid") or item.get("cloid") or "")
        if not oid:
            continue
        aliases = [str(value) for value in (item.get("oid"), item.get("cloid")) if value]
        existing = snapshots.get(oid, {})
        quantity = float(item.get("sz", 0.0) or 0.0)
        price = float(item.get("px", 0.0) or 0.0)
        previous_quantity = float(existing.get("filled_quantity", 0.0) or 0.0)
        previous_notional = previous_quantity * float(existing.get("average_price", 0.0) or 0.0)
        total_quantity = previous_quantity + quantity
        total_notional = previous_notional + quantity * price
        snapshots[oid] = {
            **existing,
            "status": "filled",
            "external_order_id": oid,
            "external_order_ids": list(dict.fromkeys([*aliases, *existing.get("external_order_ids", [])])),
            "symbol": str(item.get("coin") or existing.get("symbol") or ""),
            "side": _hyperliquid_side(str(item.get("side") or existing.get("side") or "")),
            "filled_quantity": total_quantity,
            "quantity": total_quantity,
            "average_price": total_notional / total_quantity if total_quantity > 0 else 0.0,
            "fee": float(existing.get("fee", 0.0) or 0.0) + float(item.get("fee", 0.0) or 0.0),
            "timestamp_ms": int(item.get("time") or existing.get("timestamp_ms") or 0),
            "message": "Hyperliquid account userFills 恢复",
        }
    return list(snapshots.values())


def _map_hyperliquid_open_order_snapshot(item: dict) -> dict:
    external_order_id = str(item.get("oid") or item.get("cloid") or "")
    aliases = [str(value) for value in (item.get("oid"), item.get("cloid")) if value]
    return {
        "status": "accepted",
        "external_order_id": external_order_id,
        "external_order_ids": aliases,
        "symbol": str(item.get("coin") or ""),
        "side": _hyperliquid_side(str(item.get("side") or "")),
        "quantity": float(item.get("sz", 0.0) or 0.0),
        "filled_quantity": 0.0,
        "average_price": float(item.get("limitPx", 0.0) or 0.0),
        "fee": 0.0,
        "timestamp_ms": int(item.get("timestamp") or 0),
        "message": "Hyperliquid account openOrders 恢复",
    }


def _hyperliquid_side(side: str) -> str:
    value = side.strip().lower()
    if value in {"b", "buy"}:
        return "buy"
    if value in {"a", "ask", "s", "sell"}:
        return "sell"
    return value


def _hyperliquid_info(url: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _hyperliquid_oid(external_order_id: str):
    value = str(external_order_id).strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if value.startswith("0x") and len(value) == 34:
        return value
    try:
        from nautilus_trader.core import nautilus_pyo3
        from nautilus_trader.core.nautilus_pyo3.model import ClientOrderId

        return nautilus_pyo3.hyperliquid_cloid_from_client_order_id(ClientOrderId(value))
    except Exception:
        return None


def _map_hyperliquid_order_status(payload, external_order_id: str) -> dict:
    if not isinstance(payload, dict):
        return {"status": "not_ready", "external_order_id": external_order_id, "message": "Hyperliquid orderStatus 返回格式异常"}
    if payload.get("status") == "unknown":
        return {"status": "not_found", "external_order_id": external_order_id, "message": "Hyperliquid 未找到该订单"}
    raw_status = ""
    order_data = payload.get("order")
    if isinstance(order_data, dict):
        raw_status = str(order_data.get("status") or "")
        nested = order_data.get("order")
        if isinstance(nested, dict):
            raw_status = raw_status or str(nested.get("status") or "")
    status = _normalize_hyperliquid_order_status(raw_status)
    return {
        "status": status,
        "external_order_id": external_order_id,
        "message": f"Hyperliquid orderStatus={raw_status or payload.get('status', '')}",
    }


def _normalize_hyperliquid_order_status(status: str) -> str:
    value = status.strip()
    if value == "filled":
        return "filled"
    if value == "open":
        return "accepted"
    if value == "canceled" or value.endswith("Canceled"):
        return "canceled"
    if value == "rejected" or value.endswith("Rejected"):
        return "rejected"
    if value:
        return "failed" if "Rejected" in value or "Canceled" in value else value
    return "not_ready"


def _start_node(node, name: str) -> Thread | None:
    run = getattr(node, "run", None)
    if callable(run):
        thread = Thread(target=run, name=name, daemon=True)
        thread.start()
        return thread
    run_async = getattr(node, "run_async", None)
    if callable(run_async):
        run_async()
    return None
