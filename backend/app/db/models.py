from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(32), default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(128))
    resource: Mapped[str] = mapped_column(String(128), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    ip_address: Mapped[str] = mapped_column(String(64), default="")


class SystemSetting(Base, TimestampMixin):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class StrategySetting(Base, TimestampMixin):
    __tablename__ = "strategy_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    min_net_profit: Mapped[float] = mapped_column(Float, default=5.0)
    min_annualized_return: Mapped[float] = mapped_column(Float, default=0.08)
    signal_mode: Mapped[str] = mapped_column(String(32), default="statistical")
    statistical_lookback_range: Mapped[str] = mapped_column(String(16), default="1h")
    statistical_min_samples: Mapped[int] = mapped_column(Integer, default=200)
    reachable_entry_percentile: Mapped[float] = mapped_column(Float, default=0.75)
    reachable_entry_zscore: Mapped[float] = mapped_column(Float, default=1.0)
    cost_guard_percentile: Mapped[float] = mapped_column(Float, default=0.90)
    min_unit_edge: Mapped[float] = mapped_column(Float, default=0.0)
    min_total_profit: Mapped[float] = mapped_column(Float, default=0.5)
    auto_close_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_close_live_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    exit_target_percentile: Mapped[float] = mapped_column(Float, default=0.25)
    auto_close_unit_profit_buffer: Mapped[float] = mapped_column(Float, default=0.0)
    auto_close_min_profit: Mapped[float] = mapped_column(Float, default=0.0)
    default_notional: Mapped[float] = mapped_column(Float, default=1000.0)
    max_holding_minutes: Mapped[int] = mapped_column(Integer, default=240)
    execution_mode: Mapped[str] = mapped_column(String(32), default="paper")
    paper_use_live_account_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_execute_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_execute_paper_only: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_execute_min_hold_ms: Mapped[int] = mapped_column(Integer, default=300)
    auto_execute_confirm_ticks: Mapped[int] = mapped_column(Integer, default=2)
    auto_execute_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=30)
    auto_execute_max_per_symbol_open_groups: Mapped[int] = mapped_column(Integer, default=1)
    auto_execute_max_global_open_groups: Mapped[int] = mapped_column(Integer, default=3)
    auto_execute_min_net_profit: Mapped[float] = mapped_column(Float, default=0.0)
    paper_decision_delay_ms_min: Mapped[int] = mapped_column(Integer, default=50)
    paper_decision_delay_ms_max: Mapped[int] = mapped_column(Integer, default=200)
    paper_hyperliquid_latency_ms_min: Mapped[int] = mapped_column(Integer, default=80)
    paper_hyperliquid_latency_ms_max: Mapped[int] = mapped_column(Integer, default=200)
    paper_mt5_latency_ms_min: Mapped[int] = mapped_column(Integer, default=120)
    paper_mt5_latency_ms_max: Mapped[int] = mapped_column(Integer, default=350)


class RiskSetting(Base, TimestampMixin):
    __tablename__ = "risk_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(32), default="normal")
    max_order_notional: Mapped[float] = mapped_column(Float, default=2000.0)
    max_symbol_exposure: Mapped[float] = mapped_column(Float, default=5000.0)
    max_total_leverage: Mapped[float] = mapped_column(Float, default=2.0)
    max_new_margin_fraction: Mapped[float] = mapped_column(Float, default=0.30)
    new_order_leverage: Mapped[float] = mapped_column(Float, default=20.0)
    min_margin_ratio: Mapped[float] = mapped_column(Float, default=0.35)
    max_slippage_bps: Mapped[float] = mapped_column(Float, default=8.0)
    max_market_age_seconds: Mapped[int] = mapped_column(Integer, default=10)
    max_api_errors: Mapped[int] = mapped_column(Integer, default=3)


