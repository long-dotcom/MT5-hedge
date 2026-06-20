import asyncio
import json
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.analytics.funding import funding_history
from app.analytics.lead_lag import lead_lag_report
from app.analytics.spreads import downsample_spreads, load_spread_points, summarize_spreads
from app.accounts.sync import latest_account_snapshots, sync_account_snapshots
from app.auth.dependencies import get_current_user, require_admin
from app.auth.security import create_access_token, decode_access_token, verify_password
from app.db.models import (
    AccountSnapshot,
    Alert,
    ArbitrageOpportunity,
    AuditLog,
    Fill,
    HedgeGroup,
    HedgeGroupEvent,
    MarketSnapshot,
    Order,
    Position,
    RiskEvent,
    RiskSetting,
    SpreadBucket,
    SpreadCurrent,
    SpreadSnapshot,
    StrategySetting,
    SymbolMapping,
    SystemLog,
    SystemSetting,
    User,
)
from app.db.session import SessionLocal, get_db
from app.execution.engine import close_hedge_group, open_hedge_group
from app.execution.readiness import live_execution_readiness
from app.execution.reconciler import run_execution_reconcile
from app.market.scanner import run_scan
from app.market.quotes import quote_cache
from app.market.mt5_sessions import as_session_dict, mt5_session_state
from app.config.settings import get_settings
from app.schemas import AdoptPositionIn, CloseHedgeGroupIn, LiveTradingIn, LoginRequest, RiskModeIn, RiskSettingsIn, StrategySettingsIn, SymbolMappingIn, TokenResponse


router = APIRouter(prefix="/api")


def as_dict(row: Any) -> dict[str, Any]:
    data = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    return data


def json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def audit(db: Session, user_id: int | None, action: str, resource: str, detail: str = "", request: Request | None = None) -> None:
    db.add(AuditLog(user_id=user_id, action=action, resource=resource, detail=detail, ip_address=request.client.host if request and request.client else ""))


@router.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.query(User).filter(User.username == payload.username, User.is_active.is_(True)).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    audit(db, user.id, "login", "auth", "管理员登录", request)
    db.commit()
    return TokenResponse(access_token=create_access_token(user.username, {"role": user.role}), user={"username": user.username, "role": user.role})


@router.post("/auth/logout")
def logout(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, str]:
    audit(db, user.id, "logout", "auth")
    db.commit()
    return {"status": "ok"}


@router.get("/auth/me")
def me(user: User = Depends(get_current_user)) -> dict[str, Any]:
    return {"username": user.username, "role": user.role}


