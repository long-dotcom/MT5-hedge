from __future__ import annotations

import base64
import asyncio
import hashlib
import json
from threading import Thread
from datetime import datetime, timezone
from typing import Any

import msgspec
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.db.models import ExchangeCredential


SENSITIVE_KEY_HINTS = ("key", "secret", "password", "passphrase", "token")


def normalize_venue(value: str) -> str:
    return (value or "").strip().lower()


def encrypt_credentials(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return _fernet().encrypt(raw).decode("ascii")


def decrypt_credentials(row: ExchangeCredential) -> dict[str, Any]:
    if not row.encrypted_credentials:
        return {}
    try:
        raw = _fernet().decrypt(row.encrypted_credentials.encode("ascii"))
    except InvalidToken as exc:
        raise ValueError("交易所凭证解密失败，请检查 EXCHANGE_CONFIG_SECRET/JWT_SECRET 是否变更") from exc
    return json.loads(raw.decode("utf-8"))


def credential_fingerprint(payload: dict[str, Any]) -> str:
    redacted_basis = json.dumps(_redacted(payload), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(redacted_basis.encode("utf-8")).hexdigest()[:16]


def public_exchange_credential(row: ExchangeCredential, *, include_schema: bool = True) -> dict[str, Any]:
    data = {
        "id": row.id,
        "venue": row.venue,
        "display_name": row.display_name,
        "environment": row.environment,
        "enabled": row.enabled,
        "read_only": row.read_only,
        "configured": bool(row.encrypted_credentials),
        "credentials_fingerprint": row.credentials_fingerprint,
        "last_test_status": row.last_test_status,
        "last_test_message": row.last_test_message,
        "last_tested_at": row.last_tested_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if include_schema:
        data["credential_fields"] = credential_fields_for_venue(row.venue)
    return data


def upsert_exchange_credential(db: Session, payload: dict[str, Any]) -> ExchangeCredential:
    venue = normalize_venue(str(payload.get("venue") or ""))
    if not venue:
        raise ValueError("venue 不能为空")
    row = db.query(ExchangeCredential).filter(ExchangeCredential.venue == venue).first()
    if not row:
        row = ExchangeCredential(venue=venue)
    row.display_name = str(payload.get("display_name") or venue.upper()).strip()
    row.environment = str(payload.get("environment") or "sandbox").strip().lower()
    row.enabled = bool(payload.get("enabled", False))
    row.read_only = bool(payload.get("read_only", True))
    credentials = _clean_credentials(payload.get("credentials"))
    if credentials:
        row.encrypted_credentials = encrypt_credentials(credentials)
        row.credentials_fingerprint = credential_fingerprint(credentials)
        row.last_test_status = "untested"
        row.last_test_message = ""
        row.last_tested_at = None
    db.add(row)
    return row


def mark_test_result(row: ExchangeCredential, status: str, message: str) -> None:
    row.last_test_status = status
    row.last_test_message = message
    row.last_tested_at = datetime.now(timezone.utc).replace(tzinfo=None)


def validate_exchange_credential(row: ExchangeCredential) -> tuple[str, str]:
    credentials = decrypt_credentials(row)
    missing = _missing_required_fields(row.venue, credentials)
    if missing:
        return "failed", f"缺少必填字段: {', '.join(missing)}"
    if row.venue == "binance":
        return _validate_binance(row, credentials)
    try:
        import nautilus_trader.adapters  # noqa: F401
    except Exception as exc:
        return "failed", f"NautilusTrader adapter 依赖不可用: {exc}"
    return "warning", f"{row.venue} 凭证格式有效；真实账户连通性验证尚未接入"


def binance_account_balances(row: ExchangeCredential) -> dict[str, float]:
    account = _run_async(_nautilus_binance_spot_account(row))
    balances = getattr(account, "balances", []) or []
    totals: dict[str, float] = {}
    for item in balances:
        asset = str(getattr(item, "asset", "") or "")
        free = _float(getattr(item, "free", 0.0))
        locked = _float(getattr(item, "locked", 0.0))
        amount = free + locked
        if amount > 0:
            totals[asset] = amount
    return totals


def binance_futures_account(row: ExchangeCredential) -> dict[str, Any]:
    account = _run_async(_nautilus_binance_futures_account(row))
    return {field: getattr(account, field, None) for field in getattr(account, "__struct_fields__", ())}


def binance_futures_positions(row: ExchangeCredential) -> list[dict[str, Any]]:
    payload = _run_async(_nautilus_binance_futures_positions(row))
    positions = []
    for item in payload if isinstance(payload, list) else []:
        amount = _float(getattr(item, "positionAmt", 0.0))
        if abs(amount) <= 0:
            continue
        mark_price = _float(getattr(item, "markPrice", 0.0))
        entry_price = _float(getattr(item, "entryPrice", 0.0))
        positions.append(
            {
                "platform": "binance",
                "symbol": str(getattr(item, "symbol", "") or ""),
                "side": "long" if amount > 0 else "short",
                "quantity": abs(amount),
                "entry_price": entry_price,
                "mark_price": mark_price,
                "unrealized_pnl": _float(getattr(item, "unRealizedProfit", 0.0)),
                "margin_used": _float(getattr(item, "initialMargin", 0.0)) or abs(amount * mark_price),
                "liquidation_price": _optional_float(getattr(item, "liquidationPrice", None)),
            }
        )
    return positions


def binance_ticker_book(row: ExchangeCredential, symbol: str) -> dict[str, float]:
    ticker = _run_async(_nautilus_binance_futures_ticker(row, symbol))
    bid = _float(getattr(ticker, "bidPrice", 0.0))
    ask = _float(getattr(ticker, "askPrice", 0.0))
    bid_qty = _float(getattr(ticker, "bidQty", 0.0))
    ask_qty = _float(getattr(ticker, "askQty", 0.0))
    return {
        "bid": bid,
        "ask": ask,
        "depth_notional": min(bid * bid_qty, ask * ask_qty) if bid > 0 and ask > 0 else 0.0,
    }


def binance_futures_funding_history(row: ExchangeCredential, symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    rows = _run_async(_nautilus_binance_futures_funding_history(row, symbol, start_ms, end_ms))
    return [
        {
            "symbol": str(item.get("symbol") or symbol),
            "fundingRate": item.get("fundingRate"),
            "fundingTime": item.get("fundingTime"),
        }
        for item in rows
        if isinstance(item, dict)
    ]


def credential_fields_for_venue(venue: str) -> list[dict[str, Any]]:
    venue = normalize_venue(venue)
    if venue == "okx":
        return [
            {"name": "api_key", "label": "API Key", "secret": True, "required": True},
            {"name": "api_secret", "label": "API Secret", "secret": True, "required": True},
            {"name": "passphrase", "label": "Passphrase", "secret": True, "required": True},
        ]
    if venue in {"binance", "bybit", "kraken"}:
        return [
            {"name": "api_key", "label": "API Key", "secret": True, "required": True},
            {"name": "api_secret", "label": "API Secret", "secret": True, "required": True},
        ]
    if venue == "hyperliquid":
        return [
            {"name": "account_address", "label": "Account Address", "secret": False, "required": True},
            {"name": "secret_key", "label": "Secret Key", "secret": True, "required": False},
        ]
    if venue == "mt5":
        return [
            {"name": "login", "label": "Login", "secret": False, "required": True},
            {"name": "password", "label": "Password", "secret": True, "required": True},
            {"name": "server", "label": "Server", "secret": False, "required": True},
        ]
    return [
        {"name": "api_key", "label": "API Key", "secret": True, "required": False},
        {"name": "api_secret", "label": "API Secret", "secret": True, "required": False},
    ]


def _missing_required_fields(venue: str, credentials: dict[str, Any]) -> list[str]:
    missing = []
    for field in credential_fields_for_venue(venue):
        if field.get("required") and not str(credentials.get(str(field["name"])) or "").strip():
            missing.append(str(field["name"]))
    return missing


def _validate_binance(row: ExchangeCredential, credentials: dict[str, Any]) -> tuple[str, str]:
    try:
        payload = _run_async(_nautilus_binance_futures_account(row, credentials))
        can_trade = getattr(payload, "canTrade", None)
        assets = getattr(payload, "assets", []) or []
        wallet = getattr(payload, "totalWalletBalance", "")
        return "ok", f"Binance {row.environment} Futures 验证成功: canTrade={can_trade}, assets={len(assets)}, wallet={wallet}"
    except RuntimeError as futures_exc:
        futures_message = str(futures_exc)
    try:
        payload = _run_async(_nautilus_binance_spot_account(row, credentials))
    except RuntimeError as exc:
        return "failed", f"Futures: {futures_message}; Spot: {exc}"
    can_trade = getattr(payload, "canTrade", None)
    account_type = getattr(payload, "accountType", "spot")
    return "ok", f"Binance {row.environment} 账户验证成功: accountType={account_type}, canTrade={can_trade}"


async def _nautilus_binance_futures_account(row: ExchangeCredential, credentials: dict[str, Any] | None = None):
    account_api, _ = _nautilus_binance_futures_apis(row, credentials)
    try:
        return await account_api.query_futures_account_info()
    except Exception as exc:
        raise RuntimeError(f"nautilus futures account {exc}") from exc


async def _nautilus_binance_futures_positions(row: ExchangeCredential):
    account_api, _ = _nautilus_binance_futures_apis(row)
    try:
        return await account_api.query_futures_position_risk()
    except Exception as exc:
        raise RuntimeError(f"nautilus futures positions {exc}") from exc


async def _nautilus_binance_futures_ticker(row: ExchangeCredential, symbol: str):
    _, market_api = _nautilus_binance_futures_apis(row, credentials={})
    try:
        rows = await market_api.query_ticker_book(symbol=symbol)
    except Exception as exc:
        raise RuntimeError(f"nautilus futures ticker {exc}") from exc
    if not rows:
        raise RuntimeError(f"nautilus futures ticker empty: {symbol}")
    return rows[0]


async def _nautilus_binance_futures_funding_history(row: ExchangeCredential, symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    from nautilus_trader.adapters.binance.common.enums import BinanceSecurityType
    from nautilus_trader.adapters.binance.http.endpoint import BinanceHttpEndpoint
    from nautilus_trader.core.nautilus_pyo3 import HttpMethod

    _, market_api = _nautilus_binance_futures_apis(row, credentials={})

    class FundingRateEndpoint(BinanceHttpEndpoint):
        class GetParameters(msgspec.Struct, omit_defaults=True, frozen=True):
            symbol: str
            startTime: int | None = None
            endTime: int | None = None
            limit: int | None = None

        def __init__(self):
            super().__init__(
                market_api.client,
                {HttpMethod.GET: BinanceSecurityType.NONE},
                market_api.base_endpoint + "fundingRate",
            )
            self._get_resp_decoder = msgspec.json.Decoder()

        async def get(self, params: GetParameters) -> list[dict[str, Any]]:
            raw = await self._method(HttpMethod.GET, params)
            decoded = self._get_resp_decoder.decode(raw)
            return decoded if isinstance(decoded, list) else []

    endpoint = FundingRateEndpoint()
    try:
        return await endpoint.get(FundingRateEndpoint.GetParameters(symbol=_binance_symbol(symbol), startTime=start_ms, endTime=end_ms, limit=1000))
    except Exception as exc:
        raise RuntimeError(f"nautilus futures funding {exc}") from exc


async def _nautilus_binance_spot_account(row: ExchangeCredential, credentials: dict[str, Any] | None = None):
    account_api = _nautilus_binance_spot_account_api(row, credentials)
    try:
        return await account_api.query_spot_account_info()
    except Exception as exc:
        raise RuntimeError(f"nautilus spot account {exc}") from exc


def _nautilus_binance_futures_apis(row: ExchangeCredential, credentials: dict[str, Any] | None = None):
    from nautilus_trader.adapters.binance.common.enums import BinanceAccountType
    from nautilus_trader.adapters.binance.common.urls import get_http_base_url
    from nautilus_trader.adapters.binance.futures.http.account import BinanceFuturesAccountHttpAPI
    from nautilus_trader.adapters.binance.futures.http.market import BinanceFuturesMarketHttpAPI
    from nautilus_trader.adapters.binance.http.client import BinanceHttpClient
    from nautilus_trader.common.component import LiveClock

    credentials = credentials if credentials is not None else decrypt_credentials(row)
    clock = LiveClock()
    account_type = BinanceAccountType.USDT_FUTURES
    client = BinanceHttpClient(
        clock=clock,
        api_key=str(credentials.get("api_key") or "") or None,
        api_secret=str(credentials.get("api_secret") or "") or None,
        base_url=get_http_base_url(account_type, _binance_environment(row.environment), is_us=False),
    )
    return BinanceFuturesAccountHttpAPI(client, clock, account_type), BinanceFuturesMarketHttpAPI(client, account_type)


def _nautilus_binance_spot_account_api(row: ExchangeCredential, credentials: dict[str, Any] | None = None):
    from nautilus_trader.adapters.binance.common.enums import BinanceAccountType
    from nautilus_trader.adapters.binance.common.urls import get_http_base_url
    from nautilus_trader.adapters.binance.http.client import BinanceHttpClient
    from nautilus_trader.adapters.binance.spot.http.account import BinanceSpotAccountHttpAPI
    from nautilus_trader.common.component import LiveClock

    credentials = credentials if credentials is not None else decrypt_credentials(row)
    clock = LiveClock()
    account_type = BinanceAccountType.SPOT
    client = BinanceHttpClient(
        clock=clock,
        api_key=str(credentials.get("api_key") or "") or None,
        api_secret=str(credentials.get("api_secret") or "") or None,
        base_url=get_http_base_url(account_type, _binance_environment(row.environment), is_us=False),
    )
    return BinanceSpotAccountHttpAPI(client, clock, account_type)


def _binance_environment(value: str):
    from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment

    normalized = (value or "").strip().lower()
    if normalized in {"test", "testnet", "sandbox"}:
        return BinanceEnvironment.TESTNET
    if normalized == "demo":
        return BinanceEnvironment.DEMO
    return BinanceEnvironment.LIVE


def _binance_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if ":" in value:
        value = value.split(":", 1)[1]
    return value.replace("/", "").replace("-", "").replace("_", "")


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    result: dict[str, Any] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except Exception as exc:
            result["error"] = exc

    thread = Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _fernet() -> Fernet:
    settings = get_settings()
    # 中文注释：只保留一个主密钥来源，避免每个交易所都靠环境变量散落管理。
    secret = (settings.exchange_config_secret or settings.jwt_secret).strip()
    if not secret:
        raise ValueError("缺少交易所配置加密密钥")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def _redacted(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("***" if _is_sensitive_key(key) and val else _redacted(val)) for key, val in value.items()}
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(hint in normalized for hint in SENSITIVE_KEY_HINTS)


def _clean_credentials(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, str):
            item = item.strip()
        if item in ("", None):
            continue
        cleaned[str(key)] = item
    return cleaned
