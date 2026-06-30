import json
from urllib import request

from loguru import logger
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.adapters.paper import PaperAdapter
from app.adapters.nautilus import nautilus_account_snapshot
from app.adapters.venue import nautilus_venues_from_mappings
from app.config.settings import get_settings, hyperliquid_execution_info_url
from app.db.models import AccountSnapshot, ExchangeCredential, SymbolMapping
from app.exchanges.credentials import binance_account_balances, binance_futures_account, mark_test_result


def sync_account_snapshots(db: Session) -> list[AccountSnapshot]:
    snapshots = [_hyperliquid_account_snapshot(), _mt5_account_snapshot()]
    for credential in _enabled_exchange_credentials(db):
        try:
            snapshots.append(_configured_exchange_account_snapshot(credential))
        except Exception as exc:
            logger.warning(f"交易所账户读取失败: {credential.venue}; {exc}")
            mark_test_result(credential, "failed", str(exc))
            snapshots.append(_configured_exchange_status_snapshot(credential, "error"))
    for snapshot in snapshots:
        db.add(snapshot)
    db.commit()
    return latest_account_snapshots(db)


def latest_account_snapshots(db: Session) -> list[AccountSnapshot]:
    rows: list[AccountSnapshot] = []
    platforms = ["hyperliquid", "mt5", *_enabled_nautilus_venues(db)]
    for platform in platforms:
        row = db.query(AccountSnapshot).filter(AccountSnapshot.platform == platform).order_by(desc(AccountSnapshot.created_at)).first()
        if row:
            rows.append(row)
    return rows


def ensure_initial_account_snapshots(db: Session) -> None:
    if db.query(AccountSnapshot).count():
        return
    for platform in ("hyperliquid", "mt5"):
        account = PaperAdapter(platform).get_account()
        db.add(
            AccountSnapshot(
                platform=account.platform,
                equity=account.equity,
                available_balance=account.available_balance,
                margin_used=account.margin_used,
                margin_ratio=account.margin_ratio,
                currency=account.currency,
            )
        )
    db.commit()


def _enabled_nautilus_venues(db: Session) -> list[str]:
    mappings = db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True)).all()
    venues = nautilus_venues_from_mappings(mappings)
    configured = db.query(ExchangeCredential.venue).filter(ExchangeCredential.enabled.is_(True), ExchangeCredential.venue.notin_(["hyperliquid", "mt5"])).all()
    for (venue,) in configured:
        if venue not in venues:
            venues.append(venue)
    return venues


def _enabled_exchange_credentials(db: Session) -> list[ExchangeCredential]:
    return (
        db.query(ExchangeCredential)
        .filter(ExchangeCredential.enabled.is_(True), ExchangeCredential.venue.notin_(["hyperliquid", "mt5"]))
        .order_by(ExchangeCredential.venue)
        .all()
    )


def _configured_exchange_account_snapshot(row: ExchangeCredential) -> AccountSnapshot:
    if row.venue == "binance":
        try:
            account = binance_futures_account(row)
            equity = float(account.get("totalMarginBalance", 0.0) or account.get("totalWalletBalance", 0.0) or 0.0)
            available = float(account.get("availableBalance", 0.0) or 0.0)
            margin_used = float(account.get("totalInitialMargin", 0.0) or 0.0)
            return AccountSnapshot(
                platform=row.venue,
                equity=equity,
                available_balance=available,
                margin_used=margin_used,
                margin_ratio=(equity / margin_used) if margin_used > 0 else 1.0,
                currency="USDT",
                portfolio_value=equity,
                perp_equity=equity,
                withdrawable=available,
                free_collateral=available,
                data_source=f"binance_futures_{row.environment}_account"[:64],
            )
        except Exception:
            balances = binance_account_balances(row)
            stable_assets = {"USDT", "USDC", "BUSD", "FDUSD", "TUSD", "USD"}
            stable_total = sum(amount for asset, amount in balances.items() if asset.upper() in stable_assets)
        return AccountSnapshot(
            platform=row.venue,
            equity=stable_total,
            available_balance=stable_total,
            margin_used=0.0,
            margin_ratio=1.0,
            currency="USD",
            portfolio_value=stable_total,
            perp_equity=0.0,
            withdrawable=stable_total,
            free_collateral=stable_total,
            data_source=f"binance_spot_{row.environment}_account"[:64],
        )
    try:
        data = nautilus_account_snapshot(row.venue)
        return AccountSnapshot(**data)
    except Exception:
        return _configured_exchange_status_snapshot(row, row.last_test_status or "not_implemented")


def _configured_exchange_status_snapshot(row: ExchangeCredential, status: str) -> AccountSnapshot:
    source = f"configured_{row.environment}_{status}"
    return AccountSnapshot(
        platform=row.venue,
        equity=0.0,
        available_balance=0.0,
        margin_used=0.0,
        margin_ratio=1.0,
        currency="USD",
        portfolio_value=0.0,
        perp_equity=0.0,
        withdrawable=0.0,
        free_collateral=0.0,
        data_source=source[:64],
    )