@router.get("/dashboard/summary")
def dashboard_summary(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    latest_accounts = latest_account_snapshots(db)
    equity = sum(row.equity for row in latest_accounts)
    open_groups = db.query(HedgeGroup).filter(HedgeGroup.status.in_(["opening", "open", "open_partial", "closing", "manual_intervention"])).count()
    alerts = db.query(Alert).filter(Alert.acknowledged.is_(False)).count()
    risk = db.query(RiskSetting).first()
    return {"equity": equity, "today_pnl": 0.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0, "risk_mode": risk.mode if risk else "normal", "open_hedge_groups": open_groups, "unread_alerts": alerts}


@router.get("/dashboard/equity-curve")
def equity_curve(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.query(AccountSnapshot).order_by(AccountSnapshot.created_at).limit(100).all()
    return [{"time": row.created_at.isoformat(), "equity": row.equity, "platform": row.platform} for row in rows]


@router.get("/dashboard/risk-summary")
def risk_summary(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    risk = db.query(RiskSetting).first()
    latest_events = db.query(RiskEvent).order_by(desc(RiskEvent.created_at)).limit(5).all()
    return {"risk": as_dict(risk) if risk else {}, "events": [as_dict(row) for row in latest_events]}


@router.post("/markets/scan")
def scan(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"created": run_scan(db)}


@router.get("/markets/symbols")
def market_symbols(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [as_dict(row) for row in db.query(SymbolMapping).order_by(SymbolMapping.symbol).all()]


@router.get("/markets/quotes")
def market_quotes(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = []
    for mapping in db.query(SymbolMapping).order_by(SymbolMapping.symbol).all():
        for platform in ("hyperliquid", "mt5"):
            quote = quote_cache.latest(platform, mapping.symbol)
            if quote:
                rows.append(
                    {
                        "platform": platform,
                        "symbol": mapping.symbol,
                        "bid": quote.bid,
                        "ask": quote.ask,
                        "depth_notional": quote.depth_notional,
                        "source": quote.source,
                        "sequence": quote.sequence,
                        "local_recv_ts": quote.local_recv_ts,
                        "exchange_ts": quote.exchange_ts,
                    }
                )
    return rows


@router.get("/markets/trading-sessions")
def trading_sessions(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.query(SymbolMapping).order_by(SymbolMapping.symbol).all()
    return [as_session_dict(mt5_session_state(row)) for row in rows]


@router.get("/markets/spreads")
def spreads(_: User = Depends(get_current_user), db: Session = Depends(get_db), page: int = 1, page_size: int = 20, symbol: str = "") -> dict[str, Any]:
    query = db.query(SpreadCurrent)
    if symbol:
        query = query.filter(SpreadCurrent.symbol.contains(symbol.upper()))
    total = query.count()
    rows = query.order_by(SpreadCurrent.symbol).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [as_dict(row) for row in rows]}


@router.get("/stream")
async def stream(token: str) -> StreamingResponse:
    try:
        payload = decode_access_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="登录已失效") from exc
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == payload.get("sub"), User.is_active.is_(True)).first()
        if not user:
            raise HTTPException(status_code=401, detail="用户不存在或已禁用")
    finally:
        db.close()

    async def event_generator():
        interval = max(get_settings().stream_interval_ms, 250) / 1000
        while True:
            session = SessionLocal()
            try:
                event = _stream_snapshot(session)
                yield f"event: snapshot\ndata: {json.dumps(event, default=json_default, separators=(',', ':'))}\n\n"
            finally:
                session.close()
            await asyncio.sleep(interval)

    return StreamingResponse(event_generator(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _stream_snapshot(db: Session) -> dict[str, Any]:
    spread_rows = db.query(SpreadCurrent).order_by(SpreadCurrent.symbol).all()
    opportunity_rows = db.query(ArbitrageOpportunity).filter(ArbitrageOpportunity.status.in_(["candidate", "executable", "executing"])).order_by(desc(ArbitrageOpportunity.updated_at)).limit(50).all()
    account_rows = latest_account_snapshots(db)
    latest_bucket = db.query(SpreadBucket).order_by(desc(SpreadBucket.id)).first()
    return {
        "spreads": {"total": len(spread_rows), "items": [as_dict(row) for row in spread_rows]},
        "opportunities": {"total": len(opportunity_rows), "items": [as_dict(row) for row in opportunity_rows]},
        "accounts": [as_dict(row) for row in account_rows],
        "latest_bucket_id": latest_bucket.id if latest_bucket else 0,
    }


@router.get("/analytics/spread-summary")
def spread_summary(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    symbol: str = "BTC",
    direction: str = "long_mt5_short_hyperliquid",
    range: str = "1h",
) -> dict[str, Any]:
    points = load_spread_points(db, symbol, direction, range)
    return {"symbol": symbol.upper(), "direction": direction, **summarize_spreads(points, range)}


@router.get("/analytics/spread-series")
def spread_series(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    symbol: str = "BTC",
    direction: str = "long_mt5_short_hyperliquid",
    range: str = "1h",
) -> dict[str, Any]:
    points = load_spread_points(db, symbol, direction, range)
    summary = summarize_spreads(points, range)
    return {
        "symbol": symbol.upper(),
        "direction": direction,
        "range": summary["range"],
        "summary": summary,
        "items": downsample_spreads(points, range),
    }


@router.get("/analytics/funding-series")
def funding_series(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    symbol: str = "BTC",
    range: str = "7d",
    bucket: str = "day",
) -> dict[str, Any]:
    return funding_history(db, symbol, range, bucket)


@router.get("/analytics/lead-lag")
def lead_lag(
    _: User = Depends(get_current_user),
    symbol: str = "JP225",
    window_seconds: int = 300,
    threshold_bps: float = 3.0,
    min_move: float = 0.0,
    follow_ratio: float = 0.5,
    max_lag_ms: int = 2000,
) -> dict[str, Any]:
    return lead_lag_report(symbol, window_seconds, threshold_bps, min_move, follow_ratio, max_lag_ms)


@router.get("/opportunities")
def opportunities(_: User = Depends(get_current_user), db: Session = Depends(get_db), page: int = 1, page_size: int = 20) -> dict[str, Any]:
    query = db.query(ArbitrageOpportunity).filter(ArbitrageOpportunity.status.in_(["candidate", "executable", "executing"]))
    total = query.count()
    rows = query.order_by(desc(ArbitrageOpportunity.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [as_dict(row) for row in rows]}


@router.get("/opportunities/{opportunity_id}")
def opportunity_detail(opportunity_id: int, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(ArbitrageOpportunity, opportunity_id)
    if not row:
        raise HTTPException(status_code=404, detail="机会不存在")
    return as_dict(row)


@router.post("/opportunities/{opportunity_id}/execute")
def execute_opportunity(opportunity_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        group = open_hedge_group(db, opportunity_id, source=user.username)
        return as_dict(group)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/hedge-groups")
def hedge_groups(_: User = Depends(get_current_user), db: Session = Depends(get_db), page: int = 1, page_size: int = 20) -> dict[str, Any]:
    query = db.query(HedgeGroup)
    total = query.count()
    rows = query.order_by(desc(HedgeGroup.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [as_dict(row) for row in rows]}


@router.get("/hedge-groups/{group_id}")
def hedge_group_detail(group_id: int, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    group = db.get(HedgeGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="对冲组不存在")
    data = as_dict(group)
    data["events"] = [as_dict(row) for row in group.events]
    data["orders"] = [as_dict(row) for row in db.query(Order).filter(Order.hedge_group_id == group_id).all()]
    return data


@router.post("/hedge-groups/{group_id}/close")
def close_group(group_id: int, payload: CloseHedgeGroupIn, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        group = close_hedge_group(db, group_id, payload.reason)
        audit(db, user.id, "close_hedge_group", "hedge_group", str(group_id))
        db.commit()
        return as_dict(group)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/hedge-groups/{group_id}/mark-manual")
def mark_manual(group_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    group = db.get(HedgeGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="对冲组不存在")
    group.status = "manual_intervention"
    audit(db, user.id, "mark_manual", "hedge_group", str(group_id))
    db.commit()
    return as_dict(group)


@router.get("/accounts")
def accounts(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = sync_account_snapshots(db)
    return [as_dict(row) for row in rows]


@router.get("/accounts/snapshots")
def account_snapshots(_: User = Depends(get_current_user), db: Session = Depends(get_db), page: int = 1, page_size: int = 20) -> dict[str, Any]:
    query = db.query(AccountSnapshot)
    total = query.count()
    rows = query.order_by(desc(AccountSnapshot.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [as_dict(row) for row in rows]}


@router.get("/positions")
def positions(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [as_dict(row) for row in db.query(Position).order_by(desc(Position.created_at)).all()]


@router.post("/positions/{position_id}/adopt")
def adopt_position(position_id: int, payload: AdoptPositionIn, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    position = db.get(Position, position_id)
    if not position:
        raise HTTPException(status_code=404, detail="仓位不存在")
    if position.platform not in {"hyperliquid", "mt5"}:
        raise HTTPException(status_code=400, detail="只支持接管 Hyperliquid/MT5 live 仓位")
    if abs(position.quantity) <= 0:
        raise HTTPException(status_code=400, detail="仓位数量为 0，不能接管")
    mapping = _mapping_for_position(db, position, payload.symbol)
    if not mapping:
        raise HTTPException(status_code=400, detail="找不到该仓位对应的品种映射，请先配置 symbol mapping 或在请求中指定内部 symbol")
    if _position_has_live_group(db, position, mapping):
        raise HTTPException(status_code=400, detail="该仓位已匹配 live 对冲组，不能重复接管")

    direction = _direction_for_position(position)
    hyperliquid_quantity = position.quantity if position.platform == "hyperliquid" else 0.0
    mt5_quantity = position.quantity if position.platform == "mt5" else 0.0
    notional = abs(position.quantity * (position.mark_price or position.entry_price or 0.0))
    group = HedgeGroup(
        symbol=mapping.symbol,
        direction=direction,
        status="manual_intervention",
        execution_mode="live",
        notional=notional,
        quantity=abs(position.quantity),
        hyperliquid_quantity=hyperliquid_quantity,
        mt5_quantity=mt5_quantity,
        unrealized_pnl=position.unrealized_pnl,
        close_reason=f"外部仓位接管: {payload.reason}",
        source=user.username,
        opened_at=position.created_at,
    )
    db.add(group)
    db.flush()
    detail = f"{position.platform}:{position.symbol}:{position.side}:{position.quantity}"
    db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="adopted_external_position", detail=detail))
    audit(db, user.id, "adopt_position", "position", f"{position_id}->{group.id}: {detail}")
    db.commit()
    db.refresh(group)
    return as_dict(group)


def _mapping_for_position(db: Session, position: Position, requested_symbol: str = "") -> SymbolMapping | None:
    symbol = requested_symbol.strip().upper()
    if symbol:
        return db.query(SymbolMapping).filter(SymbolMapping.symbol == symbol).first()
    if position.platform == "hyperliquid":
        return (
            db.query(SymbolMapping)
            .filter((SymbolMapping.hyperliquid_symbol == position.symbol) | (SymbolMapping.symbol == position.symbol))
            .first()
        )
    return (
        db.query(SymbolMapping)
        .filter((SymbolMapping.mt5_symbol == position.symbol) | (SymbolMapping.symbol == position.symbol))
        .first()
    )


def _direction_for_position(position: Position) -> str:
    side = position.side.lower()
    if position.platform == "hyperliquid":
        return "long_hyperliquid_short_mt5" if side == "long" else "long_mt5_short_hyperliquid"
    return "long_mt5_short_hyperliquid" if side == "long" else "long_hyperliquid_short_mt5"


def _position_has_live_group(db: Session, position: Position, mapping: SymbolMapping) -> bool:
    groups = db.query(HedgeGroup).filter(HedgeGroup.execution_mode == "live").all()
    symbols = {
        "hyperliquid": {mapping.symbol, mapping.hyperliquid_symbol},
        "mt5": {mapping.symbol, mapping.mt5_symbol},
    }
    if position.symbol not in symbols.get(position.platform, set()):
        return False
    return any(group.symbol == mapping.symbol and group.status != "closed" for group in groups)


@router.post("/execution/reconcile")
def execution_reconcile(user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    changed = run_execution_reconcile(db)
    audit(db, user.id, "run_execution_reconcile", "execution", str(changed))
    db.commit()
    return {"status": "ok", "changed": changed}


@router.get("/orders")
def orders(_: User = Depends(get_current_user), db: Session = Depends(get_db), page: int = 1, page_size: int = 20) -> dict[str, Any]:
    query = db.query(Order)
    total = query.count()
    rows = query.order_by(desc(Order.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [as_dict(row) for row in rows]}


@router.get("/fills")
def fills(_: User = Depends(get_current_user), db: Session = Depends(get_db), page: int = 1, page_size: int = 20) -> dict[str, Any]:
    query = db.query(Fill)
    total = query.count()
    rows = query.order_by(desc(Fill.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [as_dict(row) for row in rows]}


@router.get("/risk/status")
def risk_status(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    risk = db.query(RiskSetting).first()
    return as_dict(risk) if risk else {}


@router.get("/risk/events")
def risk_events(_: User = Depends(get_current_user), db: Session = Depends(get_db), page: int = 1, page_size: int = 20) -> dict[str, Any]:
    query = db.query(RiskEvent)
    total = query.count()
    rows = query.order_by(desc(RiskEvent.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [as_dict(row) for row in rows]}


@router.post("/risk/mode")
def set_risk_mode(payload: RiskModeIn, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if payload.mode not in {"normal", "reduce_only", "paused", "emergency_stop"}:
        raise HTTPException(status_code=400, detail="无效风控模式")
    risk = db.query(RiskSetting).first() or RiskSetting()
    risk.mode = payload.mode
    db.add(risk)
    audit(db, user.id, "set_risk_mode", "risk", payload.mode)
    db.commit()
    return as_dict(risk)


@router.post("/risk/emergency-stop")
def emergency_stop(user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    risk = db.query(RiskSetting).first() or RiskSetting()
    risk.mode = "emergency_stop"
    db.add(risk)
    db.add(Alert(level="critical", title="紧急停止", message="管理员触发紧急停止，系统禁止自动下单"))
    audit(db, user.id, "emergency_stop", "risk")
    db.commit()
    return {"status": "emergency_stop"}


@router.get("/settings/strategy")
def get_strategy(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return as_dict(db.query(StrategySetting).first())


@router.put("/settings/strategy")
def put_strategy(payload: StrategySettingsIn, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.query(StrategySetting).first() or StrategySetting()
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.add(row)
    audit(db, user.id, "update_strategy", "settings")
    db.commit()
    return as_dict(row)


@router.get("/settings/risk")
def get_risk(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return as_dict(db.query(RiskSetting).first())


@router.put("/settings/risk")
def put_risk(payload: RiskSettingsIn, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.query(RiskSetting).first() or RiskSetting()
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.add(row)
    audit(db, user.id, "update_risk", "settings")
    db.commit()
    return as_dict(row)


@router.get("/settings/symbol-mappings")
def get_symbol_mappings(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [as_dict(row) for row in db.query(SymbolMapping).order_by(SymbolMapping.symbol).all()]


@router.put("/settings/symbol-mappings")
def put_symbol_mappings(payload: list[SymbolMappingIn], user: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    for item in payload:
        row = db.query(SymbolMapping).filter(SymbolMapping.symbol == item.symbol).first()
        if not row:
            row = SymbolMapping(symbol=item.symbol, hyperliquid_symbol=item.hyperliquid_symbol, mt5_symbol=item.mt5_symbol)
        for key, value in item.model_dump().items():
            setattr(row, key, value)
        db.add(row)
    audit(db, user.id, "update_symbol_mappings", "settings")
    db.commit()
    return [as_dict(row) for row in db.query(SymbolMapping).order_by(SymbolMapping.symbol).all()]


@router.post("/settings/symbol-mappings")
def create_symbol_mapping(payload: SymbolMappingIn, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if db.query(SymbolMapping).filter(SymbolMapping.symbol == payload.symbol).first():
        raise HTTPException(status_code=400, detail="内部品种已存在")
    row = SymbolMapping(**payload.model_dump())
    db.add(row)
    audit(db, user.id, "create_symbol_mapping", "settings", payload.symbol)
    db.commit()
    db.refresh(row)
    return as_dict(row)


@router.put("/settings/symbol-mappings/{mapping_id}")
def update_symbol_mapping(mapping_id: int, payload: SymbolMappingIn, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(SymbolMapping, mapping_id)
    if not row:
        raise HTTPException(status_code=404, detail="品种映射不存在")
    duplicated = db.query(SymbolMapping).filter(SymbolMapping.symbol == payload.symbol, SymbolMapping.id != mapping_id).first()
    if duplicated:
        raise HTTPException(status_code=400, detail="内部品种已存在")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    audit(db, user.id, "update_symbol_mapping", "settings", payload.symbol)
    db.commit()
    db.refresh(row)
    return as_dict(row)


@router.delete("/settings/symbol-mappings/{mapping_id}")
def delete_symbol_mapping(mapping_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, str]:
    row = db.get(SymbolMapping, mapping_id)
    if not row:
        raise HTTPException(status_code=404, detail="品种映射不存在")
    symbol = row.symbol
    db.delete(row)
    audit(db, user.id, "delete_symbol_mapping", "settings", symbol)
    db.commit()
    return {"status": "ok"}


@router.post("/settings/symbol-mappings/{mapping_id}/sync-broker")
def sync_symbol_mapping_from_broker(mapping_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(SymbolMapping, mapping_id)
    if not row:
        raise HTTPException(status_code=404, detail="品种映射不存在")
    try:
        import MetaTrader5 as mt5  # type: ignore
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"MetaTrader5 包不可用: {exc}") from exc
    if not mt5.initialize():
        raise HTTPException(status_code=400, detail=f"MT5 初始化失败: {mt5.last_error()}")
    mt5.symbol_select(row.mt5_symbol, True)
    info = mt5.symbol_info(row.mt5_symbol)
    if not info:
        raise HTTPException(status_code=400, detail="MT5 品种不存在或不可见")
    volume_min = float(getattr(info, "volume_min", row.min_order_size))
    volume_step = float(getattr(info, "volume_step", volume_min))
    contract_size = float(getattr(info, "trade_contract_size", row.contract_multiplier or 1.0))
    currency_base = str(getattr(info, "currency_base", "") or "")
    currency_profit = str(getattr(info, "currency_profit", row.quote_asset or "USD") or "USD")
    currency_margin = str(getattr(info, "currency_margin", currency_profit) or currency_profit)
    calc_mode = int(getattr(info, "trade_calc_mode", 0) or 0)
    digits = int(getattr(info, "digits", row.price_precision))
    tick_size = float(getattr(info, "trade_tick_size", 0.0) or getattr(info, "point", row.min_tick))
    mt5_min_base_size = volume_min * contract_size
    mt5_base_step = volume_step * contract_size
    row.mt5_min_lot = volume_min
    row.mt5_volume_step = volume_step
    row.mt5_contract_size = contract_size
    row.mt5_currency_base = currency_base
    row.mt5_currency_profit = currency_profit
    row.mt5_currency_margin = currency_margin
    row.mt5_calc_mode = calc_mode
    row.quote_asset = currency_profit or row.quote_asset
    row.mt5_min_base_size = mt5_min_base_size
    row.contract_multiplier = contract_size
    row.hyperliquid_min_notional = row.hyperliquid_min_notional or get_settings().hyperliquid_default_min_notional
    row.min_order_size = _effective_min_order_size(row)
    row.quantity_precision = max(_decimal_places(mt5_base_step), 0)
    row.price_precision = digits
    row.min_tick = tick_size
    audit(db, user.id, "sync_symbol_mapping_from_broker", "settings", row.symbol)
    db.commit()
    db.refresh(row)
    return {
        **as_dict(row),
        "broker": {
            "volume_min": volume_min,
            "volume_step": volume_step,
            "volume_max": float(getattr(info, "volume_max", 0.0)),
            "trade_contract_size": contract_size,
            "currency_base": currency_base,
            "currency_profit": currency_profit,
            "currency_margin": currency_margin,
            "trade_calc_mode": calc_mode,
            "digits": digits,
            "trade_tick_size": tick_size,
            "swap_long": float(getattr(info, "swap_long", 0.0)),
            "swap_short": float(getattr(info, "swap_short", 0.0)),
            "swap_mode": int(getattr(info, "swap_mode", 0)),
        },
    }


def _effective_min_order_size(row: SymbolMapping) -> float:
    hyper_quote = quote_cache.latest("hyperliquid", row.symbol)
    hyper_mid = hyper_quote.mid if hyper_quote else 0.0
    hyper_notional_base = (row.hyperliquid_min_notional / hyper_mid) if hyper_mid > 0 and row.hyperliquid_min_notional > 0 else 0.0
    return max(row.mt5_min_base_size or 0.0, row.hyperliquid_min_base_size or 0.0, hyper_notional_base)


def _decimal_places(value: float) -> int:
    text = f"{value:.12f}".rstrip("0").rstrip(".")
    return len(text.split(".", 1)[1]) if "." in text else 0


@router.get("/settings/live-trading")
def get_live_trading(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.query(SystemSetting).filter(SystemSetting.key == "live_trading_enabled").first()
    return {"enabled": bool(row and row.value == "true"), "confirmation_required": "ENABLE LIVE TRADING"}


@router.get("/settings/live-readiness")
def get_live_readiness(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return live_execution_readiness(db)


@router.put("/settings/live-trading")
def put_live_trading(payload: LiveTradingIn, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if payload.enabled and payload.confirmation != "ENABLE LIVE TRADING":
        raise HTTPException(status_code=400, detail="开启实盘需要输入确认短语")
    row = db.query(SystemSetting).filter(SystemSetting.key == "live_trading_enabled").first() or SystemSetting(key="live_trading_enabled")
    row.value = "true" if payload.enabled else "false"
    db.add(row)
    audit(db, user.id, "update_live_trading", "settings", row.value)
    db.commit()
    return {"enabled": row.value == "true"}


@router.get("/logs")
def logs(_: User = Depends(get_current_user), db: Session = Depends(get_db), page: int = 1, page_size: int = 20, level: str = "", keyword: str = "") -> dict[str, Any]:
    query = db.query(SystemLog)
    if level:
        query = query.filter(SystemLog.level == level)
    if keyword:
        query = query.filter(SystemLog.message.contains(keyword))
    total = query.count()
    rows = query.order_by(desc(SystemLog.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [as_dict(row) for row in rows]}


@router.get("/alerts")
def alerts(_: User = Depends(get_current_user), db: Session = Depends(get_db), page: int = 1, page_size: int = 20) -> dict[str, Any]:
    query = db.query(Alert)
    total = query.count()
    rows = query.order_by(desc(Alert.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [as_dict(row) for row in rows]}


@router.post("/alerts/{alert_id}/ack")
def ack_alert(alert_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(Alert, alert_id)
    if not row:
        raise HTTPException(status_code=404, detail="告警不存在")
    row.acknowledged = True
    audit(db, user.id, "ack_alert", "alert", str(alert_id))
    db.commit()
    return as_dict(row)
