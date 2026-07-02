from dataclasses import dataclass
from importlib import import_module
import json
from urllib import request

from sqlalchemy.orm import Session

from app.config.settings import Settings, get_settings, hyperliquid_execution_info_url
from app.adapters.venue import NATIVE_VENUES
from app.db.models import ExchangeCredential, HedgeGroup, Position, SymbolMapping, SystemSetting
from app.execution.probe import NAUTILUS_PROBE_SUPPORTED_VENUES
from app.execution.runtime_settings import paper_live_probe_enabled_for_venue, runtime_paper_live_probe_enabled


@dataclass(frozen=True)
class ReadinessCheck:
    component: str
    status: str
    message: str


def live_execution_readiness(db: Session, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    checks: list[ReadinessCheck] = []
    checks.extend(_global_live_checks(db))
    checks.extend(_hyperliquid_live_checks(settings))
    checks.extend(_mt5_checks(settings))
    checks.extend(_symbol_mapping_checks(db))
    checks.extend(_position_safety_checks(db))
    overall = _overall_status(checks)
    return {
        "status": overall,
        "ready": overall == "ready",
        "checks": [check.__dict__ for check in checks],
    }


def paper_execution_readiness(db: Session, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    checks: list[ReadinessCheck] = []
    checks.extend(_hyperliquid_paper_checks(db, settings))
    checks.extend(_mt5_demo_checks(settings))
    checks.extend(_symbol_mapping_checks(db))
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


def _hyperliquid_live_checks(settings: Settings) -> list[ReadinessCheck]:
    user = _hyperliquid_user_address(settings)
    checks = [
        ReadinessCheck(
            "hyperliquid_account_address",
            "ok" if user else "block",
            "Hyperliquid 账户地址已配置" if user else "HYPERLIQUID_ACCOUNT_ADDRESS 未配置，无法做账户级回查",
        ),
        ReadinessCheck("hyperliquid_live_order_submit", "block", "Hyperliquid 实盘下单 SDK 已移除，当前只允许只读账户/仓位检查"),
    ]
    if user:
        checks.append(_hyperliquid_read_probe(settings, user))
    return checks


def _hyperliquid_paper_checks(db: Session, settings: Settings) -> list[ReadinessCheck]:
    mappings = db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True)).all()
    if not mappings:
        return [ReadinessCheck("hyperliquid_paper_matching", "block", "没有启用的品种映射，无法进行 Hyperliquid paper 撮合")]
    checks = [
        ReadinessCheck(
            "hyperliquid_paper_matching",
            "ok",
            f"Hyperliquid paper 使用本地 QuoteCache 撮合，已启用 {len(mappings)} 个品种",
        )
    ]
    paper_live_hyperliquid = paper_live_probe_enabled_for_venue(db, settings, "hyperliquid")
    if paper_live_hyperliquid:
        checks[0] = ReadinessCheck(
            "hyperliquid_paper_live_probe",
            "ok",
            f"Hyperliquid paper-live 探针已开启，paper 账本数量不变，HL 使用最小真实订单取成交价；已启用 {len(mappings)} 个品种",
        )
        user = _hyperliquid_user_address(settings)
        checks.append(
            ReadinessCheck(
                "hyperliquid_paper_live_credentials",
                "ok" if user and getattr(settings, "hyperliquid_secret_key", "") else "block",
                "Hyperliquid paper-live 账户地址和 API 私钥已配置" if user and getattr(settings, "hyperliquid_secret_key", "") else "HYPERLIQUID_ACCOUNT_ADDRESS 或 HYPERLIQUID_SECRET_KEY 未配置",
            )
        )
        try:
            import_module("hyperliquid.exchange")
            import_module("eth_account")
            checks.append(ReadinessCheck("hyperliquid_sdk_import", "ok", "hyperliquid-python-sdk 可导入"))
        except Exception as exc:
            checks.append(ReadinessCheck("hyperliquid_sdk_import", "block", f"hyperliquid-python-sdk 不可导入: {exc}"))
    checks.extend(_generic_paper_live_probe_checks(db, mappings, settings))
    return checks


