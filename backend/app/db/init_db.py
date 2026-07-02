from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.auth.security import hash_password, verify_password
from app.config.settings import INSECURE_DEFAULT_ADMIN_PASSWORD, get_settings, runtime_requires_strong_secrets
from app.db.models import Base, RiskSetting, StrategySetting, SymbolMapping, SystemSetting, User
from app.db.session import engine, IS_POSTGRESQL, IS_SQLITE
from app.accounts.sync import ensure_initial_account_snapshots
from app.market.symbols import seed_symbol_mappings_from_file


def _dialect_type(ddl: str) -> str:
    """Translate SQLite-style DDL fragment to the current database dialect."""
    if IS_POSTGRESQL:
        ddl = ddl.replace("DATETIME", "TIMESTAMP")
        ddl = ddl.replace("BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE")
        ddl = ddl.replace("BOOLEAN DEFAULT 1", "BOOLEAN DEFAULT TRUE")
    return ddl


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_schema_columns()
    with Session(engine) as db:
        seed_defaults(db)


def ensure_schema_columns() -> None:
    inspector = inspect(engine)
    if "symbol_mappings" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("symbol_mappings")}
    columns = {
        "leg_a_venue": "VARCHAR(32) DEFAULT 'hyperliquid'",
        "leg_a_symbol": "VARCHAR(64) DEFAULT ''",
        "leg_b_venue": "VARCHAR(32) DEFAULT 'mt5'",
        "leg_b_symbol": "VARCHAR(64) DEFAULT ''",
        "mt5_min_lot": "FLOAT DEFAULT 0.0",
        "min_entry_spread": "FLOAT DEFAULT 0.0",
        "max_close_spread": "FLOAT DEFAULT 0.0",
        "mt5_volume_step": "FLOAT DEFAULT 0.0",
        "mt5_contract_size": "FLOAT DEFAULT 1.0",
        "mt5_currency_base": "VARCHAR(16) DEFAULT ''",
        "mt5_currency_profit": "VARCHAR(16) DEFAULT 'USD'",
        "mt5_currency_margin": "VARCHAR(16) DEFAULT 'USD'",
        "mt5_calc_mode": "INTEGER DEFAULT 0",
        "mt5_min_base_size": "FLOAT DEFAULT 0.0",
        "leg_a_min_base_size": "FLOAT DEFAULT 0.0",
        "leg_a_min_notional": "FLOAT DEFAULT 10.0",
        "execution_style": "VARCHAR(64) DEFAULT 'taker_taker'",
        "hl_open_order_type": "VARCHAR(16) DEFAULT 'market'",
        "hl_close_order_type": "VARCHAR(16) DEFAULT 'market'",
        "hl_post_only": "BOOLEAN DEFAULT 0",
        "hl_maker_offset_bps": "FLOAT DEFAULT 1.0",
        "hl_order_ttl_seconds": "INTEGER DEFAULT 3",
        "hl_unfilled_action": "VARCHAR(32) DEFAULT 'cancel'",
        "single_leg_action": "VARCHAR(32) DEFAULT 'manual_intervention'",
        "mt5_open_order_type": "VARCHAR(16) DEFAULT 'market'",
        "mt5_close_order_type": "VARCHAR(16) DEFAULT 'market'",
        "mt5_session_enabled": "BOOLEAN DEFAULT 1",
        "mt5_session_auto_sync": "BOOLEAN DEFAULT 1",
        "mt5_session_template": "VARCHAR(64) DEFAULT 'auto'",
        "mt5_session_timezone": "VARCHAR(64) DEFAULT 'UTC'",
        "mt5_regular_sessions_json": "TEXT DEFAULT '[]'",
        "mt5_close_only_sessions_json": "TEXT DEFAULT '[]'",
        "mt5_quote_only_sessions_json": "TEXT DEFAULT '[]'",
        "mt5_session_source": "VARCHAR(64) DEFAULT 'manual'",
        "mt5_session_last_synced_at": "DATETIME",
        "mt5_pre_close_no_open_minutes": "INTEGER DEFAULT 15",
        "mt5_post_open_cooldown_minutes": "INTEGER DEFAULT 10",
        "allow_hold_through_mt5_close": "BOOLEAN DEFAULT 0",
    }
    with engine.begin() as conn:
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE symbol_mappings ADD COLUMN {name} {_dialect_type(ddl)}"))
        conn.execute(
            text(
                """
                UPDATE symbol_mappings
                SET leg_a_venue = CASE WHEN leg_a_venue IS NULL OR leg_a_venue = '' THEN 'hyperliquid' ELSE leg_a_venue END,
                    leg_a_symbol = CASE WHEN leg_a_symbol IS NULL OR leg_a_symbol = '' THEN leg_a_venue_symbol ELSE leg_a_symbol END,
                    leg_b_venue = CASE WHEN leg_b_venue IS NULL OR leg_b_venue = '' THEN 'mt5' ELSE leg_b_venue END,
                    leg_b_symbol = CASE WHEN leg_b_symbol IS NULL OR leg_b_symbol = '' THEN mt5_symbol ELSE leg_b_symbol END
                """
            )
        )
    if "orders" in inspector.get_table_names():
        existing_orders = {column["name"] for column in inspector.get_columns("orders")}
        order_columns = {
            "post_only": "BOOLEAN DEFAULT 0",
            "reduce_only": "BOOLEAN DEFAULT 0",
            "ttl_seconds": "INTEGER DEFAULT 0",
        }
        with engine.begin() as conn:
            for name, ddl in order_columns.items():
                if name not in existing_orders:
                    conn.execute(text(f"ALTER TABLE orders ADD COLUMN {name} {_dialect_type(ddl)}"))
    if "risk_settings" in inspector.get_table_names():
        existing_risk = {column["name"] for column in inspector.get_columns("risk_settings")}
        risk_columns = {
            "max_new_margin_fraction": "FLOAT DEFAULT 0.30",
            "new_order_leverage": "FLOAT DEFAULT 20.0",
        }
        with engine.begin() as conn:
            for name, ddl in risk_columns.items():
                if name not in existing_risk:
                    conn.execute(text(f"ALTER TABLE risk_settings ADD COLUMN {name} {_dialect_type(ddl)}"))
    if "account_snapshots" in inspector.get_table_names():
        existing_accounts = {column["name"] for column in inspector.get_columns("account_snapshots")}
        account_columns = {
            "portfolio_value": "FLOAT DEFAULT 0.0",
            "perp_equity": "FLOAT DEFAULT 0.0",
            "spot_balance": "FLOAT DEFAULT 0.0",
            "spot_hold": "FLOAT DEFAULT 0.0",
            "withdrawable": "FLOAT DEFAULT 0.0",
            "free_collateral": "FLOAT DEFAULT 0.0",
            "data_source": "VARCHAR(64) DEFAULT ''",
        }
        with engine.begin() as conn:
            for name, ddl in account_columns.items():
                if name not in existing_accounts:
                    conn.execute(text(f"ALTER TABLE account_snapshots ADD COLUMN {name} {_dialect_type(ddl)}"))
            conn.execute(
                text(
                    """
                    UPDATE account_snapshots
                    SET portfolio_value = CASE WHEN portfolio_value = 0.0 THEN equity ELSE portfolio_value END,
                        perp_equity = CASE WHEN perp_equity = 0.0 THEN equity ELSE perp_equity END,
                        withdrawable = CASE WHEN withdrawable = 0.0 THEN available_balance ELSE withdrawable END,
                        free_collateral = CASE WHEN free_collateral = 0.0 THEN available_balance ELSE free_collateral END
                    """
                )
            )
    if "strategy_settings" in inspector.get_table_names():
        existing_strategy = {column["name"] for column in inspector.get_columns("strategy_settings")}
        strategy_columns = {
            "signal_mode": "VARCHAR(32) DEFAULT 'statistical'",
            "statistical_lookback_range": "VARCHAR(16) DEFAULT '1h'",
            "statistical_min_samples": "INTEGER DEFAULT 200",
            "reachable_entry_percentile": "FLOAT DEFAULT 0.75",
            "reachable_entry_zscore": "FLOAT DEFAULT 1.0",
            "cost_guard_percentile": "FLOAT DEFAULT 0.90",
            "min_unit_edge": "FLOAT DEFAULT 0.0",
            "min_total_profit": "FLOAT DEFAULT 0.5",
            "auto_close_enabled": "BOOLEAN DEFAULT 1",
            "auto_close_live_enabled": "BOOLEAN DEFAULT 0",
            "exit_target_percentile": "FLOAT DEFAULT 0.25",
            "auto_close_unit_profit_buffer": "FLOAT DEFAULT 0.0",
            "auto_close_min_profit": "FLOAT DEFAULT 0.0",
            "paper_use_live_account_risk": "BOOLEAN DEFAULT 0",
            "auto_execute_enabled": "BOOLEAN DEFAULT 0",
            "auto_execute_paper_only": "BOOLEAN DEFAULT 1",
            "auto_execute_min_hold_ms": "INTEGER DEFAULT 300",
            "auto_execute_confirm_ticks": "INTEGER DEFAULT 2",
            "auto_execute_cooldown_seconds": "INTEGER DEFAULT 30",
            "auto_execute_max_per_symbol_open_groups": "INTEGER DEFAULT 1",
            "auto_execute_max_global_open_groups": "INTEGER DEFAULT 3",
            "auto_execute_min_net_profit": "FLOAT DEFAULT 0.0",
            "paper_decision_delay_ms_min": "INTEGER DEFAULT 50",
            "paper_decision_delay_ms_max": "INTEGER DEFAULT 200",
            "paper_leg_a_latency_ms_min": "INTEGER DEFAULT 80",
            "paper_leg_a_latency_ms_max": "INTEGER DEFAULT 200",
            "paper_leg_b_latency_ms_min": "INTEGER DEFAULT 120",
            "paper_leg_b_latency_ms_max": "INTEGER DEFAULT 350",
            "cb_cooldown_seconds": "FLOAT DEFAULT 3.0",
            "cb_initial_threshold": "FLOAT DEFAULT 0.75",
            "cb_baseline_multiplier": "FLOAT DEFAULT 2.0",
            "cb_min_baseline_samples": "INTEGER DEFAULT 50",
            "cb_detection_seconds": "FLOAT DEFAULT 5.0",
        }
        with engine.begin() as conn:
            for name, ddl in strategy_columns.items():
                if name not in existing_strategy:
                    conn.execute(text(f"ALTER TABLE strategy_settings ADD COLUMN {name} {_dialect_type(ddl)}"))
    if "spread_snapshots" in inspector.get_table_names():
        existing_spreads = {column["name"] for column in inspector.get_columns("spread_snapshots")}
        spread_columns = {
            "quantity": "FLOAT DEFAULT 1.0",
            "leg_b_quantity": "FLOAT DEFAULT 1.0",
            "leg_a_quantity": "FLOAT DEFAULT 1.0",
            "notional_currency": "VARCHAR(16) DEFAULT 'USD'",
            "fx_rate_to_usd": "FLOAT DEFAULT 1.0",
            "entry_spread": "FLOAT DEFAULT 0.0",
            "close_spread": "FLOAT DEFAULT 0.0",
            "mid_spread": "FLOAT DEFAULT 0.0",
            "spread_cost": "FLOAT DEFAULT 0.0",
            "unit_cost": "FLOAT DEFAULT 0.0",
            "unit_net_profit": "FLOAT DEFAULT 0.0",
        }
        with engine.begin() as conn:
            for name, ddl in spread_columns.items():
                if name not in existing_spreads:
                    conn.execute(text(f"ALTER TABLE spread_snapshots ADD COLUMN {name} {_dialect_type(ddl)}"))
            conn.execute(
                text(
                    """
                    UPDATE spread_snapshots
                    SET unit_cost = CASE WHEN quantity > 0 THEN total_cost / quantity ELSE total_cost END,
                        unit_net_profit = CASE WHEN quantity > 0 THEN net_profit / quantity ELSE net_profit END,
                        entry_spread = CASE WHEN entry_spread = 0.0 THEN gross_spread ELSE entry_spread END,
                        close_spread = CASE WHEN close_spread = 0.0 THEN gross_spread ELSE close_spread END,
                        mid_spread = CASE WHEN mid_spread = 0.0 THEN gross_spread ELSE mid_spread END,
                        spread_cost = CASE WHEN spread_cost = 0.0 THEN close_spread - entry_spread ELSE spread_cost END
                    WHERE (unit_cost = 0.0 AND total_cost != 0.0) OR entry_spread = 0.0 OR close_spread = 0.0
                    """
                )
            )
    if "arbitrage_opportunities" in inspector.get_table_names():
        existing_opportunities = {column["name"] for column in inspector.get_columns("arbitrage_opportunities")}
        opportunity_columns = {
            "leg_b_quantity": "FLOAT DEFAULT 1.0",
            "leg_a_quantity": "FLOAT DEFAULT 1.0",
            "notional_currency": "VARCHAR(16) DEFAULT 'USD'",
            "fx_rate_to_usd": "FLOAT DEFAULT 1.0",
            "trigger_leg_a_bid": "FLOAT DEFAULT 0.0",
            "trigger_leg_a_ask": "FLOAT DEFAULT 0.0",
            "trigger_leg_b_bid": "FLOAT DEFAULT 0.0",
            "trigger_leg_b_ask": "FLOAT DEFAULT 0.0",
            "unit_cost": "FLOAT DEFAULT 0.0",
            "unit_net_profit": "FLOAT DEFAULT 0.0",
            "entry_threshold": "FLOAT DEFAULT 0.0",
            "exit_target": "FLOAT DEFAULT 0.0",
            "overheat_threshold": "FLOAT DEFAULT 0.0",
            "signal_sample_count": "INTEGER DEFAULT 0",
        }
        with engine.begin() as conn:
            for name, ddl in opportunity_columns.items():
                if name not in existing_opportunities:
                    conn.execute(text(f"ALTER TABLE arbitrage_opportunities ADD COLUMN {name} {_dialect_type(ddl)}"))
            conn.execute(
                text(
                    """
                    UPDATE arbitrage_opportunities
                    SET unit_cost = CASE WHEN quantity > 0 THEN total_cost / quantity ELSE total_cost END,
                        unit_net_profit = CASE WHEN quantity > 0 THEN net_profit / quantity ELSE net_profit END
                    WHERE unit_cost = 0.0 AND total_cost != 0.0
                    """
                )
            )
    if "spread_current" in inspector.get_table_names():
        existing_current = {column["name"] for column in inspector.get_columns("spread_current")}
        current_columns = {
            "leg_b_quantity": "FLOAT DEFAULT 1.0",
            "leg_a_quantity": "FLOAT DEFAULT 1.0",
            "notional_currency": "VARCHAR(16) DEFAULT 'USD'",
            "fx_rate_to_usd": "FLOAT DEFAULT 1.0",
            "entry_spread": "FLOAT DEFAULT 0.0",
            "close_spread": "FLOAT DEFAULT 0.0",
            "mid_spread": "FLOAT DEFAULT 0.0",
            "spread_cost": "FLOAT DEFAULT 0.0",
        }
        with engine.begin() as conn:
            for name, ddl in current_columns.items():
                if name not in existing_current:
                    conn.execute(text(f"ALTER TABLE spread_current ADD COLUMN {name} {_dialect_type(ddl)}"))
            conn.execute(
                text(
                    """
                    UPDATE spread_current
                    SET entry_spread = CASE WHEN entry_spread = 0.0 THEN gross_spread ELSE entry_spread END,
                        close_spread = CASE WHEN close_spread = 0.0 THEN gross_spread ELSE close_spread END,
                        mid_spread = CASE WHEN mid_spread = 0.0 THEN gross_spread ELSE mid_spread END,
                        spread_cost = CASE WHEN spread_cost = 0.0 THEN close_spread - entry_spread ELSE spread_cost END
                    WHERE entry_spread = 0.0 OR close_spread = 0.0
                    """
                )
            )
    if "spread_buckets" in inspector.get_table_names():
        existing_buckets = {column["name"] for column in inspector.get_columns("spread_buckets")}
        bucket_columns = {
            "entry_spread": "FLOAT DEFAULT 0.0",
            "avg_entry_spread": "FLOAT DEFAULT 0.0",
            "avg_close_basis_spread": "FLOAT DEFAULT 0.0",
            "avg_mid_spread": "FLOAT DEFAULT 0.0",
            "avg_spread_cost": "FLOAT DEFAULT 0.0",
        }
        with engine.begin() as conn:
            for name, ddl in bucket_columns.items():
                if name not in existing_buckets:
                    conn.execute(text(f"ALTER TABLE spread_buckets ADD COLUMN {name} {_dialect_type(ddl)}"))
            conn.execute(
                text(
                    """
                    UPDATE spread_buckets
                    SET entry_spread = CASE WHEN entry_spread = 0.0 THEN close_spread ELSE entry_spread END,
                        avg_entry_spread = CASE WHEN avg_entry_spread = 0.0 THEN avg_spread ELSE avg_entry_spread END,
                        avg_close_basis_spread = CASE WHEN avg_close_basis_spread = 0.0 THEN avg_spread ELSE avg_close_basis_spread END,
                        avg_mid_spread = CASE WHEN avg_mid_spread = 0.0 THEN avg_spread ELSE avg_mid_spread END
                    WHERE entry_spread = 0.0 OR avg_entry_spread = 0.0 OR avg_close_basis_spread = 0.0
                    """
                )
            )
    if "hedge_groups" in inspector.get_table_names():
        existing_groups = {column["name"] for column in inspector.get_columns("hedge_groups")}
        group_columns = {
            "leg_b_quantity": "FLOAT DEFAULT 1.0",
            "leg_a_quantity": "FLOAT DEFAULT 1.0",
            "fees": "FLOAT DEFAULT 0.0",
            "funding": "FLOAT DEFAULT 0.0",
            "swap": "FLOAT DEFAULT 0.0",
            "trigger_spread": "FLOAT DEFAULT 0.0",
            "trigger_leg_a_bid": "FLOAT DEFAULT 0.0",
            "trigger_leg_a_ask": "FLOAT DEFAULT 0.0",
            "trigger_leg_b_bid": "FLOAT DEFAULT 0.0",
            "trigger_leg_b_ask": "FLOAT DEFAULT 0.0",
            "entry_spread": "FLOAT DEFAULT 0.0",
            "entry_threshold": "FLOAT DEFAULT 0.0",
            "exit_target": "FLOAT DEFAULT 0.0",
            "overheat_threshold": "FLOAT DEFAULT 0.0",
            "close_reason": "TEXT DEFAULT ''",
        }
        with engine.begin() as conn:
            for name, ddl in group_columns.items():
                if name not in existing_groups:
                    conn.execute(text(f"ALTER TABLE hedge_groups ADD COLUMN {name} {_dialect_type(ddl)}"))


