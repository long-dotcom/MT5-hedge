from dataclasses import dataclass

from app.adapters.base import AdapterOrder, AdapterOrderResult
from app.adapters.paper import PaperAdapter
from app.config.settings import get_settings


@dataclass(frozen=True)
class MT5DemoCheck:
    allowed: bool
    message: str
    login: str = ""
    server: str = ""


class MT5Adapter(PaperAdapter):
    def __init__(self, live: bool = False, demo: bool = False) -> None:
        super().__init__("mt5", price_bias_bps=20.0)
        self.live = live
        self.demo = bool(demo and not live)
        self.settings = get_settings()

    def place_order(self, order: AdapterOrder) -> AdapterOrderResult:
        if not self._uses_mt5():
            return super().place_order(order)
        if self.live and not self.settings.mt5_live_order_enabled:
            return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, "MT5 实盘下单开关未开启")
        if self.demo and not getattr(self.settings, "mt5_demo_order_enabled", False):
            return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, "MT5 demo 下单开关未开启")
        if order.order_type != "market":
            return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, "MT5 首版仅支持 market 订单")
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception as exc:
            return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, f"MetaTrader5 包不可用: {exc}")

        initialized = _initialize_mt5(mt5, self.settings)
        if not initialized:
            return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, f"MT5 initialize 失败: {mt5.last_error()}")
        if self.demo:
            demo_check = mt5_demo_order_check(mt5, self.settings)
            if not demo_check.allowed:
                return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, demo_check.message)

        symbol = order.venue_symbol or order.symbol
        if not mt5.symbol_select(symbol, True):
            return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, f"MT5 symbol_select 失败: {symbol}; {mt5.last_error()}")
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, f"MT5 tick 不可用: {symbol}")

        side = order.side.lower()
        if side not in {"buy", "sell"}:
            return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, f"MT5 不支持的方向: {order.side}")
        order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
        price = float(tick.ask if side == "buy" else tick.bid)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(order.quantity),
            "type": order_type,
            "price": price,
            "deviation": int(self.settings.mt5_order_deviation_points),
            "magic": int(self.settings.mt5_order_magic),
            "comment": "mt5-hedge",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": _mt5_filling_mode(mt5, symbol),
        }
        if order.reduce_only:
            position = _matching_reduce_position(mt5, symbol, side, float(order.quantity))
            if position is None:
                return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, f"MT5 reduce-only 未找到可平仓持仓: {symbol} {side} {order.quantity}")
            position_volume = float(getattr(position, "volume", 0.0) or 0.0)
            if float(order.quantity) > position_volume + 1e-9:
                return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, f"MT5 reduce-only 平仓数量超过持仓: request={order.quantity}, position={position_volume}")
            request["position"] = int(getattr(position, "ticket", 0) or 0)
        result = mt5.order_send(request)
        if result is None:
            return AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, f"MT5 order_send 无返回: {mt5.last_error()}")
        retcode = int(getattr(result, "retcode", 0))
        done_codes = {mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL, mt5.TRADE_RETCODE_PLACED}
        if retcode not in done_codes:
            comment = getattr(result, "comment", "")
            return AdapterOrderResult(False, str(getattr(result, "order", "")), "rejected", 0.0, 0.0, 0.0, f"MT5 order_send 失败 retcode={retcode}: {comment}")
        filled = float(getattr(result, "volume", order.quantity) or order.quantity)
        avg_price = float(getattr(result, "price", price) or price)
        external_id = str(getattr(result, "order", "") or getattr(result, "deal", ""))
        status = "filled" if retcode == mt5.TRADE_RETCODE_DONE else "partially_filled" if retcode == mt5.TRADE_RETCODE_DONE_PARTIAL else "accepted"
        return AdapterOrderResult(True, external_id, status, filled, avg_price, 0.0)

    def get_positions(self) -> list[dict]:
        if not self._uses_mt5():
            return super().get_positions()
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception:
            return []
        if not _initialize_mt5(mt5, self.settings):
            return []
        try:
            positions = mt5.positions_get()
        except Exception:
            positions = None
        rows = []
        for position in positions or []:
            quantity = float(getattr(position, "volume", 0.0) or 0.0)
            if quantity <= 0:
                continue
            side = "long" if int(getattr(position, "type", 0)) == int(getattr(mt5, "POSITION_TYPE_BUY", 0)) else "short"
            rows.append(
                {
                    "platform": "mt5",
                    "symbol": str(getattr(position, "symbol", "")),
                    "side": side,
                    "quantity": quantity,
                    "ticket": str(getattr(position, "ticket", "") or ""),
                    "entry_price": float(getattr(position, "price_open", 0.0) or 0.0),
                    "mark_price": float(getattr(position, "price_current", 0.0) or 0.0),
                    "unrealized_pnl": float(getattr(position, "profit", 0.0) or 0.0),
                    "margin_used": 0.0,
                    "liquidation_price": None,
                }
            )
        return rows

    def get_order(self, order_id: str) -> dict:
        if not self._uses_mt5():
            return super().get_order(order_id)
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception as exc:
            return {"status": "failed", "external_order_id": order_id, "message": f"MetaTrader5 包不可用: {exc}"}
        if not _initialize_mt5(mt5, self.settings):
            return {"status": "failed", "external_order_id": order_id, "message": f"MT5 initialize 失败: {mt5.last_error()}"}
        ticket = _ticket(order_id)
        if ticket is None:
            return {"status": "failed", "external_order_id": order_id, "message": "MT5 order_id 不是有效 ticket"}

        order = _first_mt5_result(lambda: mt5.orders_get(ticket=ticket))
        if order is None:
            order = _first_mt5_result(lambda: mt5.history_orders_get(ticket=ticket))
        if order is None:
            return {"status": "not_found", "external_order_id": order_id}

        status = _mt5_order_status(mt5, int(getattr(order, "state", -1)))
        filled_quantity = _mt5_order_filled_quantity(order)
        average_price = float(getattr(order, "price_current", 0.0) or getattr(order, "price_done", 0.0) or getattr(order, "price_open", 0.0) or 0.0)
        return {
            "status": status,
            "external_order_id": str(getattr(order, "ticket", order_id)),
            "filled_quantity": filled_quantity,
            "average_price": average_price,
            "message": str(getattr(order, "comment", "") or ""),
        }

    def get_trades(self, order_id: str) -> list[dict]:
        if not self._uses_mt5():
            return super().get_trades(order_id)
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception:
            return []
        if not _initialize_mt5(mt5, self.settings):
            return []
        ticket = _ticket(order_id)
        if ticket is None:
            return []
        deals = _mt5_deals_for_order(mt5, ticket)
        trades = []
        for deal in deals:
            quantity = float(getattr(deal, "volume", 0.0) or 0.0)
            if quantity <= 0:
                continue
            trades.append(
                {
                    "order_id": order_id,
                    "quantity": quantity,
                    "price": float(getattr(deal, "price", 0.0) or 0.0),
                    "fee": float(getattr(deal, "commission", 0.0) or 0.0) + float(getattr(deal, "fee", 0.0) or 0.0),
                }
            )
        return trades

    def _uses_mt5(self) -> bool:
        return bool(self.live or self.demo)