def _hyperliquid_account_snapshot() -> AccountSnapshot:
    settings = get_settings()
    account_address = settings.hyperliquid_account_address
    if account_address:
        try:
            data = _post_hyperliquid_info({"type": "clearinghouseState", "user": account_address})
            spot_data = _post_hyperliquid_info({"type": "spotClearinghouseState", "user": account_address})
            margin = data.get("marginSummary") or data.get("crossMarginSummary") or {}
            equity = float(margin.get("accountValue", 0.0))
            margin_used = float(margin.get("totalMarginUsed", 0.0))
            withdrawable = float(data.get("withdrawable", 0.0) or 0.0)
            spot_balance, spot_hold = _spot_usdc_balance(spot_data)
            spot_free = max(spot_balance - spot_hold, 0.0)
            free_collateral = max(withdrawable, spot_free)
            portfolio_value = spot_balance
            margin_ratio = (equity / margin_used) if margin_used > 0 else 1.0
            return AccountSnapshot(
                platform="hyperliquid",
                equity=portfolio_value,
                available_balance=free_collateral,
                margin_used=margin_used,
                margin_ratio=margin_ratio,
                currency="USDC",
                portfolio_value=portfolio_value,
                perp_equity=equity,
                spot_balance=spot_balance,
                spot_hold=spot_hold,
                withdrawable=withdrawable,
                free_collateral=free_collateral,
                data_source="hyperliquid_testnet" if "testnet" in hyperliquid_execution_info_url(settings) else "hyperliquid",
            )
        except Exception as exc:
            logger.warning(f"Hyperliquid 账户读取失败，回退 Paper 账户: {exc}")
    account = PaperAdapter("hyperliquid").get_account()
    return AccountSnapshot(
        platform=account.platform,
        equity=account.equity,
        available_balance=account.available_balance,
        margin_used=account.margin_used,
        margin_ratio=account.margin_ratio,
        currency=account.currency,
        portfolio_value=account.equity,
        perp_equity=account.equity,
        withdrawable=account.available_balance,
        free_collateral=account.available_balance,
        data_source="paper",
    )


def _mt5_account_snapshot() -> AccountSnapshot:
    settings = get_settings()
    try:
        import MetaTrader5 as mt5  # type: ignore
    except Exception as exc:
        logger.warning(f"MetaTrader5 包不可用，回退 Paper 账户: {exc}")
        return _paper_mt5_account_snapshot()

    initialized = False
    try:
        if settings.mt5_login and settings.mt5_password and settings.mt5_server:
            initialized = mt5.initialize(login=int(settings.mt5_login), password=settings.mt5_password, server=settings.mt5_server)
        else:
            initialized = mt5.initialize()
        if not initialized:
            logger.warning(f"MT5 initialize 失败，回退 Paper 账户: {mt5.last_error()}")
            return _paper_mt5_account_snapshot()
        info = mt5.account_info()
        if not info:
            logger.warning(f"MT5 account_info 为空，回退 Paper 账户: {mt5.last_error()}")
            return _paper_mt5_account_snapshot()
        margin = float(getattr(info, "margin", 0.0) or 0.0)
        margin_level = float(getattr(info, "margin_level", 0.0) or 0.0)
        return AccountSnapshot(
            platform="mt5",
            equity=float(getattr(info, "equity", 0.0) or 0.0),
            available_balance=float(getattr(info, "margin_free", 0.0) or 0.0),
            margin_used=margin,
            margin_ratio=(margin_level / 100) if margin_level > 0 else 1.0,
            currency=str(getattr(info, "currency", "USD") or "USD"),
            portfolio_value=float(getattr(info, "equity", 0.0) or 0.0),
            perp_equity=float(getattr(info, "equity", 0.0) or 0.0),
            withdrawable=float(getattr(info, "margin_free", 0.0) or 0.0),
            free_collateral=float(getattr(info, "margin_free", 0.0) or 0.0),
            data_source="mt5_account_info",
        )
    except Exception as exc:
        logger.warning(f"MT5 账户读取失败，回退 Paper 账户: {exc}")
        return _paper_mt5_account_snapshot()
    finally:
        if initialized:
            mt5.shutdown()


def _paper_mt5_account_snapshot() -> AccountSnapshot:
    account = PaperAdapter("mt5").get_account()
    return AccountSnapshot(
        platform=account.platform,
        equity=account.equity,
        available_balance=account.available_balance,
        margin_used=account.margin_used,
        margin_ratio=account.margin_ratio,
        currency=account.currency,
        portfolio_value=account.equity,
        perp_equity=account.equity,
        withdrawable=account.available_balance,
        free_collateral=account.available_balance,
        data_source="paper",
    )


def _spot_usdc_balance(data: dict) -> tuple[float, float]:
    for item in data.get("balances") or []:
        if item.get("coin") == "USDC":
            return float(item.get("total", 0.0) or 0.0), float(item.get("hold", 0.0) or 0.0)
    return 0.0, 0.0


def _post_hyperliquid_info(payload: dict):
    settings = get_settings()
    req = request.Request(
        hyperliquid_execution_info_url(settings),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))