class SymbolMapping(Base, TimestampMixin):
    __tablename__ = "symbol_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    hyperliquid_symbol: Mapped[str] = mapped_column(String(64))
    mt5_symbol: Mapped[str] = mapped_column(String(64))
    base_asset: Mapped[str] = mapped_column(String(32), default="")
    quote_asset: Mapped[str] = mapped_column(String(32), default="USD")
    contract_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    min_order_size: Mapped[float] = mapped_column(Float, default=0.001)
    mt5_min_lot: Mapped[float] = mapped_column(Float, default=0.0)
    mt5_volume_step: Mapped[float] = mapped_column(Float, default=0.0)
    mt5_contract_size: Mapped[float] = mapped_column(Float, default=1.0)
    mt5_currency_base: Mapped[str] = mapped_column(String(16), default="")
    mt5_currency_profit: Mapped[str] = mapped_column(String(16), default="USD")
    mt5_currency_margin: Mapped[str] = mapped_column(String(16), default="USD")
    mt5_calc_mode: Mapped[int] = mapped_column(Integer, default=0)
    mt5_min_base_size: Mapped[float] = mapped_column(Float, default=0.0)
    hyperliquid_min_base_size: Mapped[float] = mapped_column(Float, default=0.0)
    hyperliquid_min_notional: Mapped[float] = mapped_column(Float, default=10.0)
    execution_style: Mapped[str] = mapped_column(String(64), default="taker_taker")
    hl_open_order_type: Mapped[str] = mapped_column(String(16), default="market")
    hl_close_order_type: Mapped[str] = mapped_column(String(16), default="market")
    hl_post_only: Mapped[bool] = mapped_column(Boolean, default=False)
    hl_maker_offset_bps: Mapped[float] = mapped_column(Float, default=1.0)
    hl_order_ttl_seconds: Mapped[int] = mapped_column(Integer, default=3)
    hl_unfilled_action: Mapped[str] = mapped_column(String(32), default="cancel")
    single_leg_action: Mapped[str] = mapped_column(String(32), default="manual_intervention")
    mt5_open_order_type: Mapped[str] = mapped_column(String(16), default="market")
    mt5_close_order_type: Mapped[str] = mapped_column(String(16), default="market")
    mt5_pre_close_no_open_minutes: Mapped[int] = mapped_column(Integer, default=15)
    mt5_post_open_cooldown_minutes: Mapped[int] = mapped_column(Integer, default=10)
    allow_hold_through_mt5_close: Mapped[bool] = mapped_column(Boolean, default=False)
    quantity_precision: Mapped[int] = mapped_column(Integer, default=4)
    price_precision: Mapped[int] = mapped_column(Integer, default=2)
    min_tick: Mapped[float] = mapped_column(Float, default=0.01)
    max_slippage_bps: Mapped[float] = mapped_column(Float, default=8.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AccountSnapshot(Base, TimestampMixin):
    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    equity: Mapped[float] = mapped_column(Float)
    available_balance: Mapped[float] = mapped_column(Float)
    margin_used: Mapped[float] = mapped_column(Float, default=0.0)
    margin_ratio: Mapped[float] = mapped_column(Float, default=1.0)
    currency: Mapped[str] = mapped_column(String(16), default="USD")
    portfolio_value: Mapped[float] = mapped_column(Float, default=0.0)
    perp_equity: Mapped[float] = mapped_column(Float, default=0.0)
    spot_balance: Mapped[float] = mapped_column(Float, default=0.0)
    spot_hold: Mapped[float] = mapped_column(Float, default=0.0)
    withdrawable: Mapped[float] = mapped_column(Float, default=0.0)
    free_collateral: Mapped[float] = mapped_column(Float, default=0.0)
    data_source: Mapped[str] = mapped_column(String(64), default="")


class Position(Base, TimestampMixin):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    mark_price: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    margin_used: Mapped[float] = mapped_column(Float, default=0.0)
    liquidation_price: Mapped[float | None] = mapped_column(Float, nullable=True)


class MarketSnapshot(Base, TimestampMixin):
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    bid: Mapped[float] = mapped_column(Float)
    ask: Mapped[float] = mapped_column(Float)
    mid: Mapped[float] = mapped_column(Float)
    depth_notional: Mapped[float] = mapped_column(Float, default=0.0)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SpreadCurrent(Base, TimestampMixin):
    __tablename__ = "spread_current"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    direction: Mapped[str] = mapped_column(String(32))
    hyperliquid_bid: Mapped[float] = mapped_column(Float)
    hyperliquid_ask: Mapped[float] = mapped_column(Float)
    mt5_bid: Mapped[float] = mapped_column(Float)
    mt5_ask: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    mt5_quantity: Mapped[float] = mapped_column(Float, default=1.0)
    hyperliquid_quantity: Mapped[float] = mapped_column(Float, default=1.0)
    notional_currency: Mapped[str] = mapped_column(String(16), default="USD")
    fx_rate_to_usd: Mapped[float] = mapped_column(Float, default=1.0)
    gross_spread: Mapped[float] = mapped_column(Float)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    unit_net_profit: Mapped[float] = mapped_column(Float, default=0.0)
    total_cost: Mapped[float] = mapped_column(Float)
    net_profit: Mapped[float] = mapped_column(Float)
    annualized_return: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")
    sampled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SpreadBucket(Base, TimestampMixin):
    __tablename__ = "spread_buckets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(32), index=True)
    bucket_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    bucket_seconds: Mapped[int] = mapped_column(Integer, default=5)
    open_spread: Mapped[float] = mapped_column(Float)
    high_spread: Mapped[float] = mapped_column(Float)
    low_spread: Mapped[float] = mapped_column(Float)
    close_spread: Mapped[float] = mapped_column(Float)
    avg_spread: Mapped[float] = mapped_column(Float)
    avg_unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    avg_unit_net_profit: Mapped[float] = mapped_column(Float, default=0.0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)


class SpreadSnapshot(Base, TimestampMixin):
    __tablename__ = "spread_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(32))
    hyperliquid_bid: Mapped[float] = mapped_column(Float)
    hyperliquid_ask: Mapped[float] = mapped_column(Float)
    mt5_bid: Mapped[float] = mapped_column(Float)
    mt5_ask: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    mt5_quantity: Mapped[float] = mapped_column(Float, default=1.0)
    hyperliquid_quantity: Mapped[float] = mapped_column(Float, default=1.0)
    notional_currency: Mapped[str] = mapped_column(String(16), default="USD")
    fx_rate_to_usd: Mapped[float] = mapped_column(Float, default=1.0)
    gross_spread: Mapped[float] = mapped_column(Float)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    unit_net_profit: Mapped[float] = mapped_column(Float, default=0.0)
    total_cost: Mapped[float] = mapped_column(Float)
    net_profit: Mapped[float] = mapped_column(Float)
    annualized_return: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")


class ArbitrageOpportunity(Base, TimestampMixin):
    __tablename__ = "arbitrage_opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(32))
    notional: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    mt5_quantity: Mapped[float] = mapped_column(Float, default=1.0)
    hyperliquid_quantity: Mapped[float] = mapped_column(Float, default=1.0)
    notional_currency: Mapped[str] = mapped_column(String(16), default="USD")
    fx_rate_to_usd: Mapped[float] = mapped_column(Float, default=1.0)
    gross_spread: Mapped[float] = mapped_column(Float)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    unit_net_profit: Mapped[float] = mapped_column(Float, default=0.0)
    total_cost: Mapped[float] = mapped_column(Float)
    net_profit: Mapped[float] = mapped_column(Float)
    annualized_return: Mapped[float] = mapped_column(Float)
    entry_threshold: Mapped[float] = mapped_column(Float, default=0.0)
    exit_target: Mapped[float] = mapped_column(Float, default=0.0)
    overheat_threshold: Mapped[float] = mapped_column(Float, default=0.0)
    signal_sample_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="candidate")
    reject_reason: Mapped[str] = mapped_column(Text, default="")


