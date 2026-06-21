import os
from dataclasses import dataclass, fields
from functools import lru_cache
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[3]
HYPERLIQUID_MAINNET_INFO_URL = "https://api.hyperliquid.xyz/info"
HYPERLIQUID_TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz/info"


def _load_env_file() -> dict[str, str]:
    path = ROOT_DIR / ".env"
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _coerce(value: str, default):
    if isinstance(default, bool):
        return value.lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return value


@dataclass
class Settings:
    app_name: str = "MT5 Hedge"
    environment: str = "local"
    database_url: str = f"sqlite:///{ROOT_DIR / 'data' / 'mt5_hedge.db'}"
    jwt_secret: str = "change-me-before-live"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 720
    admin_username: str = "admin"
    admin_password: str = "admin123"
    default_execution_mode: str = "paper"
    symbol_mapping_path: str = str(ROOT_DIR / "config" / "symbol_mappings.yaml")
    live_trading_enabled: bool = False
    live_trading_confirmation: str = ""
    scanner_interval_seconds: int = 15
    scanner_interval_ms: int = 0
    candidate_interval_seconds: int = 5
    spread_history_interval_seconds: int = 5
    spread_bucket_seconds: int = 5
    stream_interval_ms: int = 1000
    quote_source_mode: str = "paper"
    paper_quote_interval_ms: int = 200
    mt5_quote_poll_interval_ms: int = 200
    hyperliquid_http_poll_interval_ms: int = 1000
    loose_quote_sync_ms: int = 3000
    strict_quote_sync_ms: int = 500
    quote_stale_ms: int = 1500
    hyperliquid_market_data_source: str = "native"
    hyperliquid_market_data_fallback: str = "native"
    hyperliquid_l2book_fast_enabled: bool = True
    hyperliquid_info_url: str = "https://api.hyperliquid.xyz/info"
    hyperliquid_ws_url: str = "wss://api.hyperliquid.xyz/ws"
    hyperliquid_default_taker_fee_rate: float = 0.00045
    hyperliquid_default_maker_fee_rate: float = 0.00015
    hyperliquid_default_min_notional: float = 10.0
    hyperliquid_fee_round_trips: float = 2.0
    mt5_default_commission_rate: float = 0.0
    mt5_spread_rebate_rate: float = 0.20
    mt5_swap_free: bool = True
    mt5_session_cache_ttl_seconds: int = 30
    mt5_session_tick_stale_seconds: int = 120
    default_slippage_bps: float = 0.0
    default_fx_cost_rate: float = 0.0
    fx_fallback_rates: str = '{"JPY":0.00625}'
    cost_cache_ttl_seconds: int = 60

    hyperliquid_account_address: str = ""
    nautilus_hyperliquid_enabled: bool = False
    nautilus_hyperliquid_submit_enabled: bool = False
    nautilus_hyperliquid_order_timeout_seconds: float = 15.0
    execution_reconcile_pending_stale_seconds: int = 300
    nautilus_hyperliquid_environment: str = "testnet"
    nautilus_hyperliquid_product_types: str = "PERP,PERP_HIP3"
    nautilus_hyperliquid_private_key: str = ""
    nautilus_hyperliquid_vault_address: str = ""
    nautilus_trader_id: str = "MT5-HEDGE-001"
    mt5_live_order_enabled: bool = False
    mt5_order_deviation_points: int = 20
    mt5_order_magic: int = 260620
    mt5_login: str = ""
    mt5_password: str = ""
    mt5_server: str = ""


def hyperliquid_execution_info_url(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    configured_url = getattr(settings, "hyperliquid_info_url", HYPERLIQUID_MAINNET_INFO_URL)
    known_urls = {HYPERLIQUID_MAINNET_INFO_URL, HYPERLIQUID_TESTNET_INFO_URL}
    if configured_url not in known_urls:
        return configured_url
    environment = str(getattr(settings, "nautilus_hyperliquid_environment", "") or "").strip().lower()
    if environment == "testnet":
        return HYPERLIQUID_TESTNET_INFO_URL
    if environment == "mainnet":
        return HYPERLIQUID_MAINNET_INFO_URL
    return configured_url


@lru_cache
def get_settings() -> Settings:
    env_file = _load_env_file()
    values = {}
    defaults = Settings()
    for item in fields(Settings):
        env_key = item.name.upper()
        raw_value = os.getenv(env_key, env_file.get(env_key))
        if raw_value is not None:
            values[item.name] = _coerce(raw_value, getattr(defaults, item.name))
    return Settings(**values)