def _initialize_mt5(mt5, settings) -> bool:
    if settings.mt5_login and settings.mt5_password and settings.mt5_server:
        return bool(mt5.initialize(login=int(settings.mt5_login), password=settings.mt5_password, server=settings.mt5_server))
    return bool(mt5.initialize())


def mt5_demo_order_check(mt5, settings) -> MT5DemoCheck:
    if not getattr(settings, "mt5_demo_order_enabled", False):
        return MT5DemoCheck(False, "MT5_DEMO_ORDER_ENABLED 未开启")
    try:
        info = mt5.account_info()
    except Exception as exc:
        return MT5DemoCheck(False, f"MT5 account_info 读取失败: {exc}")
    if not info:
        return MT5DemoCheck(False, f"MT5 account_info 为空: {mt5.last_error()}")

    login = str(getattr(info, "login", "") or "")
    server = str(getattr(info, "server", "") or "")
    trade_mode = int(getattr(info, "trade_mode", -1))
    demo_mode = int(getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0))
    if trade_mode != demo_mode:
        return MT5DemoCheck(False, f"当前 MT5 账户不是 DEMO，禁止 paper 模式下单: login={login} server={server} trade_mode={trade_mode}", login, server)

    expected_login = str(getattr(settings, "mt5_login", "") or "").strip()
    if expected_login and login != expected_login:
        return MT5DemoCheck(False, f"当前 MT5 demo 登录号与 MT5_LOGIN 不匹配: expected={expected_login}, actual={login}", login, server)
    expected_server = str(getattr(settings, "mt5_server", "") or "").strip()
    if expected_server and server.lower() != expected_server.lower():
        return MT5DemoCheck(False, f"当前 MT5 demo 服务器与 MT5_SERVER 不匹配: expected={expected_server}, actual={server}", login, server)
    return MT5DemoCheck(True, f"MT5 demo 账户检查通过: {login} {server}".strip(), login, server)


