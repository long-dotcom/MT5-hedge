from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict[str, Any]


class StrategySettingsIn(BaseModel):
    min_net_profit: float
    min_annualized_return: float
    signal_mode: str = "statistical"
    statistical_lookback_range: str = "1h"
    statistical_min_samples: int = 200
    reachable_entry_percentile: float = 0.75
    reachable_entry_zscore: float = 1.0
    cost_guard_percentile: float = 0.90
    min_unit_edge: float = 0.0
    min_total_profit: float = 0.5
    auto_close_enabled: bool = True
    auto_close_live_enabled: bool = False
    exit_target_percentile: float = 0.25
    auto_close_unit_profit_buffer: float = 0.0
    auto_close_min_profit: float = 0.0
    default_notional: float
    max_holding_minutes: int
    execution_mode: str
    paper_use_live_account_risk: bool = False
    auto_execute_enabled: bool = False
    auto_execute_paper_only: bool = True
    auto_execute_min_hold_ms: int = 300
    auto_execute_confirm_ticks: int = 2
    auto_execute_cooldown_seconds: int = 30
    auto_execute_max_per_symbol_open_groups: int = 1
    auto_execute_max_global_open_groups: int = 3
    auto_execute_min_net_profit: float = 0.0
    paper_decision_delay_ms_min: int = 50
    paper_decision_delay_ms_max: int = 200
    paper_hyperliquid_latency_ms_min: int = 80
    paper_hyperliquid_latency_ms_max: int = 200
    paper_mt5_latency_ms_min: int = 120
    paper_mt5_latency_ms_max: int = 350


class RiskSettingsIn(BaseModel):
    mode: str
    max_order_notional: float
    max_symbol_exposure: float
    max_total_leverage: float
    max_new_margin_fraction: float
    new_order_leverage: float
    min_margin_ratio: float
    max_slippage_bps: float
    max_market_age_seconds: int
    max_api_errors: int


class LiveTradingIn(BaseModel):
    enabled: bool
    confirmation: str = ""


class RiskModeIn(BaseModel):
    mode: str


class CloseHedgeGroupIn(BaseModel):
    reason: str = "manual"


class AdoptPositionIn(BaseModel):
    reason: str = "adopt external live position"
    symbol: str = ""


class HyperliquidProbeTestIn(BaseModel):
    symbol: str
    side: str = "buy"
    quantity: float | None = None
    reduce_only: bool = False
    submit: bool = False
    slippage: float | None = None
    confirmation: str = ""

    @field_validator("symbol", "side", "confirmation")
    @classmethod
    def strip_probe_text(cls, value: str) -> str:
        return value.strip()


class SymbolMappingIn(BaseModel):
    symbol: str
    hyperliquid_symbol: str
    mt5_symbol: str
    base_asset: str = ""
    quote_asset: str = "USD"
    contract_multiplier: float = 1.0
    min_order_size: float = 0.001
    min_entry_spread: float = 0.0
    max_close_spread: float = 0.0
    mt5_min_lot: float = 0.0
    mt5_volume_step: float = 0.0
    mt5_contract_size: float = 1.0
    mt5_currency_base: str = ""
    mt5_currency_profit: str = "USD"
    mt5_currency_margin: str = "USD"
    mt5_calc_mode: int = 0
    mt5_min_base_size: float = 0.0
    hyperliquid_min_base_size: float = 0.0
    hyperliquid_min_notional: float = 10.0
    execution_style: str = "taker_taker"
    hl_open_order_type: str = "market"
    hl_close_order_type: str = "market"
    hl_post_only: bool = False
    hl_maker_offset_bps: float = 1.0
    hl_order_ttl_seconds: int = 3
    hl_unfilled_action: str = "cancel"
    single_leg_action: str = "manual_intervention"
    mt5_open_order_type: str = "market"
    mt5_close_order_type: str = "market"
    mt5_session_enabled: bool = True
    mt5_session_auto_sync: bool = True
    mt5_session_template: str = "auto"
    mt5_session_timezone: str = "UTC"
    mt5_regular_sessions_json: str = "[]"
    mt5_close_only_sessions_json: str = "[]"
    mt5_quote_only_sessions_json: str = "[]"
    mt5_session_source: str = "manual"
    mt5_pre_close_no_open_minutes: int = 15
    mt5_post_open_cooldown_minutes: int = 10
    allow_hold_through_mt5_close: bool = False
    quantity_precision: int = 4
    price_precision: int = 2
    min_tick: float = 0.01
    max_slippage_bps: float = 8.0
    enabled: bool = True

    @field_validator("symbol", "hyperliquid_symbol", "mt5_symbol", "base_asset", "quote_asset", "mt5_currency_base", "mt5_currency_profit", "mt5_currency_margin", "mt5_session_template", "mt5_session_timezone", "mt5_session_source")
    @classmethod
    def strip_symbol_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_hyperliquid_symbol(self) -> "SymbolMappingIn":
        value = self.hyperliquid_symbol.strip()
        normalized = value.upper()
        if ":" not in value and "." not in value and "-" not in value and normalized.endswith("USD"):
            base = value[:-3]
            raise ValueError(f"Hyperliquid 标准永续请填写基础币符号 `{base}`，不要填写 MT5 符号 `{value}`")
        return self


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RowResponse(ORMModel):
    id: int
    created_at: datetime