def seed_defaults(db: Session) -> None:
    settings = get_settings()
    admin_user = db.query(User).filter(User.username == settings.admin_username).first()
    if not admin_user:
        db.add(
            User(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
                role="admin",
            )
        )
    elif runtime_requires_strong_secrets(settings) and verify_password(INSECURE_DEFAULT_ADMIN_PASSWORD, admin_user.password_hash):
        raise RuntimeError("不安全启动配置：数据库中的管理员账号仍使用默认密码。请先重置管理员密码后再以生产或实盘相关模式启动。")
    if not db.query(StrategySetting).first():
        db.add(StrategySetting(execution_mode=settings.default_execution_mode))
    if not db.query(RiskSetting).first():
        db.add(RiskSetting())
    if not db.query(SystemSetting).filter(SystemSetting.key == "live_trading_enabled").first():
        db.add(SystemSetting(key="live_trading_enabled", value="false"))
    db.commit()
    ensure_initial_account_snapshots(db)
    seed_flag = db.query(SystemSetting).filter(SystemSetting.key == "symbol_mappings_seeded").first()
    if not seed_flag:
        # 中文注释：配置文件只作为首次启动的种子数据，后续增删改都以数据库为准，避免重启覆盖前端保存的映射。
        if not db.query(SymbolMapping).first():
            seed_symbol_mappings_from_file(db)
        db.add(SystemSetting(key="symbol_mappings_seeded", value="true"))
        db.commit()