def _mt5_filling_mode(mt5, symbol: str) -> int:
    info = mt5.symbol_info(symbol)
    filling = int(getattr(info, "filling_mode", 0) or 0) if info else 0
    for mode in ("ORDER_FILLING_IOC", "ORDER_FILLING_RETURN", "ORDER_FILLING_FOK"):
        value = getattr(mt5, mode, None)
        if value is not None and (filling == 0 or filling & int(value)):
            return int(value)
    return int(getattr(mt5, "ORDER_FILLING_IOC", 1))


def _matching_reduce_position(mt5, symbol: str, close_side: str, quantity: float):
    try:
        positions = mt5.positions_get(symbol=symbol)
    except TypeError:
        positions = mt5.positions_get()
    except Exception:
        positions = None
    target_type = getattr(mt5, "POSITION_TYPE_SELL", 1) if close_side == "buy" else getattr(mt5, "POSITION_TYPE_BUY", 0)
    candidates = []
    for position in positions or []:
        if str(getattr(position, "symbol", "")) != symbol:
            continue
        if int(getattr(position, "type", -1)) != int(target_type):
            continue
        volume = float(getattr(position, "volume", 0.0) or 0.0)
        if volume <= 0:
            continue
        candidates.append((abs(volume - quantity), position))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _ticket(order_id: str) -> int | None:
    try:
        return int(str(order_id).strip())
    except (TypeError, ValueError):
        return None


def _first_mt5_result(reader):
    try:
        rows = reader()
    except TypeError:
        return None
    except Exception:
        return None
    if not rows:
        return None
    return rows[0]


def _mt5_order_status(mt5, state: int) -> str:
    if state == int(getattr(mt5, "ORDER_STATE_FILLED", -1000)):
        return "filled"
    if state == int(getattr(mt5, "ORDER_STATE_PARTIAL", -1001)):
        return "partially_filled"
    if state in {
        int(getattr(mt5, "ORDER_STATE_STARTED", -1002)),
        int(getattr(mt5, "ORDER_STATE_PLACED", -1003)),
        int(getattr(mt5, "ORDER_STATE_REQUEST_ADD", -1004)),
        int(getattr(mt5, "ORDER_STATE_REQUEST_MODIFY", -1005)),
    }:
        return "accepted"
    if state in {
        int(getattr(mt5, "ORDER_STATE_CANCELED", -1006)),
        int(getattr(mt5, "ORDER_STATE_EXPIRED", -1007)),
    }:
        return "canceled"
    if state == int(getattr(mt5, "ORDER_STATE_REJECTED", -1008)):
        return "rejected"
    return "unknown"


def _mt5_order_filled_quantity(order) -> float:
    initial = float(getattr(order, "volume_initial", 0.0) or 0.0)
    current = float(getattr(order, "volume_current", 0.0) or 0.0)
    if initial > 0:
        return max(initial - current, 0.0)
    return float(getattr(order, "volume", 0.0) or 0.0)


def _mt5_deals_for_order(mt5, ticket: int) -> tuple:
    readers = [
        lambda: mt5.history_deals_get(order=ticket),
        lambda: mt5.history_deals_get(ticket=ticket),
    ]
    for reader in readers:
        try:
            rows = reader()
        except TypeError:
            rows = None
        except Exception:
            rows = None
        if rows:
            return tuple(rows)
    return ()
