from dataclasses import dataclass
from importlib import import_module
import json
from urllib import request

from sqlalchemy.orm import Session

from app.config.settings import Settings, get_settings
from app.db.models import HedgeGroup, Position, SymbolMapping, SystemSetting


@dataclass(frozen=True)
class ReadinessCheck:
    component: str
    status: str
    message: str


def live_execution_readiness(db: Session, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    checks: list[ReadinessCheck] = []
    checks.extend(_global_live_checks(db))
    checks.extend(_hyperliquid_nautilus_checks(settings))
    checks.extend(_mt5_checks(settings))
    checks.extend(_symbol_mapping_checks(db))
    checks.extend(_position_safety_checks(db))
    overall = _overall_status(checks)
    return {
        "status": overall,
        "ready": overall == "ready",
        "checks": [check.__dict__ for check in checks],
    }


def _global_live_checks(db: Session) -> list[ReadinessCheck]:
    row = db.query(SystemSetting).filter(SystemSetting.key == "live_trading_enabled").first()
    enabled = bool(row and row.value == "true")
    return [
        ReadinessCheck(
            "global_live_switch",
            "ok" if enabled else "block",
            "系统实盘总开关已开启" if enabled else "系统实盘总开关未开启",
        )
    ]


def _hyperliquid_nautilus_checks(settings: Settings) -> list[ReadinessCheck]:
    user = _hyperliquid_user_address(settings)
    checks = [
        ReadinessCheck(
            "nautilus_hyperliquid_enabled",
            "ok" if settings.nautilus_hyperliquid_enabled else "block",
            "NautilusTrader Hyperliquid gateway 已启用" if settings.nautilus_hyperliquid_enabled else "NAUTILUS_HYPERLIQUID_ENABLED 未开启",
        ),
        ReadinessCheck(
            "nautilus_hyperliquid_submit_enabled",
            "ok" if settings.nautilus_hyperliquid_submit_enabled else "block",
            "NautilusTrader Hyperliquid 实盘提交已开启" if settings.nautilus_hyperliquid_submit_enabled else "NAUTILUS_HYPERLIQUID_SUBMIT_ENABLED 未开启",
        ),
        ReadinessCheck(
            "hyperliquid_private_key",
            "ok" if settings.nautilus_hyperliquid_private_key else "block",
            "NautilusTrader Hyperliquid private key 已配置" if settings.nautilus_hyperliquid_private_key else "NAUTILUS_HYPERLIQUID_PRIVATE_KEY 未配置",
        ),
        ReadinessCheck(
            "hyperliquid_account_address",
            "ok" if user else "block",
            "Hyperliquid 账户地址已配置" if user else "HYPERLIQUID_ACCOUNT_ADDRESS 或钱包/ vault 地址未配置，无法做账户级回查",
        ),
    ]
    try:
        import_module("nautilus_trader.adapters.hyperliquid")
        checks.append(ReadinessCheck("nautilus_trader_import", "ok", "nautilus_trader Hyperliquid adapter 可导入"))
    except Exception as exc:
        checks.append(ReadinessCheck("nautilus_trader_import", "block", f"nautilus_trader Hyperliquid adapter 不可导入: {exc}"))
    if user:
        checks.append(_hyperliquid_read_probe(settings, user))
    return checks


def _mt5_checks(settings: Settings) -> list[ReadinessCheck]:
    checks = [
        ReadinessCheck(
            "mt5_live_order_enabled",
            "ok" if settings.mt5_live_order_enabled else "block",
            "MT5 实盘下单开关已开启" if settings.mt5_live_order_enabled else "MT5_LIVE_ORDER_ENABLED 未开启",
        )
    ]
    try:
        mt5 = import_module("MetaTrader5")
        checks.append(ReadinessCheck("metatrader5_import", "ok", "MetaTrader5 Python 包可导入"))
        checks.append(_mt5_read_probe(mt5, settings))
    except Exception as exc:
        checks.append(ReadinessCheck("metatrader5_import", "block", f"MetaTrader5 Python 包不可导入: {exc}"))
    if settings.mt5_login and settings.mt5_server:
        checks.append(ReadinessCheck("mt5_login_config", "ok", "MT5 登录参数已配置"))
    else:
        checks.append(ReadinessCheck("mt5_login_config", "warn", "MT5 登录参数未完整配置，将依赖本机终端已有登录会话"))
    return checks


def _symbol_mapping_checks(db: Session) -> list[ReadinessCheck]:
    rows = db.query(SymbolMapping).filter(SymbolMapping.enabled == True).all()  # noqa: E712
    if not rows:
        return [ReadinessCheck("symbol_mappings", "block", "没有启用的品种映射")]
    checks = [ReadinessCheck("symbol_mappings", "ok", f"已启用 {len(rows)} 个品种映射")]
    missing_mt5_specs = [row.symbol for row in rows if not row.mt5_symbol or row.mt5_volume_step <= 0 or row.mt5_contract_size <= 0]
    if missing_mt5_specs:
        checks.append(ReadinessCheck("mt5_symbol_specs", "warn", f"以下品种 MT5 规格未完整同步: {', '.join(missing_mt5_specs)}"))
    else:
        checks.append(ReadinessCheck("mt5_symbol_specs", "ok", "启用品种 MT5 规格已同步"))
    auto_comp = [row.symbol for row in rows if row.single_leg_action in {"auto_close", "reverse_filled_leg"}]
    if auto_comp:
        checks.append(ReadinessCheck("single_leg_compensation", "warn", f"以下品种启用单腿自动反向冲销: {', '.join(auto_comp)}"))
    else:
        checks.append(ReadinessCheck("single_leg_compensation", "ok", "单腿异常默认人工介入"))
    return checks


def _position_safety_checks(db: Session) -> list[ReadinessCheck]:
    positions = db.query(Position).filter(Position.platform.in_(["hyperliquid", "mt5"])).all()
    active_positions = [row for row in positions if abs(row.quantity) > 0]
    if not active_positions:
        return [ReadinessCheck("live_position_management", "ok", "当前未发现已同步 live 仓位")]

    residual: list[str] = []
    orphan: list[str] = []
    for position in active_positions:
        matches = _live_groups_for_position(db, position)
        if not matches:
            orphan.append(_position_label(position))
            continue
        if any(group.status == "closed" for group in matches) and not any(group.status != "closed" for group in matches):
            residual.append(_position_label(position))

    checks: list[ReadinessCheck] = []
    if residual:
        checks.append(ReadinessCheck("live_residual_positions", "block", f"已关闭 live 对冲组仍存在残余仓位: {', '.join(residual)}"))
    if orphan:
        checks.append(ReadinessCheck("live_orphan_positions", "block", f"存在未归属 live 对冲组的外部仓位: {', '.join(orphan)}"))
    if not checks:
        checks.append(ReadinessCheck("live_position_management", "ok", "已同步 live 仓位均归属于系统对冲组"))
    return checks


def _live_groups_for_position(db: Session, position: Position) -> list[HedgeGroup]:
    groups = db.query(HedgeGroup).filter(HedgeGroup.execution_mode == "live").all()
    return [group for group in groups if _position_matches_group(db, position, group)]


def _position_matches_group(db: Session, position: Position, group: HedgeGroup) -> bool:
    if position.platform not in {"hyperliquid", "mt5"}:
        return False
    symbols = {
        "hyperliquid": {group.symbol},
        "mt5": {group.symbol},
    }
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
    if mapping:
        if mapping.hyperliquid_symbol:
            symbols["hyperliquid"].add(mapping.hyperliquid_symbol)
        if mapping.mt5_symbol:
            symbols["mt5"].add(mapping.mt5_symbol)
    if position.symbol not in symbols.get(position.platform, set()):
        return False
    if _position_side(position.side) != _expected_position_side(group.direction, position.platform):
        return False
    if group.status == "closed":
        return True
    expected_quantity = _expected_position_quantity(group, position.platform)
    if expected_quantity <= 0:
        return False
    tolerance = max(expected_quantity * 0.000001, 0.00000001)
    return abs(abs(position.quantity) - expected_quantity) <= tolerance


def _expected_position_side(direction: str, platform: str) -> str:
    if direction == "long_hyperliquid_short_mt5":
        return "long" if platform == "hyperliquid" else "short"
    return "short" if platform == "hyperliquid" else "long"


def _expected_position_quantity(group: HedgeGroup, platform: str) -> float:
    if platform == "hyperliquid":
        value = group.hyperliquid_quantity
    else:
        value = group.mt5_quantity
    return float(group.quantity if value is None else value)


def _position_side(side: str) -> str:
    value = str(side or "").strip().lower()
    if value in {"buy", "long"}:
        return "long"
    if value in {"sell", "short"}:
        return "short"
    return value


def _position_label(position: Position) -> str:
    return f"{position.platform}:{position.symbol}:{position.side}:{position.quantity}"


def _hyperliquid_user_address(settings: Settings) -> str:
    return settings.hyperliquid_account_address or settings.hyperliquid_wallet_address or settings.nautilus_hyperliquid_vault_address


def _hyperliquid_read_probe(settings: Settings, user: str) -> ReadinessCheck:
    try:
        payload = json.dumps({"type": "clearinghouseState", "user": user}).encode("utf-8")
        req = request.Request(settings.hyperliquid_info_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and ("marginSummary" in data or "crossMarginSummary" in data or "assetPositions" in data):
            return ReadinessCheck("hyperliquid_read_probe", "ok", "Hyperliquid clearinghouseState 只读探测成功")
        return ReadinessCheck("hyperliquid_read_probe", "block", "Hyperliquid clearinghouseState 返回格式异常")
    except Exception as exc:
        return ReadinessCheck("hyperliquid_read_probe", "block", f"Hyperliquid clearinghouseState 只读探测失败: {exc}")


def _mt5_read_probe(mt5, settings: Settings) -> ReadinessCheck:
    initialized = False
    try:
        if settings.mt5_login and settings.mt5_password and settings.mt5_server:
            initialized = mt5.initialize(login=int(settings.mt5_login), password=settings.mt5_password, server=settings.mt5_server)
        else:
            initialized = mt5.initialize()
        if not initialized:
            return ReadinessCheck("mt5_read_probe", "block", f"MT5 initialize 失败: {mt5.last_error()}")
        info = mt5.account_info()
        if not info:
            return ReadinessCheck("mt5_read_probe", "block", f"MT5 account_info 为空: {mt5.last_error()}")
        login = getattr(info, "login", "")
        server = getattr(info, "server", "")
        return ReadinessCheck("mt5_read_probe", "ok", f"MT5 account_info 只读探测成功: {login} {server}".strip())
    except Exception as exc:
        return ReadinessCheck("mt5_read_probe", "block", f"MT5 account_info 只读探测失败: {exc}")
    finally:
        if initialized:
            try:
                mt5.shutdown()
            except Exception:
                pass


def _overall_status(checks: list[ReadinessCheck]) -> str:
    if any(check.status == "block" for check in checks):
        return "blocked"
    if any(check.status == "warn" for check in checks):
        return "warning"
    return "ready"