def _generic_paper_live_probe_checks(db: Session, mappings: list[SymbolMapping], settings: Settings) -> list[ReadinessCheck]:
    if not runtime_paper_live_probe_enabled(db, settings):
        return []
    mapped_venues = {
        venue
        for mapping in mappings
        for venue in (str(mapping.leg_a_venue or "").strip().lower(), str(mapping.leg_b_venue or "").strip().lower())
        if venue and venue != "mt5"
    }
    enabled_mapped = sorted(mapped_venues)
    checks = [
        ReadinessCheck(
            "paper_live_probe_venues",
            "ok" if enabled_mapped else "warn",
            f"通用 paper-live 探针 venue 已启用: {', '.join(enabled_mapped)}" if enabled_mapped else "PAPER_LIVE_PROBE_ENABLED 已开启，但当前启用品种映射未命中 PAPER_LIVE_PROBE_VENUES",
        )
    ]
    unsupported = sorted(mapped_venues - {"hyperliquid"} - NAUTILUS_PROBE_SUPPORTED_VENUES)
    if unsupported:
        checks.append(
            ReadinessCheck(
                "paper_live_probe_adapter_support",
                "warn",
                f"以下 venue 已进入通用探针框架，但真实探针下单 adapter 尚未实现: {', '.join(unsupported)}",
            )
        )
    for venue in sorted(mapped_venues & NAUTILUS_PROBE_SUPPORTED_VENUES):
        credential = db.query(ExchangeCredential).filter(ExchangeCredential.venue == venue, ExchangeCredential.enabled.is_(True)).first()
        ready = bool(credential and not credential.read_only and credential.encrypted_credentials)
        checks.append(
            ReadinessCheck(
                f"{venue}_paper_live_probe_credentials",
                "ok" if ready else "block",
                f"{venue} paper-live 探针凭证已启用且允许交易" if ready else f"{venue} paper-live 探针需要启用交易所配置、填写凭证并关闭只读模式",
            )
        )
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


def _mt5_demo_checks(settings: Settings) -> list[ReadinessCheck]:
    checks = [
        ReadinessCheck(
            "mt5_demo_order_enabled",
            "ok" if getattr(settings, "mt5_demo_order_enabled", False) else "block",
            "MT5 demo 下单开关已开启" if getattr(settings, "mt5_demo_order_enabled", False) else "MT5_DEMO_ORDER_ENABLED 未开启",
        )
    ]
    try:
        mt5 = import_module("MetaTrader5")
        checks.append(ReadinessCheck("metatrader5_import", "ok", "MetaTrader5 Python 包可导入"))
        checks.append(_mt5_demo_probe(mt5, settings))
    except Exception as exc:
        checks.append(ReadinessCheck("metatrader5_import", "block", f"MetaTrader5 Python 包不可导入: {exc}"))
    if settings.mt5_login and settings.mt5_server:
        checks.append(ReadinessCheck("mt5_login_config", "ok", "MT5 登录参数已配置，并会用于锁定 demo 账户身份"))
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
    positions = db.query(Position).filter(Position.platform.in_(list(NATIVE_VENUES))).all()
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
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
    leg_a_venue = mapping.leg_a_venue if mapping else "hyperliquid"
    leg_b_venue = mapping.leg_b_venue if mapping else "mt5"
    if position.platform not in {leg_a_venue, leg_b_venue}:
        return False
    symbols = {
        leg_a_venue: {group.symbol},
        leg_b_venue: {group.symbol},
    }
    if mapping:
        if mapping.leg_a_venue_symbol:
            symbols[leg_a_venue].add(mapping.leg_a_venue_symbol)
        if mapping.mt5_symbol:
            symbols[leg_b_venue].add(mapping.mt5_symbol)
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
    if direction == "long_leg_a_short_leg_b":
        if platform == "hyperliquid":
            return "long"
        return "short"
    return "short" if platform == "hyperliquid" else "long"


def _expected_position_quantity(group: HedgeGroup, platform: str) -> float:
    if platform == "hyperliquid":
        value = group.leg_a_quantity
    else:
        value = group.leg_b_quantity
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
    return settings.hyperliquid_account_address


def _hyperliquid_read_probe(settings: Settings, user: str) -> ReadinessCheck:
    try:
        payload = json.dumps({"type": "clearinghouseState", "user": user}).encode("utf-8")
        req = request.Request(hyperliquid_execution_info_url(settings), data=payload, headers={"Content-Type": "application/json"}, method="POST")
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


def _mt5_demo_probe(mt5, settings: Settings) -> ReadinessCheck:
    initialized = False
    try:
        if settings.mt5_login and settings.mt5_password and settings.mt5_server:
            initialized = mt5.initialize(login=int(settings.mt5_login), password=settings.mt5_password, server=settings.mt5_server)
        else:
            initialized = mt5.initialize()
        if not initialized:
            return ReadinessCheck("mt5_demo_account", "block", f"MT5 initialize 失败: {mt5.last_error()}")
        from app.adapters.mt5 import mt5_demo_order_check

        check = mt5_demo_order_check(mt5, settings)
        return ReadinessCheck("mt5_demo_account", "ok" if check.allowed else "block", check.message)
    except Exception as exc:
        return ReadinessCheck("mt5_demo_account", "block", f"MT5 demo 账户检查失败: {exc}")
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