class HedgeGroup(Base, TimestampMixin):
    __tablename__ = "hedge_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending_open")
    execution_mode: Mapped[str] = mapped_column(String(32), default="paper")
    notional: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    mt5_quantity: Mapped[float] = mapped_column(Float, default=1.0)
    hyperliquid_quantity: Mapped[float] = mapped_column(Float, default=1.0)
    open_cost: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    funding: Mapped[float] = mapped_column(Float, default=0.0)
    swap: Mapped[float] = mapped_column(Float, default=0.0)
    trigger_spread: Mapped[float] = mapped_column(Float, default=0.0)
    entry_spread: Mapped[float] = mapped_column(Float, default=0.0)
    entry_threshold: Mapped[float] = mapped_column(Float, default=0.0)
    exit_target: Mapped[float] = mapped_column(Float, default=0.0)
    overheat_threshold: Mapped[float] = mapped_column(Float, default=0.0)
    close_reason: Mapped[str] = mapped_column(Text, default="")
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="system")
    events: Mapped[list["HedgeGroupEvent"]] = relationship(back_populates="hedge_group")


class HedgeGroupEvent(Base, TimestampMixin):
    __tablename__ = "hedge_group_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hedge_group_id: Mapped[int] = mapped_column(ForeignKey("hedge_groups.id"))
    event_type: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")
    hedge_group: Mapped[HedgeGroup] = relationship(back_populates="events")


class Order(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hedge_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(16))
    order_type: Mapped[str] = mapped_column(String(16), default="market")
    post_only: Mapped[bool] = mapped_column(Boolean, default=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    ttl_seconds: Mapped[int] = mapped_column(Integer, default=0)
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="new")
    external_order_id: Mapped[str] = mapped_column(String(128), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")


class Fill(Base, TimestampMixin):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)


class PnlSnapshot(Base, TimestampMixin):
    __tablename__ = "pnl_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hedge_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    equity: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float)


class SystemLog(Base, TimestampMixin):
    __tablename__ = "system_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(16), index=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[str] = mapped_column(Text, default="")


class RiskEvent(Base, TimestampMixin):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(16), default="warning")
    rule: Mapped[str] = mapped_column(String(128))
    message: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(String(32), default="")


class Alert(Base, TimestampMixin):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    title: Mapped[str] = mapped_column(String(128))
    message: Mapped[str] = mapped_column(Text)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)


class WorkerRun(Base, TimestampMixin):
    __tablename__ = "worker_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_name: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
