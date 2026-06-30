from datetime import datetime, timedelta, timezone
import json
import time
from importlib import reload
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analytics.spreads import SpreadPoint, downsample_spreads, load_spread_points, summarize_spreads
from app.analytics.funding import FundingPoint, bucket_funding_points, summarize_funding
from app.analytics.lead_lag import lead_lag_report
from app.auth.security import hash_password
from app.market import scanner as scanner_module
from app.market import symbols as symbol_module
from app.db import init_db as init_db_module
from app.db.models import AccountSnapshot, Alert, ArbitrageOpportunity, AuditLog, Base, Fill, HedgeGroup, HedgeGroupEvent, Order, Position, RiskEvent, RiskSetting, SpreadCurrent, SpreadDirectionCurrent, StrategySetting, SymbolMapping, SystemLog, SystemSetting, User
from app.execution.auto_closer import evaluate_auto_close, run_auto_close
from app.execution.auto_executor import run_auto_execute
from app.execution.carry_costs import _mt5_swap_cost, _paper_hyperliquid_funding_cost
from app.execution import gateway as gateway_module
from app.execution.engine import _effective_close_exit_target, _execution_adapters, _final_close_still_executable, _has_position_effect, _is_pending_result, _maker_price, close_hedge_group, open_hedge_group
from app.execution.gateway import AdapterExecutionGateway, FillEvent, GatewayOrderResult, LegOrderIntent, OrderEvent, build_execution_gateway
from app.execution.hedge_pool import HedgeGroupSnapshot, hedge_pool
from app.execution.persistence import persist_hedge_pool_events
from app.execution.readiness import live_execution_readiness, paper_execution_readiness
from app.execution.reconciler import reconcile_hedge_group, reconcile_orphan_positions, reconcile_residual_positions, sync_live_positions
from app.config.settings import HYPERLIQUID_MAINNET_INFO_URL, HYPERLIQUID_TESTNET_INFO_URL, Settings, enforce_runtime_security, hyperliquid_execution_info_url, insecure_runtime_reasons
from app.api import router as api_router
from app.diagnostics.pipeline import _pool_payload
from app.market.mt5_schedule import apply_mt5_session_template, infer_template, local_schedule_state
from app.market.mt5_sessions import MT5SessionState, mt5_action_allowed, mt5_session_state
from app.market.orderbook import order_book_cache, simulate_market_fill
from app.market.scan_state import scan_state_store
from app.risk.engine import pre_trade_check
from app.market.quotes import QuoteCache, QuoteSynchronizer, quote_cache
from app.adapters.paper import PaperAdapter
from app.adapters.base import AdapterOrder, AdapterOrderResult
from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.mt5 import MT5Adapter, mt5_demo_order_check
from app.schemas import AdoptPositionIn
from app.schemas import SymbolMappingIn
from app.strategy.cost import estimate_cost
from app.strategy.live_costs import _estimate_mt5_swap_cost, _hyperliquid_effective_fee_rates
from app.strategy.statistical_signal import evaluate_entry_signal, refresh_signal_stats_cache
from app.strategy.spread_math import spreads_for_direction
from app.strategy.signals import evaluate_signal
from app.workers.market_data import MarketDataManager, _exchange_time_from_hyperliquid_ms, hyperliquid_symbol_map, l2book_subscription


def test_cost_model_positive_total() -> None:
    cost = estimate_cost(1000, 64990, 65010, 8)
    assert cost.total > 0
    assert cost.mt5_spread > 0


def test_relative_sqlite_database_url_resolves_from_project_root(monkeypatch) -> None:
    import app.db.session as session_module

    monkeypatch.setenv("DATABASE_URL", "sqlite:///data/mt5_hedge.db")
    try:
        session_module.get_settings.cache_clear()
        reloaded = reload(session_module)
        expected = (reloaded.ROOT_DIR / "data" / "mt5_hedge.db").as_posix()
        assert expected in str(reloaded.engine.url).replace("\\", "/")
    finally:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        session_module.get_settings.cache_clear()
        reload(session_module)


def test_runtime_security_allows_local_defaults() -> None:
    settings = Settings(environment="local")

    enforce_runtime_security(settings)

    assert insecure_runtime_reasons(settings)


def test_runtime_security_rejects_production_defaults() -> None:
    settings = Settings(environment="production")

    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        enforce_runtime_security(settings)


def test_runtime_security_rejects_live_defaults_even_in_local() -> None:
    settings = Settings(environment="local", live_trading_enabled=True)

    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        enforce_runtime_security(settings)


def test_runtime_security_accepts_production_custom_secrets() -> None:
    settings = Settings(
        environment="production",
        jwt_secret="a-prod-secret-with-enough-entropy",
        admin_password="not-the-default-password",
    )

    enforce_runtime_security(settings)

    assert insecure_runtime_reasons(settings) == []


def test_seed_defaults_rejects_existing_default_admin_in_secure_runtime(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(User(username="admin", password_hash=hash_password("admin123"), role="admin"))
    db.commit()
    monkeypatch.setattr(
        init_db_module,
        "get_settings",
        lambda: Settings(environment="production", jwt_secret="strong-secret", admin_password="changed-password"),
    )

    with pytest.raises(RuntimeError, match="默认密码"):
        init_db_module.seed_defaults(db)

    db.close()


def test_stream_auth_rejects_missing_bearer_header() -> None:
    request = SimpleNamespace(headers={})

    with pytest.raises(HTTPException) as exc:
        api_router.bearer_token_from_request(request)

    assert exc.value.status_code == 401


def test_stream_auth_reads_bearer_header_without_query_token() -> None:
    request = SimpleNamespace(headers={"authorization": "Bearer secure-token"})

    assert api_router.bearer_token_from_request(request) == "secure-token"


def test_mt5_spread_rebate_reduces_spread_cost() -> None:
    cost = estimate_cost(
        notional=1000,
        mt5_bid=100,
        mt5_ask=101,
        max_slippage_bps=0,
        quantity=1,
        hyperliquid_bid=100,
        hyperliquid_ask=101,
        hyperliquid_fee_rate=0,
        hyperliquid_funding_rate=0,
        mt5_commission_rate=0,
        mt5_swap_cost=0,
        mt5_spread_rebate_rate=0.2,
    )
    assert round(cost.mt5_spread, 6) == round((1 / 100.5) * 1000 * 0.8, 6)


def test_signal_rejects_unprofitable() -> None:
    signal = evaluate_signal(-1, 0.2, 5, 0.08)
    assert signal.status == "rejected"


def test_risk_blocks_paused_mode() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        db.add(StrategySetting())
        db.add(RiskSetting(mode="paused"))
        db.add(SymbolMapping(symbol="BTC", hyperliquid_symbol="BTC", mt5_symbol="BTCUSD"))
        db.commit()
        decision = pre_trade_check(db, "BTC", 1000, 1, datetime.now(timezone.utc).replace(tzinfo=None))
        assert not decision.allowed


def test_quote_synchronizer_rejects_unsynced_quotes() -> None:
    cache = QuoteCache()
    sync = QuoteSynchronizer(cache)
    cache.put("hyperliquid", "BTC", 100, 101, 10000, "test")
    quote = cache.put("mt5", "BTC", 102, 103, 10000, "test")
    object.__setattr__(quote, "local_recv_ts", quote.local_recv_ts.replace(year=quote.local_recv_ts.year - 1))
    synced, reason = sync.synchronized("BTC", "strict", max_time_diff_ms=100, max_age_ms=1000)
    assert synced is None
    assert "过期" in reason or "未对齐" in reason


def test_quote_synchronizer_accepts_aligned_quotes() -> None:
    cache = QuoteCache()
    sync = QuoteSynchronizer(cache)
    cache.put("hyperliquid", "BTC", 100, 101, 10000, "test")
    cache.put("mt5", "BTC", 102, 103, 10000, "test")
    synced, reason = sync.synchronized("BTC", "strict", max_time_diff_ms=500, max_age_ms=1000)
    assert synced is not None
    assert reason == ""


def test_hyperliquid_fast_l2book_subscription_includes_fast_flag() -> None:
    assert l2book_subscription("xyz:JP225", fast=True) == {"type": "l2Book", "coin": "xyz:JP225", "fast": True}
    assert l2book_subscription("BTC", fast=False) == {"type": "l2Book", "coin": "BTC"}


def test_hyperliquid_symbol_map_can_include_standard_and_hip3_symbols() -> None:
    mappings = [
        SimpleNamespace(symbol="BTC", hyperliquid_symbol="BTC"),
        SimpleNamespace(symbol="JP225", hyperliquid_symbol="xyz:JP225"),
    ]

    assert hyperliquid_symbol_map(mappings, hip3_only=False) == {"BTC": "BTC", "xyz:JP225": "JP225"}
    assert hyperliquid_symbol_map(mappings, hip3_only=True) == {"xyz:JP225": "JP225"}


def test_hyperliquid_l2book_message_writes_exchange_timestamp() -> None:
    manager = MarketDataManager()
    cache = QuoteCache()
    import app.workers.market_data as market_data_module

    original_worker_cache = market_data_module.quote_cache
    try:
        market_data_module.quote_cache = cache
        payload = {
            "channel": "l2Book",
            "data": {
                "coin": "xyz:JP225",
                "time": 1_782_040_271_224,
                "levels": [
                    [{"px": "72301.0", "sz": "0.00051", "n": 1}],
                    [{"px": "72499.0", "sz": "0.00048", "n": 1}],
                ],
            },
        }

        manager._handle_hyperliquid_message(payload, {"xyz:JP225": "JP225"}, "hyperliquid_l2Book_fast")

        quote = cache.latest("hyperliquid", "JP225")
        assert quote is not None
        assert quote.bid == 72301.0
        assert quote.ask == 72499.0
        assert quote.source == "hyperliquid_l2Book_fast"
        assert quote.exchange_ts == _exchange_time_from_hyperliquid_ms(1_782_040_271_224)
    finally:
        market_data_module.quote_cache = original_worker_cache


def test_l2_market_fill_walks_multiple_levels() -> None:
    book = order_book_cache.put(
        "hyperliquid",
        "L2TEST",
        bids=[(100.0, 1.0), (99.0, 2.0)],
        asks=[(101.0, 1.0), (102.0, 2.0)],
        source="test",
    )

    fill = simulate_market_fill(book, "buy", 2.0)

    assert fill.enough_liquidity
    assert fill.filled_quantity == 2.0
    assert fill.average_price == 101.5
    assert fill.worst_price == 102.0


def test_scanner_liquidity_uses_l2_before_top_depth() -> None:
    order_book_cache.put(
        "hyperliquid",
        "OIL-L2",
        bids=[(73.70, 1.0), (73.69, 100.0)],
        asks=[(73.72, 1.0), (73.73, 100.0)],
        source="test",
    )

    enough = scanner_module._hyperliquid_liquidity_reason("OIL-L2", "sell", 70.0, 5000.0, 100.0)
    not_enough = scanner_module._hyperliquid_liquidity_reason("OIL-L2", "sell", 200.0, 5000.0, 100.0)

    assert enough == ""
    assert "L2 深度不足" in not_enough


def test_live_market_data_starts_hyperliquid_ws_without_http_polling(monkeypatch) -> None:
    manager = MarketDataManager()
    started = []

    monkeypatch.setattr(
        "app.workers.market_data.get_settings",
        lambda: SimpleNamespace(quote_source_mode="live"),
    )
    monkeypatch.setattr(manager, "_start_thread", lambda name, target: started.append(name))
    try:
        manager.start()
        assert started == ["hyperliquid-ws", "mt5-polling"]
    finally:
        manager.stop()


def test_mt5_points_swap_cost() -> None:
    cost = _estimate_mt5_swap_cost(swap_value=-34.2, swap_mode=1, point=0.01, contract_size=1.0, quantity=0.5, holding_days=1)
    assert cost == 0.171


def test_mt5_positive_swap_reduces_cost() -> None:
    cost = _estimate_mt5_swap_cost(swap_value=10.0, swap_mode=1, point=0.01, contract_size=1.0, quantity=1, holding_days=1)
    assert cost == -0.1


def test_hyperliquid_short_positive_funding_reduces_cost() -> None:
    cost = estimate_cost(
        notional=1000,
        mt5_bid=100,
        mt5_ask=100.1,
        max_slippage_bps=0,
        quantity=0,
        hyperliquid_bid=0,
        hyperliquid_ask=0,
        hyperliquid_fee_rate=0,
        hyperliquid_funding_rate=0.001,
        hyperliquid_side="sell",
        mt5_commission_rate=0,
        mt5_swap_cost=0,
        holding_hours=1,
    )
    assert cost.hyperliquid_funding == -1


def test_hyperliquid_roundtrip_fee_and_spread() -> None:
    cost = estimate_cost(
        notional=1000,
        mt5_bid=100,
        mt5_ask=100,
        max_slippage_bps=0,
        quantity=0.01,
        hyperliquid_bid=64272,
        hyperliquid_ask=64273,
        hyperliquid_fee_rate=0.00045,
        hyperliquid_fee_round_trips=2,
        hyperliquid_funding_rate=0,
        mt5_commission_rate=0,
        mt5_swap_cost=0,
    )
    assert cost.hyperliquid_fee == 0.9
    assert cost.hyperliquid_spread == 0.01


def test_hyperliquid_maker_open_taker_close_fee() -> None:
    cost = estimate_cost(
        notional=1000,
        mt5_bid=100,
        mt5_ask=100,
        max_slippage_bps=0,
        hyperliquid_fee_rate=0.00015,
        hyperliquid_close_fee_rate=0.00045,
        hyperliquid_funding_rate=0,
        mt5_commission_rate=0,
        mt5_swap_cost=0,
    )
    assert cost.hyperliquid_fee == 0.6


def test_execution_gateway_maps_adapter_fill_event() -> None:
    adapter = PaperAdapter("hyperliquid")
    gateway = AdapterExecutionGateway(adapter)
    quote_cache.put("hyperliquid", "BTC", 100, 101, 10000, "test")
    result = gateway.submit_order(LegOrderIntent(platform="hyperliquid", symbol="BTC", side="buy", quantity=2))
    assert result.success
    assert result.order_event.status == "filled"
    assert result.order_event.filled_quantity == 2
    assert len(result.fill_events) == 1
    assert result.fill_events[0].price == 101


def test_hyperliquid_paper_market_order_uses_l2_average_price() -> None:
    adapter = PaperAdapter("hyperliquid")
    gateway = AdapterExecutionGateway(adapter)
    quote_cache.put("hyperliquid", "L2-PAPER", 100, 101, 10000, "test")
    order_book_cache.put(
        "hyperliquid",
        "L2-PAPER",
        bids=[(100.0, 10.0)],
        asks=[(101.0, 1.0), (103.0, 1.0)],
        source="test",
    )

    result = gateway.submit_order(LegOrderIntent(platform="hyperliquid", symbol="L2-PAPER", side="buy", quantity=2))

    assert result.success
    assert result.order_event.average_price == 102.0
    assert result.fill_events[0].price == 102.0


def test_hyperliquid_paper_fee_uses_venue_symbol_effective_taker_rate() -> None:
    provider_calls = []

    def fee_provider(symbol: str):
        provider_calls.append(symbol)
        return SimpleNamespace(taker_fee_rate=0.00009, maker_fee_rate=0.00003)

    adapter = PaperAdapter("hyperliquid", fee_rate_provider=fee_provider)
    gateway = AdapterExecutionGateway(adapter)
    quote_cache.put("hyperliquid", "JPY-FEE", 100, 101, 10000, "test")

    result = gateway.submit_order(LegOrderIntent(platform="hyperliquid", symbol="JPY-FEE", venue_symbol="xyz:JPY", side="buy", quantity=10))

    assert result.success
    assert provider_calls == ["xyz:JPY"]
    assert result.order_event.fee == pytest.approx(10 * 101 * 0.00009)


def test_mt5_paper_fee_is_zero() -> None:
    adapter = PaperAdapter("mt5")
    gateway = AdapterExecutionGateway(adapter)
    quote_cache.put("mt5", "JPY-MT5-FEE", 100, 101, 10000, "test")

    result = gateway.submit_order(LegOrderIntent(platform="mt5", symbol="JPY-MT5-FEE", side="buy", quantity=10))

    assert result.success
    assert result.order_event.fee == 0.0


def test_execution_gateway_preserves_adapter_rejection() -> None:
    adapter = PaperAdapter("hyperliquid")
    gateway = AdapterExecutionGateway(adapter)
    quote_cache.put("hyperliquid", "BTC", 100, 101, 10000, "test")
    result = gateway.submit_order(
        LegOrderIntent(platform="hyperliquid", symbol="BTC", side="buy", quantity=2, order_type="limit", price=102, post_only=True)
    )
    assert not result.success
    assert result.order_event.status == "rejected"
    assert "post-only" in result.order_event.message
    assert result.fill_events == ()


def test_mt5_live_order_requires_explicit_switch() -> None:
    adapter = MT5Adapter(live=True)
    adapter.settings = SimpleNamespace(mt5_live_order_enabled=False)
    result = adapter.place_order(AdapterOrder(platform="mt5", symbol="OIL", side="buy", quantity=0.01, venue_symbol="USOIL"))
    assert not result.success
    assert "开关未开启" in result.error_message


def test_mt5_demo_requires_explicit_switch_before_import() -> None:
    adapter = MT5Adapter(demo=True)
    adapter.settings = SimpleNamespace(mt5_demo_order_enabled=False)
    result = adapter.place_order(AdapterOrder(platform="mt5", symbol="OIL", side="buy", quantity=0.01, venue_symbol="USOIL"))
    assert not result.success
    assert "demo 下单开关未开启" in result.error_message


def test_mt5_demo_order_check_requires_demo_account_and_configured_identity() -> None:
    class FakeMT5:
        ACCOUNT_TRADE_MODE_DEMO = 0

        def __init__(self, trade_mode=0, login=123, server="broker-demo") -> None:
            self.info = SimpleNamespace(trade_mode=trade_mode, login=login, server=server)

        def account_info(self):
            return self.info

        def last_error(self):
            return (0, "")

    settings = SimpleNamespace(mt5_demo_order_enabled=True, mt5_login="123", mt5_server="broker-demo")
    assert mt5_demo_order_check(FakeMT5(), settings).allowed
    assert not mt5_demo_order_check(FakeMT5(trade_mode=2), settings).allowed
    assert "不是 DEMO" in mt5_demo_order_check(FakeMT5(trade_mode=2), settings).message
    assert not mt5_demo_order_check(FakeMT5(login=999), settings).allowed
    assert not mt5_demo_order_check(FakeMT5(server="broker-real"), settings).allowed


def test_mt5_live_market_order_maps_order_send(monkeypatch) -> None:
    sent_requests = []

    class FakeMT5:
        ORDER_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1
        TRADE_ACTION_DEAL = 1
        ORDER_TIME_GTC = 0
        ORDER_FILLING_IOC = 1
        ORDER_FILLING_RETURN = 2
        ORDER_FILLING_FOK = 4
        TRADE_RETCODE_DONE = 10009
        TRADE_RETCODE_DONE_PARTIAL = 10010
        TRADE_RETCODE_PLACED = 10008

        def initialize(self, **kwargs):
            return True

        def last_error(self):
            return (0, "")

        def symbol_select(self, symbol, enabled):
            return symbol == "USOIL" and enabled

        def symbol_info_tick(self, symbol):
            return SimpleNamespace(bid=76.44, ask=76.46)

        def symbol_info(self, symbol):
            return SimpleNamespace(filling_mode=0)

        def order_send(self, request):
            sent_requests.append(request)
            return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=12345, deal=67890, volume=request["volume"], price=request["price"], comment="done")

    monkeypatch.setitem(sys.modules, "MetaTrader5", FakeMT5())
    adapter = MT5Adapter(live=True)
    adapter.settings = SimpleNamespace(
        mt5_live_order_enabled=True,
        mt5_login="",
        mt5_password="",
        mt5_server="",
        mt5_order_deviation_points=20,
        mt5_order_magic=260620,
    )
    result = adapter.place_order(AdapterOrder(platform="mt5", symbol="OIL", side="buy", quantity=0.01, venue_symbol="USOIL"))
    assert result.success
    assert result.external_order_id == "12345"
    assert result.filled_quantity == 0.01
    assert result.average_price == 76.46
    assert sent_requests[0]["symbol"] == "USOIL"


def test_mt5_live_reduce_only_uses_position_ticket(monkeypatch) -> None:
    sent_requests = []

    class FakeMT5:
        ORDER_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1
        POSITION_TYPE_BUY = 0
        POSITION_TYPE_SELL = 1
        TRADE_ACTION_DEAL = 1
        ORDER_TIME_GTC = 0
        ORDER_FILLING_IOC = 1
        ORDER_FILLING_RETURN = 2
        ORDER_FILLING_FOK = 4
        TRADE_RETCODE_DONE = 10009
        TRADE_RETCODE_DONE_PARTIAL = 10010
        TRADE_RETCODE_PLACED = 10008

        def initialize(self, **kwargs):
            return True

        def last_error(self):
            return (0, "")

        def symbol_select(self, symbol, enabled):
            return True

        def symbol_info_tick(self, symbol):
            return SimpleNamespace(bid=76.44, ask=76.46)

        def symbol_info(self, symbol):
            return SimpleNamespace(filling_mode=0)

        def positions_get(self, symbol=None):
            return [SimpleNamespace(ticket=555, symbol="USOIL", type=self.POSITION_TYPE_SELL, volume=0.02)]

        def order_send(self, request):
            sent_requests.append(request)
            return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=12345, volume=request["volume"], price=request["price"], comment="done")

    monkeypatch.setitem(sys.modules, "MetaTrader5", FakeMT5())
    adapter = MT5Adapter(live=True)
    adapter.settings = SimpleNamespace(
        mt5_live_order_enabled=True,
        mt5_login="",
        mt5_password="",
        mt5_server="",
        mt5_order_deviation_points=20,
        mt5_order_magic=260620,
    )

    result = adapter.place_order(AdapterOrder(platform="mt5", symbol="OIL", side="buy", quantity=0.02, venue_symbol="USOIL", reduce_only=True))

    assert result.success
    assert sent_requests[0]["position"] == 555
    assert sent_requests[0]["type"] == FakeMT5.ORDER_TYPE_BUY


def test_mt5_live_reduce_only_rejects_without_matching_position(monkeypatch) -> None:
    class FakeMT5:
        ORDER_TYPE_BUY = 0
        POSITION_TYPE_SELL = 1
        TRADE_ACTION_DEAL = 1
        ORDER_TIME_GTC = 0
        ORDER_FILLING_IOC = 1

        def initialize(self, **kwargs):
            return True

        def last_error(self):
            return (0, "")

        def symbol_select(self, symbol, enabled):
            return True

        def symbol_info_tick(self, symbol):
            return SimpleNamespace(bid=76.44, ask=76.46)

        def symbol_info(self, symbol):
            return SimpleNamespace(filling_mode=0)

        def positions_get(self, symbol=None):
            return []

        def order_send(self, request):
            raise AssertionError("reduce-only 没有持仓时不应发单")

    monkeypatch.setitem(sys.modules, "MetaTrader5", FakeMT5())
    adapter = MT5Adapter(live=True)
    adapter.settings = SimpleNamespace(
        mt5_live_order_enabled=True,
        mt5_login="",
        mt5_password="",
        mt5_server="",
        mt5_order_deviation_points=20,
        mt5_order_magic=260620,
    )

    result = adapter.place_order(AdapterOrder(platform="mt5", symbol="OIL", side="buy", quantity=0.02, venue_symbol="USOIL", reduce_only=True))

    assert not result.success
    assert "reduce-only" in result.error_message


def test_mt5_live_reduce_only_rejects_oversized_close(monkeypatch) -> None:
    class FakeMT5:
        ORDER_TYPE_BUY = 0
        POSITION_TYPE_SELL = 1
        TRADE_ACTION_DEAL = 1
        ORDER_TIME_GTC = 0
        ORDER_FILLING_IOC = 1

        def initialize(self, **kwargs):
            return True

        def last_error(self):
            return (0, "")

        def symbol_select(self, symbol, enabled):
            return True

        def symbol_info_tick(self, symbol):
            return SimpleNamespace(bid=76.44, ask=76.46)

        def symbol_info(self, symbol):
            return SimpleNamespace(filling_mode=0)

        def positions_get(self, symbol=None):
            return [SimpleNamespace(ticket=555, symbol="USOIL", type=self.POSITION_TYPE_SELL, volume=0.02)]

        def order_send(self, request):
            raise AssertionError("reduce-only 超过持仓数量时不应发单")

    monkeypatch.setitem(sys.modules, "MetaTrader5", FakeMT5())
    adapter = MT5Adapter(live=True)
    adapter.settings = SimpleNamespace(
        mt5_live_order_enabled=True,
        mt5_login="",
        mt5_password="",
        mt5_server="",
        mt5_order_deviation_points=20,
        mt5_order_magic=260620,
    )

    result = adapter.place_order(AdapterOrder(platform="mt5", symbol="OIL", side="buy", quantity=0.05, venue_symbol="USOIL", reduce_only=True))

    assert not result.success
    assert "超过持仓" in result.error_message


def test_hyperliquid_live_positions_read_clearinghouse_state(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "assetPositions": [
                        {
                            "position": {
                                "coin": "OIL",
                                "szi": "2.5",
                                "entryPx": "76.1",
                                "markPx": "76.4",
                                "unrealizedPnl": "0.75",
                                "marginUsed": "12.3",
                                "liquidationPx": "40",
                            }
                        },
                        {"position": {"coin": "BTC", "szi": "0"}},
                    ]
                }
            ).encode("utf-8")

    calls = []

    def fake_urlopen(req, timeout):
        calls.append(json.loads(req.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr("app.adapters.hyperliquid.request.urlopen", fake_urlopen)
    adapter = HyperliquidAdapter(live=True)
    adapter.settings = SimpleNamespace(
        hyperliquid_account_address="0xabc",
        hyperliquid_info_url="https://example.test/info",
    )

    positions = adapter.get_positions()

    assert calls == [
        {"type": "allMids"},
        {"type": "clearinghouseState", "user": "0xabc"},
    ]
    assert positions == [
        {
            "platform": "hyperliquid",
            "symbol": "OIL",
            "side": "long",
            "quantity": 2.5,
            "entry_price": 76.1,
            "mark_price": 76.4,
            "unrealized_pnl": 0.75,
            "margin_used": 12.3,
            "liquidation_price": 40.0,
        }
    ]


def test_hyperliquid_live_positions_read_hip3_dex_positions(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            if self.payload.get("type") == "allMids":
                return json.dumps({"xyz:JP225": "72015"}).encode("utf-8")
            dex = self.payload.get("dex")
            positions = []
            if dex == "xyz":
                positions = [
                    {
                        "position": {
                            "coin": "xyz:JP225",
                            "szi": "0.0002",
                            "entryPx": "71875",
                            "unrealizedPnl": "0.03",
                            "marginUsed": "0.75",
                            "liquidationPx": "70034",
                        }
                    }
                ]
            return json.dumps({"assetPositions": positions}).encode("utf-8")

    calls = []

    def fake_urlopen(req, timeout):
        payload = json.loads(req.data.decode("utf-8"))
        calls.append(payload)
        return FakeResponse(payload)

    monkeypatch.setattr("app.adapters.hyperliquid.request.urlopen", fake_urlopen)
    adapter = HyperliquidAdapter(live=True)
    adapter.settings = SimpleNamespace(
        hyperliquid_account_address="0xabc",
        hyperliquid_info_url="https://example.test/info",
    )

    positions = adapter.get_positions(dexes=["xyz"])

    assert calls == [
        {"type": "allMids"},
        {"type": "allMids", "dex": "xyz"},
        {"type": "clearinghouseState", "user": "0xabc"},
        {"type": "clearinghouseState", "user": "0xabc", "dex": "xyz"},
    ]
    assert positions == [
        {
            "platform": "hyperliquid",
            "symbol": "xyz:JP225",
            "side": "long",
            "quantity": 0.0002,
            "entry_price": 71875.0,
            "mark_price": 72015.0,
            "unrealized_pnl": 0.03,
            "margin_used": 0.75,
            "liquidation_price": 70034.0,
        }
    ]


def test_hyperliquid_execution_info_url_uses_configured_url() -> None:
    settings = SimpleNamespace(hyperliquid_info_url=HYPERLIQUID_MAINNET_INFO_URL)

    assert hyperliquid_execution_info_url(settings) == HYPERLIQUID_MAINNET_INFO_URL


def test_hyperliquid_live_positions_use_execution_info_url(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"assetPositions": []}).encode("utf-8")

    urls = []

    def fake_urlopen(req, timeout):
        urls.append(req.full_url)
        return FakeResponse()

    monkeypatch.setattr("app.adapters.hyperliquid.request.urlopen", fake_urlopen)
    adapter = HyperliquidAdapter(live=True)
    adapter.settings = SimpleNamespace(
        hyperliquid_account_address="0xabc",
        hyperliquid_info_url=HYPERLIQUID_MAINNET_INFO_URL,
    )

    assert adapter.get_positions() == []
    assert urls == [HYPERLIQUID_MAINNET_INFO_URL, HYPERLIQUID_MAINNET_INFO_URL]


def test_hyperliquid_paper_live_probe_uses_minimum_real_order_and_paper_quantity(monkeypatch) -> None:
    submitted = []

    class FakeExchange:
        def market_open(self, name, is_buy, sz, px, slippage):
            submitted.append((name, is_buy, sz, px, slippage))
            return {
                "status": "ok",
                "response": {
                    "data": {
                        "statuses": [
                            {"filled": {"totalSz": str(sz), "avgPx": "100.5", "oid": 12345}},
                        ]
                    }
                },
            }

    adapter = HyperliquidAdapter(live=True)
    adapter.paper_price_probe = True
    adapter.settings = SimpleNamespace(
        hyperliquid_paper_live_order_enabled=True,
        hyperliquid_account_address="0xabc",
        hyperliquid_secret_key="0xkey",
        hyperliquid_default_min_notional=10.0,
        hyperliquid_paper_live_slippage=0.01,
        hyperliquid_info_url="https://example.test/info",
    )
    adapter._post_info = lambda payload: (
        {"universe": [{"name": "BTC", "szDecimals": 5}]} if payload["type"] == "meta" else {"BTC": "65000"}
    )
    adapter._fee_rate = lambda order: 0.001
    monkeypatch.setattr("app.adapters.hyperliquid._load_hyperliquid_exchange", lambda settings: FakeExchange())

    result = adapter.place_order(AdapterOrder(platform="hyperliquid", symbol="BTC", side="buy", quantity=0.25, venue_symbol="BTC"))

    assert submitted == [("BTC", True, 0.00016, None, 0.01)]
    assert result.success
    assert result.external_order_id == "12345"
    assert result.filled_quantity == 0.25
    assert result.average_price == 100.5
    assert result.fee == pytest.approx(0.25 * 100.5 * 0.001)
    assert "探针真实成交量" in result.error_message


def test_execution_adapters_enable_hyperliquid_probe_only_for_paper_switch(monkeypatch) -> None:
    monkeypatch.setattr("app.execution.engine.get_settings", lambda: SimpleNamespace(hyperliquid_paper_live_order_enabled=True))

    hl, mt5 = _execution_adapters(live=False, simulated=True)

    assert hl.live is True
    assert hl.paper_price_probe is True
    assert getattr(hl, "simulated") is True
    assert mt5.demo is True


def test_symbol_mapping_rejects_mt5_style_hyperliquid_symbol() -> None:
    with pytest.raises(ValueError, match="Hyperliquid 标准永续"):
        SymbolMappingIn(symbol="BTC", hyperliquid_symbol="BTCUSD", mt5_symbol="BTCUSD")


def test_accepted_order_without_fill_is_pending_not_position_effect() -> None:
    accepted = AdapterOrderResult(True, "nt-live-1", "accepted", 0.0, 0.0, 0.0)
    filled = AdapterOrderResult(True, "nt-live-2", "filled", 0.01, 65000.0, 0.1)
    assert _is_pending_result(accepted)
    assert not _has_position_effect(accepted)
    assert _has_position_effect(filled)


def test_live_close_hedge_group_places_reverse_orders(monkeypatch) -> None:
    db, group_id = _live_close_test_db()
    submitted = []

    class FakeGateway:
        def __init__(self, platform: str) -> None:
            self.platform = platform

        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, f"{intent.platform}-close", "filled", intent.quantity, 100.0, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.0, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.0, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway(adapter.platform))
    monkeypatch.setattr("app.execution.engine.live_execution_readiness", lambda db: {"checks": []})
    group = close_hedge_group(db, group_id, "manual close")

    assert group.status == "closed"
    assert group.close_reason == "manual close"
    orders = db.query(Order).filter(Order.hedge_group_id == group_id).order_by(Order.platform).all()
    assert [(order.platform, order.side, order.status) for order in orders] == [("hyperliquid", "sell", "filled"), ("mt5", "buy", "filled")]
    assert [order.reduce_only for order in orders] == [True, True]
    assert [intent.reduce_only for intent in submitted] == [True, True]
    assert db.query(Fill).count() == 2


def test_live_close_hedge_group_keeps_pending_orders_closing(monkeypatch) -> None:
    db, group_id = _live_close_test_db()
    submitted = []

    class FakeGateway:
        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, f"{intent.platform}-accepted", "accepted", 0.0, 0.0, 0.0)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "accepted", result.external_order_id, intent.quantity, 0.0, 0.0, 0.0)
            return GatewayOrderResult(True, event, (), result)

    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway())
    monkeypatch.setattr("app.execution.engine.live_execution_readiness", lambda db: {"checks": []})
    group = close_hedge_group(db, group_id, "manual close")

    assert group.status == "closing"
    assert "待成交" in group.close_reason
    assert db.query(Order).filter(Order.hedge_group_id == group_id, Order.status == "accepted").count() == 1
    assert [intent.platform for intent in submitted] == ["hyperliquid"]
    assert db.query(Fill).count() == 0


def test_live_close_blocks_when_readiness_has_blockers(monkeypatch) -> None:
    db, group_id = _live_close_test_db()
    monkeypatch.setattr(
        "app.execution.engine.live_execution_readiness",
        lambda db: {"checks": [{"component": "mt5_live_order_enabled", "status": "block", "message": "MT5_LIVE_ORDER_ENABLED 未开启"}]},
    )

    with pytest.raises(ValueError, match="实盘执行就绪检查未通过"):
        close_hedge_group(db, group_id, "manual close")


def test_live_open_blocks_when_readiness_has_blockers(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="live"))
    db.add(SystemSetting(key="live_trading_enabled", value="true"))
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL"))
    opportunity = ArbitrageOpportunity(
        symbol="OIL",
        direction="long_hyperliquid_short_mt5",
        status="executable",
        notional=1000,
        quantity=1.0,
        hyperliquid_quantity=1.0,
        mt5_quantity=0.1,
        gross_spread=10,
        trigger_hyperliquid_bid=99.0,
        trigger_hyperliquid_ask=101.0,
        trigger_mt5_bid=110.0,
        trigger_mt5_ask=111.0,
        unit_cost=1,
        unit_net_profit=9,
        entry_threshold=8,
        exit_target=2,
        total_cost=1,
        net_profit=9,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()
    monkeypatch.setattr(
        "app.execution.engine.live_execution_readiness",
        lambda db: {"checks": [{"component": "hyperliquid_live_order_submit", "status": "block", "message": "Hyperliquid 实盘下单未启用"}]},
    )

    with pytest.raises(ValueError, match="实盘执行就绪检查未通过"):
        open_hedge_group(db, opportunity.id)


def test_live_open_orders_are_not_reduce_only(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="live"))
    db.add(SystemSetting(key="live_trading_enabled", value="true"))
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL"))
    opportunity = ArbitrageOpportunity(
        symbol="OIL",
        direction="long_hyperliquid_short_mt5",
        status="executable",
        notional=1000,
        quantity=1.0,
        hyperliquid_quantity=1.0,
        mt5_quantity=0.1,
        gross_spread=10,
        trigger_hyperliquid_bid=99.0,
        trigger_hyperliquid_ask=101.0,
        trigger_mt5_bid=110.0,
        trigger_mt5_ask=111.0,
        unit_cost=1,
        unit_net_profit=9,
        entry_threshold=8,
        exit_target=2,
        total_cost=1,
        net_profit=9,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()
    quote_cache.put("hyperliquid", "OIL", 100, 101, 10000, "test")
    quote_cache.put("mt5", "OIL", 100, 101, 10000, "test")
    submitted = []

    class FakeGateway:
        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, f"{intent.platform}-open", "filled", intent.quantity, 100.0, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.0, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.0, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway())
    monkeypatch.setattr("app.execution.engine.live_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.mt5_market_order_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, message="ok"))
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: (SimpleNamespace(hyperliquid=SimpleNamespace(local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)), time_diff_ms=0), ""))
    monkeypatch.setattr("app.execution.engine.pre_trade_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, reason=""))

    group = open_hedge_group(db, opportunity.id)

    assert group.status == "open"
    assert group.trigger_hyperliquid_bid == 99.0
    assert group.trigger_hyperliquid_ask == 101.0
    assert group.trigger_mt5_bid == 110.0
    assert group.trigger_mt5_ask == 111.0
    assert [intent.reduce_only for intent in submitted] == [False, False]
    assert {order.reduce_only for order in db.query(Order).filter(Order.hedge_group_id == group.id).all()} == {False}


def test_hyper_maker_price_is_normalized_to_tick_and_precision() -> None:
    mapping = SymbolMapping(symbol="EUR", hyperliquid_symbol="xyz:EUR", mt5_symbol="EURUSD", price_precision=5, min_tick=0.00001)

    sell_price = _maker_price("sell", bid=1.1459, ask=1.1459, offset_bps=1.0, mapping=mapping)
    buy_price = _maker_price("buy", bid=1.1459, ask=1.1460, offset_bps=1.0, mapping=mapping)

    assert sell_price == 1.14602
    assert buy_price == 1.14578
    assert len(str(sell_price).split(".")[1]) <= 5


def test_paper_open_uses_hyperliquid_sim_and_mt5_demo_adapters(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper"))
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL"))
    opportunity = ArbitrageOpportunity(
        symbol="OIL",
        direction="long_hyperliquid_short_mt5",
        status="executable",
        notional=1000,
        quantity=1.0,
        hyperliquid_quantity=1.0,
        mt5_quantity=0.1,
        gross_spread=10,
        unit_cost=1,
        unit_net_profit=9,
        entry_threshold=8,
        exit_target=2,
        total_cost=1,
        net_profit=9,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()
    seen = []

    class FakeGateway:
        def __init__(self, adapter) -> None:
            seen.append((adapter.platform, getattr(adapter, "simulated", False), getattr(adapter, "demo", False), getattr(adapter, "live", False)))

        def submit_order(self, intent, *, paper_latency_ms=0):
            result = AdapterOrderResult(True, f"{intent.platform}-paper", "filled", intent.quantity, 100.0, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.0, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.0, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.mt5_market_order_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, message="ok"))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway(adapter))
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: (SimpleNamespace(hyperliquid=SimpleNamespace(local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)), time_diff_ms=0), ""))
    monkeypatch.setattr("app.execution.engine.pre_trade_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, reason=""))
    monkeypatch.setattr("app.execution.engine.get_settings", lambda: SimpleNamespace(hyperliquid_paper_live_order_enabled=False, paper_live_parallel_execution=False, strict_quote_sync_ms=500, quote_stale_ms=1500, default_slippage_bps=0))
    monkeypatch.setattr("app.execution.engine.get_settings", lambda: SimpleNamespace(hyperliquid_paper_live_order_enabled=False, strict_quote_sync_ms=500, quote_stale_ms=1500, default_slippage_bps=0))

    group = open_hedge_group(db, opportunity.id)

    assert group.status == "open"
    assert ("hyperliquid", True, False, False) in seen
    assert ("mt5", False, True, False) in seen


def test_open_blocks_when_mt5_session_disallows_open_before_hyperliquid_leg(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper"))
    db.add(SymbolMapping(symbol="SPCX", hyperliquid_symbol="xyz:SPCX", mt5_symbol="SPCXz"))
    opportunity = ArbitrageOpportunity(
        symbol="SPCX",
        direction="long_mt5_short_hyperliquid",
        status="executable",
        notional=500,
        quantity=34.0,
        hyperliquid_quantity=34.0,
        mt5_quantity=0.34,
        gross_spread=0.2,
        unit_cost=0.01,
        unit_net_profit=0.19,
        entry_threshold=0.1,
        exit_target=0.0,
        total_cost=0.34,
        net_profit=6.46,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()
    gateway_calls = []
    sync_calls = []

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr(
        "app.execution.engine.mt5_session_state",
        lambda mapping: MT5SessionState(mapping.symbol, "reduce_only", "MT5 当前只允许平仓", True, False, False, True, True),
    )
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: gateway_calls.append(adapter.platform))
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: sync_calls.append(args) or (None, "should not sync"))

    with pytest.raises(ValueError, match="MT5 当前不允许该方向新开仓"):
        open_hedge_group(db, opportunity.id)

    db.refresh(opportunity)
    assert "MT5 当前不允许该方向新开仓" in opportunity.reject_reason
    assert db.query(HedgeGroup).count() == 0
    assert db.query(Order).count() == 0
    assert db.query(Fill).count() == 0
    assert gateway_calls == []
    assert sync_calls == []


def test_open_blocks_when_mt5_order_check_rejects_before_hyperliquid_leg(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper"))
    db.add(SymbolMapping(symbol="SPCX", hyperliquid_symbol="xyz:SPCX", mt5_symbol="SPCXz"))
    opportunity = ArbitrageOpportunity(
        symbol="SPCX",
        direction="long_mt5_short_hyperliquid",
        status="executable",
        notional=500,
        quantity=34.0,
        hyperliquid_quantity=34.0,
        mt5_quantity=0.34,
        gross_spread=0.2,
        unit_cost=0.01,
        unit_net_profit=0.19,
        entry_threshold=0.1,
        exit_target=0.0,
        total_cost=0.34,
        net_profit=6.46,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()
    gateway_calls = []
    checks = []

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr(
        "app.execution.engine.mt5_market_order_check",
        lambda symbol, side, quantity, **kwargs: checks.append((symbol, side, quantity, kwargs)) or SimpleNamespace(allowed=False, message="retcode=10044: Only position closing is allowed"),
    )
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: gateway_calls.append(adapter.platform))

    with pytest.raises(ValueError, match="MT5 当前订单预检查失败"):
        open_hedge_group(db, opportunity.id)

    db.refresh(opportunity)
    assert checks == [("SPCXz", "buy", 0.34, {"demo": True})]
    assert "retcode=10044" in opportunity.reject_reason
    assert db.query(HedgeGroup).count() == 0
    assert db.query(Order).count() == 0
    assert gateway_calls == []


def test_auto_execute_waits_for_mt5_tradability_cache(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper", auto_execute_enabled=True, auto_execute_min_hold_ms=0, auto_execute_confirm_ticks=1))
    db.add(
        ArbitrageOpportunity(
            symbol="SPCX",
            direction="long_mt5_short_hyperliquid",
            status="executable",
            notional=500,
            quantity=34.0,
            hyperliquid_quantity=34.0,
            mt5_quantity=0.34,
            gross_spread=0.2,
            unit_cost=0.01,
            unit_net_profit=0.19,
            entry_threshold=0.1,
            exit_target=0.0,
            total_cost=0.34,
            net_profit=6.46,
            annualized_return=0.1,
        )
    )
    db.commit()
    calls = []

    monkeypatch.setattr("app.execution.auto_executor.mt5_tradability_cache.initialized", lambda: False)
    monkeypatch.setattr("app.execution.auto_executor.open_hedge_group", lambda *args, **kwargs: calls.append(args))

    assert run_auto_execute(db) == 0
    assert calls == []
    assert "缓存尚未初始化" in db.query(SystemLog).order_by(SystemLog.id.desc()).first().message


def test_open_quarantines_mt5_side_after_order_send_10044(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper"))
    db.add(SymbolMapping(symbol="SPCX", hyperliquid_symbol="xyz:SPCX", mt5_symbol="SPCXz"))
    opportunity = ArbitrageOpportunity(
        symbol="SPCX",
        direction="long_mt5_short_hyperliquid",
        status="executable",
        notional=500,
        quantity=34.0,
        hyperliquid_quantity=34.0,
        mt5_quantity=0.34,
        gross_spread=0.2,
        unit_cost=0.01,
        unit_net_profit=0.19,
        entry_threshold=0.1,
        exit_target=0.0,
        total_cost=0.34,
        net_profit=6.46,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()

    class FakeGateway:
        def __init__(self, adapter) -> None:
            self.platform = adapter.platform

        def submit_order(self, intent, *, paper_latency_ms=0):
            if intent.platform == "hyperliquid":
                result = AdapterOrderResult(True, "hl-1", "filled", intent.quantity, 151.0, 0.1)
                event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 151.0, 0.1)
                fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 151.0, 0.1, result.external_order_id)
                return GatewayOrderResult(True, event, (fill,), result)
            result = AdapterOrderResult(False, "mt5-1", "rejected", 0.0, 0.0, 0.0, "MT5 order_send 失败 retcode=10044: mt5-hedge")
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "rejected", result.external_order_id, intent.quantity, 0.0, 0.0, 0.0, result.error_message)
            return GatewayOrderResult(False, event, (), result)

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.mt5_market_order_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, message="Done"))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway(adapter))
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: (SimpleNamespace(hyperliquid=SimpleNamespace(local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)), time_diff_ms=0), ""))
    monkeypatch.setattr("app.execution.engine.pre_trade_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, reason=""))
    monkeypatch.setattr("app.execution.engine.get_settings", lambda: SimpleNamespace(hyperliquid_paper_live_order_enabled=False, paper_live_parallel_execution=False, strict_quote_sync_ms=500, quote_stale_ms=1500, default_slippage_bps=0))

    group = open_hedge_group(db, opportunity.id)

    assert group.status == "manual_intervention"
    block = db.get(SystemSetting, "mt5_tradability_block:SPCX:buy")
    assert block is not None
    assert "retcode=10044" in block.value
    assert db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "mt5", Order.status == "rejected").count() == 1


def test_paper_open_records_actual_entry_spread_from_fills(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper"))
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL"))
    opportunity = ArbitrageOpportunity(
        symbol="OIL",
        direction="long_hyperliquid_short_mt5",
        status="executable",
        notional=1000,
        quantity=1.0,
        hyperliquid_quantity=1.0,
        mt5_quantity=0.1,
        gross_spread=10,
        unit_cost=1,
        unit_net_profit=9,
        entry_threshold=8,
        exit_target=2,
        total_cost=1,
        net_profit=9,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()

    class FakeGateway:
        def __init__(self, adapter) -> None:
            self.platform = adapter.platform

        def submit_order(self, intent, *, paper_latency_ms=0):
            price = 101.0 if intent.platform == "hyperliquid" else 103.0
            result = AdapterOrderResult(True, f"{intent.platform}-paper", "filled", intent.quantity, price, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, price, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, price, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.refresh_execution_quotes", lambda *args, **kwargs: ["hyperliquid"])
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.mt5_market_order_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, message="ok"))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway(adapter))
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: (SimpleNamespace(hyperliquid=SimpleNamespace(local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)), time_diff_ms=0), ""))
    monkeypatch.setattr("app.execution.engine.pre_trade_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, reason=""))

    group = open_hedge_group(db, opportunity.id)

    assert group.status == "open"
    assert group.trigger_spread == 10
    assert group.entry_spread == 2.0
    assert group.fees == 0.2


def test_paper_open_waits_for_hyperliquid_fill_before_mt5(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper"))
    db.add(SymbolMapping(symbol="JP225", hyperliquid_symbol="xyz:JP225", mt5_symbol="JP225z"))
    opportunity = ArbitrageOpportunity(
        symbol="JP225",
        direction="long_hyperliquid_short_mt5",
        status="executable",
        notional=450,
        quantity=1.0,
        hyperliquid_quantity=0.00625,
        mt5_quantity=1.0,
        gross_spread=10,
        unit_cost=1,
        unit_net_profit=9,
        entry_threshold=8,
        exit_target=2,
        total_cost=1,
        net_profit=9,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()
    submitted = []

    class FakeGateway:
        def __init__(self, adapter) -> None:
            self.platform = adapter.platform

        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, f"{intent.platform}-pending", "accepted", 0.0, 0.0, 0.0, "timeout")
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "accepted", result.external_order_id, intent.quantity, 0.0, 0.0, 0.0)
            return GatewayOrderResult(True, event, (), result)

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.mt5_market_order_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, message="ok"))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway(adapter))
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: (SimpleNamespace(hyperliquid=SimpleNamespace(local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)), time_diff_ms=0), ""))
    monkeypatch.setattr("app.execution.engine.pre_trade_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, reason=""))
    monkeypatch.setattr("app.execution.engine.get_settings", lambda: SimpleNamespace(hyperliquid_paper_live_order_enabled=False, paper_live_parallel_execution=False, strict_quote_sync_ms=500, quote_stale_ms=1500, default_slippage_bps=0))

    group = open_hedge_group(db, opportunity.id)

    assert group.status == "opening"
    assert opportunity.status == "executing"
    assert [intent.platform for intent in submitted] == ["hyperliquid"]
    orders = db.query(Order).filter(Order.hedge_group_id == group.id).all()
    assert [(order.platform, order.status) for order in orders] == [("hyperliquid", "accepted")]
    assert db.query(Fill).count() == 0


def test_open_refreshes_execution_quotes_after_strict_sync_failure(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper", paper_hyperliquid_latency_ms_min=0, paper_hyperliquid_latency_ms_max=0, paper_mt5_latency_ms_min=0, paper_mt5_latency_ms_max=0))
    db.add(SymbolMapping(symbol="JP225", hyperliquid_symbol="xyz:JP225", mt5_symbol="JP225z"))
    opportunity = ArbitrageOpportunity(
        symbol="JP225",
        direction="long_hyperliquid_short_mt5",
        status="executable",
        notional=450,
        quantity=1.0,
        hyperliquid_quantity=1.0,
        mt5_quantity=1.0,
        gross_spread=20,
        unit_cost=1,
        unit_net_profit=19,
        entry_threshold=10,
        exit_target=2,
        total_cost=1,
        net_profit=19,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()
    synced = SimpleNamespace(
        hyperliquid=SimpleNamespace(ask=100.0, bid=99.0, local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)),
        mt5=SimpleNamespace(ask=121.0, bid=120.0),
        time_diff_ms=300,
    )
    sync_results = [(None, "行情过期，最大延迟 3000ms"), (synced, "")]
    refreshed = []

    class FakeGateway:
        def submit_order(self, intent, *, paper_latency_ms=0):
            result = AdapterOrderResult(True, f"{intent.platform}-open", "filled", intent.quantity, 100.0, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.0, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.0, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.refresh_execution_quotes", lambda mapping, **kwargs: refreshed.append((mapping.symbol, kwargs.get("refresh_mt5"))) or ["hyperliquid", "mt5"])
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.mt5_market_order_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, message="ok"))
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: sync_results.pop(0))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway())
    monkeypatch.setattr("app.execution.engine.get_settings", lambda: SimpleNamespace(hyperliquid_paper_live_order_enabled=False, paper_live_parallel_execution=False, strict_quote_sync_ms=500, quote_stale_ms=1500, default_slippage_bps=0))

    group = open_hedge_group(db, opportunity.id)

    assert refreshed == [("JP225", None), ("JP225", False)]
    assert group.status == "open"
    assert db.query(Order).filter(Order.hedge_group_id == group.id).count() == 2


def test_paper_live_parallel_submits_hyperliquid_and_mt5_without_waiting(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper", paper_hyperliquid_latency_ms_min=0, paper_hyperliquid_latency_ms_max=0, paper_mt5_latency_ms_min=0, paper_mt5_latency_ms_max=0))
    db.add(SymbolMapping(symbol="JP225", hyperliquid_symbol="xyz:JP225", mt5_symbol="JP225z"))
    opportunity = ArbitrageOpportunity(
        symbol="JP225",
        direction="long_hyperliquid_short_mt5",
        status="executable",
        notional=450,
        quantity=1.0,
        hyperliquid_quantity=0.00015,
        mt5_quantity=1.0,
        gross_spread=20,
        unit_cost=1,
        unit_net_profit=19,
        entry_threshold=10,
        exit_target=2,
        total_cost=1,
        net_profit=19,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()
    submitted = []

    class FakeGateway:
        def __init__(self, adapter) -> None:
            self.platform = adapter.platform

        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, f"{intent.platform}-open", "filled", intent.quantity, 100.0, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.0, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.0, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.mt5_market_order_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, message="ok"))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway(adapter))
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: (SimpleNamespace(hyperliquid=SimpleNamespace(local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)), time_diff_ms=0), ""))
    monkeypatch.setattr("app.execution.engine.pre_trade_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, reason=""))
    monkeypatch.setattr("app.execution.engine.get_settings", lambda: SimpleNamespace(hyperliquid_paper_live_order_enabled=True, paper_live_parallel_execution=True, strict_quote_sync_ms=500, quote_stale_ms=1500, default_slippage_bps=0))

    group = open_hedge_group(db, opportunity.id)

    assert group.status == "open"
    assert {intent.platform for intent in submitted} == {"hyperliquid", "mt5"}
    assert db.query(Order).filter(Order.hedge_group_id == group.id).count() == 2


def test_open_rejects_when_refreshed_quotes_no_longer_meet_entry(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper"))
    db.add(SymbolMapping(symbol="JP225", hyperliquid_symbol="xyz:JP225", mt5_symbol="JP225z"))
    opportunity = ArbitrageOpportunity(
        symbol="JP225",
        direction="long_hyperliquid_short_mt5",
        status="executable",
        notional=450,
        quantity=1.0,
        hyperliquid_quantity=1.0,
        mt5_quantity=1.0,
        gross_spread=20,
        unit_cost=1,
        unit_net_profit=19,
        entry_threshold=10,
        exit_target=2,
        total_cost=1,
        net_profit=19,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()
    synced = SimpleNamespace(
        hyperliquid=SimpleNamespace(ask=100.0, bid=99.0, local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)),
        mt5=SimpleNamespace(ask=106.0, bid=105.0),
        time_diff_ms=10,
    )
    sync_results = [(None, "行情未对齐，时间差 900ms"), (synced, "")]

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.refresh_execution_quotes", lambda mapping: ["hyperliquid", "mt5"])
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.mt5_market_order_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, message="ok"))
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: sync_results.pop(0))

    with pytest.raises(ValueError, match="主动刷新后价差不再满足入场线"):
        open_hedge_group(db, opportunity.id)


def test_auto_close_skips_live_group_without_live_switch(monkeypatch) -> None:
    db, group_id = _live_close_test_db(auto_close_live_enabled=True, live_trading_enabled=False)
    _seed_auto_close_quotes()
    called = []
    hedge_pool.load_from_db(db)

    closed = run_auto_close(db)
    group = db.get(HedgeGroup, group_id)

    assert closed == 0
    assert called == []
    assert group.status == "open"


def test_auto_close_paper_group_submits_reverse_orders(monkeypatch) -> None:
    db, group_id = _live_close_test_db(auto_close_live_enabled=True, live_trading_enabled=True)
    db.get(StrategySetting, 1).execution_mode = "paper"
    group_row = db.get(HedgeGroup, group_id)
    group_row.execution_mode = "paper"
    db.commit()
    hedge_pool.load_from_db(db)
    _seed_auto_close_quotes()
    submitted = []

    class FakeGateway:
        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, f"{intent.platform}-auto-close", "filled", intent.quantity, 100.0, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.0, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.0, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.auto_closer.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.auto_closer.build_execution_gateway", lambda adapter: FakeGateway())

    closed = run_auto_close(db)
    persist_hedge_pool_events(db)
    group = db.get(HedgeGroup, group_id)

    assert closed == 1
    assert group.status == "closed"
    assert "平仓价差回归" in group.close_reason
    assert {order.reduce_only for order in db.query(Order).filter(Order.hedge_group_id == group.id).all()} == {True}
    assert [intent.reduce_only for intent in submitted] == [True, True]
    assert db.query(Fill).count() == 2


def test_reconcile_opening_group_advances_to_open(monkeypatch) -> None:
    db, group = _pending_reconcile_test_db("opening")

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": 1.0, "average_price": 100.0, "fee": 0.1}

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())
    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    assert changed
    assert group.status == "open"
    assert db.query(Fill).count() == 2


def test_reconcile_closing_group_advances_to_closed(monkeypatch) -> None:
    db, group = _pending_reconcile_test_db("closing")

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            quantity = 1.0 if platform == "hyperliquid" else 0.1
            return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": quantity, "average_price": 100.0, "fee": 0.1}

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())
    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    assert changed
    assert group.status == "closed"
    assert group.closed_at is not None
    assert group.unrealized_pnl == 0.0
    assert db.query(Fill).count() == 2


def test_reconcile_recovers_hyperliquid_fill_from_account_snapshot(monkeypatch) -> None:
    db, group = _pending_reconcile_test_db("opening")

    class FakeGateway:
        def query_account_orders(self, platform):
            return [
                {
                    "status": "filled",
                    "external_order_id": "hl-1",
                    "symbol": "OIL",
                    "side": "buy",
                    "quantity": 1.0,
                    "filled_quantity": 1.0,
                    "average_price": 76.5,
                    "fee": 0.2,
                    "message": "account snapshot",
                }
            ]

        def query_order(self, platform, external_order_id):
            if platform == "mt5":
                return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": 0.1, "average_price": 76.6, "fee": 0.1}
            return {"status": "accepted", "external_order_id": external_order_id}

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())

    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    hl_order = db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "hyperliquid").one()
    assert changed
    assert group.status == "open"
    assert hl_order.status == "filled"
    assert hl_order.price == 76.5
    assert db.query(Fill).filter(Fill.order_id == hl_order.id).one().fee == 0.2


def test_reconcile_recovers_missing_hyperliquid_external_id_from_unique_account_snapshot(monkeypatch) -> None:
    db, group = _pending_reconcile_test_db("opening")
    hl_order = db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "hyperliquid").one()
    hl_order.external_order_id = ""
    db.commit()

    class FakeGateway:
        def query_account_orders(self, platform):
            return [
                {
                    "status": "filled",
                    "external_order_id": "98765",
                    "symbol": "OIL",
                    "side": "buy",
                    "quantity": 1.0,
                    "filled_quantity": 1.0,
                    "average_price": 76.5,
                    "fee": 0.2,
                    "timestamp_ms": int(time.time() * 1000),
                    "message": "account snapshot",
                }
            ]

        def query_order(self, platform, external_order_id):
            if platform == "mt5":
                return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": 0.1, "average_price": 76.6, "fee": 0.1}
            return {"status": "accepted", "external_order_id": external_order_id}

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())

    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(hl_order)

    assert changed
    assert hl_order.external_order_id == "98765"
    assert hl_order.status == "filled"
    assert db.query(Fill).filter(Fill.order_id == hl_order.id).count() == 1


def test_reconcile_hyper_maker_fill_submits_mt5_taker(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="EUR", hyperliquid_symbol="xyz:EUR", mt5_symbol="EURUSD", execution_style="hyper_maker_mt5_taker"))
    group = HedgeGroup(
        symbol="EUR",
        direction="long_mt5_short_hyperliquid",
        status="opening",
        execution_mode="paper",
        notional=1145.0,
        quantity=0.01,
        hyperliquid_quantity=1000.0,
        mt5_quantity=0.01,
        open_cost=0.2,
    )
    db.add(group)
    db.flush()
    hl_order = Order(
        hedge_group_id=group.id,
        platform="hyperliquid",
        symbol="EUR",
        side="sell",
        quantity=1000.0,
        order_type="limit",
        status="filled",
        external_order_id="hl-maker-1",
    )
    db.add(hl_order)
    db.flush()
    db.add(Fill(order_id=hl_order.id, platform="hyperliquid", symbol="EUR", side="sell", quantity=500.0, price=1.146, fee=0.1))
    db.commit()
    db.refresh(group)
    submitted = []

    class FakeGateway:
        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, "mt5-taker-1", "filled", intent.quantity, 1.1458, 0.01)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 1.1458, 0.01)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 1.1458, 0.01, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())

    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    mt5_order = db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "mt5").one()
    assert changed
    assert group.status == "open"
    assert mt5_order.quantity == pytest.approx(0.005)
    assert submitted[0].venue_symbol == "EURUSD"
    assert submitted[0].side == "buy"
    assert db.query(Fill).count() == 2


def test_reconcile_taker_open_hyper_fill_submits_mt5_leg(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="JP225", hyperliquid_symbol="xyz:JP225", mt5_symbol="JP225z"))
    group = HedgeGroup(
        symbol="JP225",
        direction="long_hyperliquid_short_mt5",
        status="opening",
        execution_mode="paper",
        notional=450.0,
        quantity=1.0,
        hyperliquid_quantity=0.00625,
        mt5_quantity=1.0,
        open_cost=0.2,
    )
    db.add(group)
    db.flush()
    db.add(
        Order(
            hedge_group_id=group.id,
            platform="hyperliquid",
            symbol="JP225",
            side="buy",
            quantity=0.00625,
            status="accepted",
            external_order_id="hl-open-1",
        )
    )
    db.commit()
    db.refresh(group)
    submitted = []

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": 0.003125, "average_price": 72000.0, "fee": 0.1}

        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, "mt5-open-1", "filled", intent.quantity, 72010.0, 0.01)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 72010.0, 0.01)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 72010.0, 0.01, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())

    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    mt5_order = db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "mt5").one()
    assert changed
    assert group.status == "open"
    assert mt5_order.side == "sell"
    assert mt5_order.quantity == pytest.approx(0.5)
    assert mt5_order.reduce_only is False
    assert submitted[0].venue_symbol == "JP225z"
    assert db.query(Fill).count() == 2


def test_reconcile_taker_close_hyper_fill_submits_mt5_reduce_only_leg(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL"))
    group = HedgeGroup(
        symbol="OIL",
        direction="long_hyperliquid_short_mt5",
        status="closing",
        execution_mode="paper",
        notional=1000.0,
        quantity=1.0,
        hyperliquid_quantity=1.0,
        mt5_quantity=0.1,
        open_cost=0.2,
        unrealized_pnl=2.0,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(group)
    db.flush()
    db.add(
        Order(
            hedge_group_id=group.id,
            platform="hyperliquid",
            symbol="OIL",
            side="sell",
            quantity=1.0,
            reduce_only=True,
            status="accepted",
            external_order_id="hl-close-1",
        )
    )
    db.commit()
    db.refresh(group)
    submitted = []

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": 1.0, "average_price": 75.0, "fee": 0.1}

        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, "mt5-close-1", "filled", intent.quantity, 75.1, 0.01)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 75.1, 0.01)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 75.1, 0.01, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())

    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    mt5_order = db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "mt5").one()
    assert changed
    assert group.status == "closed"
    assert group.closed_at is not None
    assert group.unrealized_pnl == 0.0
    assert mt5_order.side == "buy"
    assert mt5_order.quantity == pytest.approx(0.1)
    assert mt5_order.reduce_only is True
    assert submitted[0].venue_symbol == "USOIL"
    assert submitted[0].reduce_only is True


def test_reconcile_opening_single_fill_cancels_pending_leg(monkeypatch) -> None:
    db, group = _pending_reconcile_test_db("opening")

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            if platform == "hyperliquid":
                return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": 1.0, "average_price": 100.0, "fee": 0.1}
            return {"status": "accepted", "external_order_id": external_order_id, "filled_quantity": 0.0}

        def cancel_order(self, platform, external_order_id):
            return True

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())
    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    mt5_order = db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "mt5").one()
    assert changed
    assert group.status == "manual_intervention"
    assert mt5_order.status == "canceled"
    assert "撤销未成交腿" in group.events[-1].detail


def test_reconcile_opening_single_fill_auto_reverses_filled_leg(monkeypatch) -> None:
    db, group = _pending_reconcile_test_db("opening")
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL", single_leg_action="auto_close"))
    db.commit()

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            if platform == "hyperliquid":
                return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": 1.0, "average_price": 100.0, "fee": 0.1}
            return {"status": "accepted", "external_order_id": external_order_id, "filled_quantity": 0.0}

        def cancel_order(self, platform, external_order_id):
            return True

        def submit_order(self, intent, *, paper_latency_ms=0):
            assert intent.platform == "hyperliquid"
            assert intent.side == "sell"
            assert intent.reduce_only is True
            result = AdapterOrderResult(True, "hl-comp", "filled", intent.quantity, 99.5, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 99.5, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 99.5, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())

    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    compensation = db.query(Order).filter(Order.hedge_group_id == group.id, Order.external_order_id == "hl-comp").one()
    assert changed
    assert group.status == "failed"
    assert group.fees == pytest.approx(0.4)
    assert compensation.side == "sell"
    assert compensation.reduce_only is True
    assert db.query(Fill).filter(Fill.order_id == compensation.id).count() == 1
    assert db.query(HedgeGroupEvent).filter(HedgeGroupEvent.event_type == "opening_single_leg_compensation").count() == 1


def test_reconcile_closing_single_fill_cancels_pending_leg(monkeypatch) -> None:
    db, group = _pending_reconcile_test_db("closing")

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            if platform == "hyperliquid":
                return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": 1.0, "average_price": 100.0, "fee": 0.1}
            return {"status": "accepted", "external_order_id": external_order_id, "filled_quantity": 0.0}

        def cancel_order(self, platform, external_order_id):
            return True

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())
    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    mt5_order = db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "mt5").one()
    assert changed
    assert group.status == "manual_intervention"
    assert mt5_order.status == "canceled"
    assert "撤销未成交腿" in group.close_reason


def test_reconcile_closing_single_fill_auto_reverses_and_restores_open(monkeypatch) -> None:
    db, group = _pending_reconcile_test_db("closing")
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL", single_leg_action="auto_close"))
    db.commit()

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            if platform == "hyperliquid":
                return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": 1.0, "average_price": 100.0, "fee": 0.1}
            return {"status": "accepted", "external_order_id": external_order_id, "filled_quantity": 0.0}

        def cancel_order(self, platform, external_order_id):
            return True

        def submit_order(self, intent, *, paper_latency_ms=0):
            assert intent.platform == "hyperliquid"
            assert intent.side == "buy"
            assert intent.reduce_only is True
            result = AdapterOrderResult(True, "hl-comp", "filled", intent.quantity, 100.5, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.5, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.5, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())

    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    compensation = db.query(Order).filter(Order.hedge_group_id == group.id, Order.external_order_id == "hl-comp").one()
    assert changed
    assert group.status == "open"
    assert group.fees == pytest.approx(0.4)
    assert compensation.side == "buy"
    assert compensation.reduce_only is True
    assert db.query(Fill).filter(Fill.order_id == compensation.id).count() == 1
    assert db.query(HedgeGroupEvent).filter(HedgeGroupEvent.event_type == "closing_single_leg_compensation").count() == 1


def test_reconcile_unreconstructable_pending_order_escalates_manual(monkeypatch) -> None:
    db, group = _pending_reconcile_test_db("opening")
    old_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=30)
    for order in db.query(Order).filter(Order.hedge_group_id == group.id).all():
        order.created_at = old_time
    db.commit()

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            return {"status": "not_ready", "external_order_id": external_order_id, "message": "本地 cache 不包含该订单"}

        def cancel_order(self, platform, external_order_id):
            return True

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())
    monkeypatch.setattr("app.execution.reconciler.get_settings", lambda: SimpleNamespace(execution_reconcile_pending_stale_seconds=1))

    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    assert changed
    assert group.status == "manual_intervention"
    assert "不可重建" in group.close_reason
    assert db.query(Alert).filter(Alert.title == "外部订单状态不可重建").count() == 1
    assert db.query(Order).filter(Order.hedge_group_id == group.id, Order.status == "canceled").count() == 2


def test_sync_live_positions_replaces_current_rows(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(Position(platform="mt5", symbol="OLD", side="long", quantity=1, entry_price=1, mark_price=1))
    db.add(SymbolMapping(symbol="JP225", hyperliquid_symbol="xyz:JP225", mt5_symbol="JP225"))
    db.commit()
    captured = {}

    class FakeHyperAdapter:
        platform = "hyperliquid"

        def __init__(self, live=False):
            pass

        def get_positions(self, dexes=None):
            captured["dexes"] = dexes
            return [{"platform": "hyperliquid", "symbol": "xyz:JP225", "side": "long", "quantity": 0.0002, "entry_price": 71875, "mark_price": 72015, "unrealized_pnl": 0.03}]

    class FakeMT5Adapter:
        platform = "mt5"

        def __init__(self, live=False):
            pass

        def get_positions(self):
            return [{"platform": "mt5", "symbol": "USOIL", "side": "short", "quantity": 0.1, "entry_price": 80, "mark_price": 79, "unrealized_pnl": 1.2}]

    monkeypatch.setattr("app.execution.reconciler.HyperliquidAdapter", FakeHyperAdapter)
    monkeypatch.setattr("app.execution.reconciler.MT5Adapter", FakeMT5Adapter)
    count = sync_live_positions(db)
    db.commit()

    rows = db.query(Position).all()
    assert captured["dexes"] == ["xyz"]
    assert count == 2
    assert [(row.platform, row.symbol, row.side, row.quantity) for row in rows] == [
        ("hyperliquid", "xyz:JP225", "long", 0.0002),
        ("mt5", "USOIL", "short", 0.1),
    ]


def test_reconcile_residual_positions_marks_closed_group_manual() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL"))
    group = HedgeGroup(
        symbol="OIL",
        direction="long_hyperliquid_short_mt5",
        status="closed",
        execution_mode="live",
        notional=1000,
        quantity=1.0,
        hyperliquid_quantity=1.0,
        mt5_quantity=0.1,
        open_cost=1.0,
        fees=0.2,
        unrealized_pnl=0.0,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
        closed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(group)
    db.flush()
    db.add(Position(platform="mt5", symbol="USOIL", side="short", quantity=0.1, entry_price=80, mark_price=79))
    db.commit()

    changed = reconcile_residual_positions(db)
    db.commit()
    db.refresh(group)

    assert changed == 1
    assert group.status == "manual_intervention"
    assert "USOIL" in group.close_reason
    assert db.query(Alert).filter(Alert.title == "平仓后残余仓位").count() == 1


def test_hyperliquid_live_position_sync_triggers_residual_reconcile(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"assetPositions": [{"position": {"coin": "OIL", "szi": "-1.5", "entryPx": "76", "markPx": "75.8"}}]}).encode("utf-8")

    class FakeMT5Adapter:
        platform = "mt5"

        def __init__(self, live=False):
            pass

        def get_positions(self):
            return []

    monkeypatch.setattr("app.adapters.hyperliquid.request.urlopen", lambda req, timeout: FakeResponse())
    monkeypatch.setattr("app.adapters.hyperliquid.get_settings", lambda: SimpleNamespace(
        hyperliquid_account_address="0xabc",
        hyperliquid_info_url="https://example.test/info",
    ))
    monkeypatch.setattr("app.execution.reconciler.MT5Adapter", FakeMT5Adapter)
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL"))
    group = HedgeGroup(
        symbol="OIL",
        direction="long_hyperliquid_short_mt5",
        status="closed",
        execution_mode="live",
        notional=1000,
        quantity=1.0,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
        closed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(group)
    db.commit()

    assert sync_live_positions(db) == 1
    changed = reconcile_residual_positions(db)
    db.commit()
    db.refresh(group)

    assert changed == 1
    assert group.status == "manual_intervention"
    assert db.query(Position).filter(Position.platform == "hyperliquid", Position.symbol == "OIL", Position.side == "short").count() == 1


def test_reconcile_orphan_positions_alerts_unmanaged_live_position() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(Position(platform="hyperliquid", symbol="OIL", side="long", quantity=1.5, entry_price=76, mark_price=77))
    db.commit()

    changed = reconcile_orphan_positions(db)
    changed_again = reconcile_orphan_positions(db)
    db.commit()

    assert changed == 1
    assert changed_again == 0
    alert = db.query(Alert).filter(Alert.title == "外部孤儿仓位").one()
    assert "hyperliquid:OIL:long:1.5" in alert.message


def test_reconcile_orphan_positions_ignores_position_with_live_group() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL"))
    db.add(
        HedgeGroup(
            symbol="OIL",
            direction="long_hyperliquid_short_mt5",
            status="open",
            execution_mode="live",
            notional=1000,
            quantity=1.0,
            hyperliquid_quantity=1.0,
            mt5_quantity=0.1,
            opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )
    db.add(Position(platform="hyperliquid", symbol="OIL", side="long", quantity=1.0, entry_price=76, mark_price=77))
    db.add(Position(platform="mt5", symbol="USOIL", side="short", quantity=0.1, entry_price=76, mark_price=77))
    db.commit()

    changed = reconcile_orphan_positions(db)

    assert changed == 0
    assert db.query(Alert).filter(Alert.title == "外部孤儿仓位").count() == 0


def test_reconcile_orphan_positions_requires_matching_side_and_quantity() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL", mt5_volume_step=0.01, mt5_contract_size=100, enabled=True))
    db.add(
        HedgeGroup(
            symbol="OIL",
            direction="long_hyperliquid_short_mt5",
            status="open",
            execution_mode="live",
            notional=1000,
            quantity=1,
            hyperliquid_quantity=1.0,
            mt5_quantity=0.1,
        )
    )
    db.add(Position(platform="mt5", symbol="USOIL", side="short", quantity=0.2, entry_price=70, mark_price=71))
    db.add(Position(platform="hyperliquid", symbol="OIL", side="short", quantity=1.0, entry_price=70, mark_price=71))
    db.commit()

    changed = reconcile_orphan_positions(db)

    assert changed == 2
    messages = [row.message for row in db.query(Alert).filter(Alert.title == "外部孤儿仓位").all()]
    assert any("mt5:USOIL:short:0.2" in message for message in messages)
    assert any("hyperliquid:OIL:short:1.0" in message for message in messages)


def test_adopt_position_creates_live_manual_group() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    user = User(username="admin", password_hash="x", role="admin")
    db.add(user)
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL"))
    position = Position(platform="mt5", symbol="USOIL", side="short", quantity=0.2, entry_price=76, mark_price=77, unrealized_pnl=-2.5)
    db.add(position)
    db.commit()
    db.refresh(user)
    db.refresh(position)

    group = api_router.adopt_position(position.id, AdoptPositionIn(reason="import broker position"), user=user, db=db)

    assert group["status"] == "manual_intervention"
    assert group["execution_mode"] == "live"
    assert group["symbol"] == "OIL"
    assert group["direction"] == "long_hyperliquid_short_mt5"
    assert group["hyperliquid_quantity"] == 0.0
    assert group["mt5_quantity"] == 0.2
    assert db.query(HedgeGroupEvent).filter(HedgeGroupEvent.event_type == "adopted_external_position").count() == 1
    assert db.query(AuditLog).filter(AuditLog.action == "adopt_position").count() == 1


def test_close_adopted_single_leg_group_only_closes_existing_leg(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="live"))
    db.add(SystemSetting(key="live_trading_enabled", value="true"))
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL", allow_hold_through_mt5_close=True))
    group = HedgeGroup(
        symbol="OIL",
        direction="long_hyperliquid_short_mt5",
        status="manual_intervention",
        execution_mode="live",
        notional=1000,
        quantity=0.2,
        hyperliquid_quantity=0.0,
        mt5_quantity=0.2,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    submitted = []

    class FakeGateway:
        def __init__(self, platform: str) -> None:
            self.platform = platform

        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append((intent.platform, intent.side, intent.quantity))
            result = AdapterOrderResult(True, f"{intent.platform}-close", "filled", intent.quantity, 100.0, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.0, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.0, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway(adapter.platform))
    monkeypatch.setattr("app.execution.engine.live_execution_readiness", lambda db: {"checks": []})

    closed = close_hedge_group(db, group.id, "close adopted")

    assert closed.status == "closed"
    assert submitted == [("mt5", "buy", 0.2)]
    assert db.query(Order).filter(Order.hedge_group_id == group.id).count() == 1


def test_paper_close_realized_pnl_uses_actual_close_fills(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper"))
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL"))
    group = HedgeGroup(
        symbol="OIL",
        direction="long_hyperliquid_short_mt5",
        status="open",
        execution_mode="paper",
        notional=1000,
        quantity=1.0,
        hyperliquid_quantity=1.0,
        mt5_quantity=0.1,
        entry_spread=5.0,
        open_cost=999.0,
        fees=0.2,
        unrealized_pnl=999.0,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(group)
    db.commit()

    class FakeGateway:
        def __init__(self, adapter) -> None:
            self.platform = adapter.platform

        def submit_order(self, intent, *, paper_latency_ms=0):
            price = 101.0 if intent.platform == "hyperliquid" else 103.0
            result = AdapterOrderResult(True, f"{intent.platform}-close", "filled", intent.quantity, price, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, price, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, price, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.refresh_execution_quotes", lambda *args, **kwargs: ["hyperliquid"])
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway(adapter))

    closed = close_hedge_group(db, group.id, "manual close")

    assert closed.status == "closed"
    assert closed.unrealized_pnl == 0.0
    assert round(closed.realized_pnl, 6) == 2.6


def _pending_reconcile_test_db(status: str):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    group = HedgeGroup(
        symbol="OIL",
        direction="long_hyperliquid_short_mt5",
        status=status,
        execution_mode="live",
        notional=1000,
        quantity=1.0,
        hyperliquid_quantity=1.0,
        mt5_quantity=0.1,
        open_cost=1.0,
        fees=0.2,
        unrealized_pnl=5.0,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None) if status == "closing" else None,
    )
    db.add(group)
    db.flush()
    db.add_all(
        [
            Order(hedge_group_id=group.id, platform="hyperliquid", symbol="OIL", side="buy" if status == "opening" else "sell", quantity=1.0, status="accepted", external_order_id="hl-1"),
            Order(hedge_group_id=group.id, platform="mt5", symbol="OIL", side="sell" if status == "opening" else "buy", quantity=0.1, status="accepted", external_order_id="12345"),
        ]
    )
    db.commit()
    db.refresh(group)
    return db, group


def _live_close_test_db(auto_close_live_enabled: bool = False, live_trading_enabled: bool = True):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(
        StrategySetting(
            execution_mode="live",
            auto_close_enabled=True,
            auto_close_live_enabled=auto_close_live_enabled,
            auto_close_min_profit=0.0,
            max_holding_minutes=240,
        )
    )
    db.add(SystemSetting(key="live_trading_enabled", value="true" if live_trading_enabled else "false"))
    db.add(
        SymbolMapping(
            symbol="OIL",
            hyperliquid_symbol="OIL",
            mt5_symbol="USOIL",
            allow_hold_through_mt5_close=True,
            hl_close_order_type="market",
            mt5_close_order_type="market",
        )
    )
    group = HedgeGroup(
        symbol="OIL",
        direction="long_hyperliquid_short_mt5",
        status="open",
        execution_mode="live",
        notional=1000,
        quantity=1.0,
        hyperliquid_quantity=1.0,
        mt5_quantity=0.1,
        open_cost=1.0,
        entry_spread=10.0,
        exit_target=2.0,
        fees=0.2,
        unrealized_pnl=5.0,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(group)
    db.commit()
    return db, group.id


def _seed_auto_close_quotes() -> None:
    quote_cache.put("hyperliquid", "OIL", bid=100.0, ask=101.0, depth_notional=10000, source="test")
    quote_cache.put("mt5", "OIL", bid=100.5, ask=101.0, depth_notional=10000, source="test")


def test_gateway_factory_uses_adapter_gateway_for_live_hyperliquid() -> None:
    built = build_execution_gateway(HyperliquidAdapter(live=True))
    assert isinstance(built, AdapterExecutionGateway)
    non_live = build_execution_gateway(PaperAdapter("hyperliquid"))
    assert isinstance(non_live, AdapterExecutionGateway)
    fallback = build_execution_gateway(PaperAdapter("mt5"))
    assert isinstance(fallback, AdapterExecutionGateway)


def test_gateway_factory_uses_adapter_gateway_for_simulated_hyperliquid() -> None:
    adapter = PaperAdapter("hyperliquid")
    setattr(adapter, "simulated", True)

    built = build_execution_gateway(adapter)

    assert isinstance(built, AdapterExecutionGateway)


def test_hedge_groups_api_returns_realtime_spreads() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    user = User(username="admin", password_hash="x", role="admin")
    db.add(user)
    db.add(
        HedgeGroup(
            symbol="OIL",
            direction="long_hyperliquid_short_mt5",
            status="open",
            execution_mode="paper",
            notional=1000,
            quantity=1,
            entry_spread=12,
            exit_target=2,
        )
    )
    db.commit()
    quote_cache.put("hyperliquid", "OIL", bid=99, ask=101, depth_notional=1000, source="test")
    quote_cache.put("mt5", "OIL", bid=110, ask=111, depth_notional=1000, source="test")

    result = api_router.hedge_groups(user, db, page=1, page_size=20)

    item = result["items"][0]
    assert item["entry_spread"] == 12
    assert item["current_entry_spread"] == 9
    assert item["current_close_spread"] == 12
    assert item["quote_time_diff_ms"] >= 0
    assert item["quote_age_ms"] >= 0


def test_hedge_groups_api_returns_runtime_unrealized_pnl() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    user = User(username="admin", password_hash="x", role="admin")
    db.add(user)
    group = HedgeGroup(
        symbol="GROUP-PNL",
        direction="long_hyperliquid_short_mt5",
        status="open",
        execution_mode="paper",
        notional=1000,
        quantity=1,
        hyperliquid_quantity=2,
        mt5_quantity=2,
        entry_spread=20,
        unrealized_pnl=0,
    )
    db.add(group)
    db.commit()
    hedge_pool.load_from_db(db)
    quote_cache.put("hyperliquid", "GROUP-PNL", bid=100, ask=101, depth_notional=1000, source="test")
    quote_cache.put("mt5", "GROUP-PNL", bid=115, ask=116, depth_notional=1000, source="test")

    result = api_router.hedge_groups(user, db, page=1, page_size=20)

    item = result["items"][0]
    assert item["current_close_spread"] == 16
    assert item["unrealized_pnl"] == 8


def test_hedge_groups_stream_channel_returns_only_current_page() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add_all(
        [
            HedgeGroup(symbol="HG1", direction="long_hyperliquid_short_mt5", status="open", execution_mode="paper", notional=100, quantity=1),
            HedgeGroup(symbol="HG2", direction="long_hyperliquid_short_mt5", status="open", execution_mode="paper", notional=100, quantity=1),
        ]
    )
    db.commit()

    event = api_router._stream_snapshot(db, channel="hedge-groups", page=1, page_size=1)

    assert set(event) == {"hedge_groups"}
    assert event["hedge_groups"]["total"] == 2
    assert event["hedge_groups"]["page"] == 1
    assert len(event["hedge_groups"]["items"]) == 1


def test_positions_stream_channel_returns_only_positions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(Position(platform="mt5", symbol="USOIL", side="short", quantity=0.2, entry_price=76, mark_price=77, unrealized_pnl=-2.5))
    db.commit()

    event = api_router._stream_snapshot(db, channel="positions")

    assert set(event) == {"positions"}
    assert len(event["positions"]) == 1
    assert event["positions"][0]["symbol"] == "USOIL"
    assert event["positions"][0]["unrealized_pnl"] == -2.5


def test_accounts_stream_channel_returns_only_latest_accounts() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add_all(
        [
            AccountSnapshot(platform="hyperliquid", equity=100, available_balance=90, margin_used=10, margin_ratio=10),
            AccountSnapshot(platform="mt5", equity=200, available_balance=180, margin_used=20, margin_ratio=10),
        ]
    )
    db.commit()

    event = api_router._stream_snapshot(db, channel="accounts")

    assert set(event) == {"accounts"}
    assert {item["platform"] for item in event["accounts"]} == {"hyperliquid", "mt5"}
    assert sum(item["equity"] for item in event["accounts"]) == 300


def test_execution_stream_channel_returns_current_order_and_fill_pages() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    order = Order(platform="hyperliquid", symbol="OIL", side="buy", quantity=1, status="filled")
    db.add(order)
    db.flush()
    db.add(Fill(order_id=order.id, platform="hyperliquid", symbol="OIL", side="buy", quantity=1, price=80, fee=0.1))
    db.commit()

    event = api_router._stream_snapshot(db, channel="execution", page=1, fill_page=1, page_size=20)

    assert set(event) == {"orders", "fills"}
    assert event["orders"]["total"] == 1
    assert event["orders"]["items"][0]["symbol"] == "OIL"
    assert event["fills"]["total"] == 1
    assert event["fills"]["items"][0]["price"] == 80


def test_dashboard_summary_uses_runtime_unrealized_pnl() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(AccountSnapshot(platform="hyperliquid", equity=100, available_balance=90, margin_used=10, margin_ratio=10))
    group = HedgeGroup(
        symbol="DASH-PNL",
        direction="long_hyperliquid_short_mt5",
        status="open",
        execution_mode="paper",
        notional=1000,
        quantity=1,
        hyperliquid_quantity=2,
        mt5_quantity=2,
        entry_spread=20,
        unrealized_pnl=0,
    )
    closed = HedgeGroup(symbol="DASH-CLOSED", direction="long_hyperliquid_short_mt5", status="closed", execution_mode="paper", notional=100, quantity=1, realized_pnl=3)
    db.add_all([group, closed])
    db.commit()
    hedge_pool.load_from_db(db)
    quote_cache.put("hyperliquid", "DASH-PNL", bid=100, ask=101, depth_notional=1000, source="test")
    quote_cache.put("mt5", "DASH-PNL", bid=115, ask=116, depth_notional=1000, source="test")

    result = api_router.dashboard_summary(User(username="admin", password_hash="x", role="admin"), db)

    assert result["equity"] == 100
    assert result["realized_pnl"] == 3
    assert result["unrealized_pnl"] == 8
    assert result["today_pnl"] == 11


def test_dashboard_stream_channel_returns_summary_and_curve() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(AccountSnapshot(platform="mt5", equity=200, available_balance=180, margin_used=20, margin_ratio=10))
    db.commit()

    event = api_router._stream_snapshot(db, channel="dashboard")

    assert set(event) == {"dashboard_summary", "equity_curve"}
    assert event["dashboard_summary"]["equity"] == 200
    assert len(event["equity_curve"]) == 1


def test_equity_curve_aggregates_platform_snapshots_by_sync_batch() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    first = datetime(2026, 6, 25, 3, 12, 0)
    second = first + timedelta(minutes=5)
    db.add_all(
        [
            AccountSnapshot(platform="hyperliquid", equity=0, available_balance=0, margin_used=0, margin_ratio=1, created_at=first),
            AccountSnapshot(platform="mt5", equity=50000, available_balance=49000, margin_used=1000, margin_ratio=50, created_at=first + timedelta(milliseconds=300)),
            AccountSnapshot(platform="hyperliquid", equity=100, available_balance=100, margin_used=0, margin_ratio=1, created_at=second),
            AccountSnapshot(platform="mt5", equity=49900, available_balance=48900, margin_used=1000, margin_ratio=50, created_at=second + timedelta(milliseconds=300)),
        ]
    )
    db.commit()

    curve = api_router._equity_curve_payload(db)

    assert [point["platform"] for point in curve] == ["total", "total"]
    assert [point["equity"] for point in curve] == [50000, 50000]
    assert curve[0]["platforms"] == {"hyperliquid": 0, "mt5": 50000}


def test_logs_stream_channel_returns_current_log_and_alert_pages() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SystemLog(level="info", category="test", message="hello"))
    db.add(Alert(level="critical", title="risk", message="check"))
    db.commit()

    event = api_router._stream_snapshot(db, channel="logs", page=1, alert_page=1, page_size=20)

    assert set(event) == {"logs", "alerts"}
    assert event["logs"]["total"] == 1
    assert event["logs"]["items"][0]["message"] == "hello"
    assert event["alerts"]["total"] == 1
    assert event["alerts"]["items"][0]["title"] == "risk"


def test_risk_stream_channel_returns_status_and_current_event_page() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(RiskSetting(mode="paused"))
    db.add(RiskEvent(level="warning", rule="latency", message="slow", symbol="OIL"))
    db.commit()

    event = api_router._stream_snapshot(db, channel="risk", page=1, page_size=10)

    assert set(event) == {"risk_status", "risk_events"}
    assert event["risk_status"]["mode"] == "paused"
    assert event["risk_events"]["total"] == 1
    assert event["risk_events"]["items"][0]["rule"] == "latency"


def test_lead_lag_stream_channel_returns_only_report() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()

    event = api_router._stream_snapshot(db, channel="lead-lag", symbol="JP225", window_seconds=60, threshold_bps=3, min_move=0, max_lag_ms=2000)

    assert set(event) == {"lead_lag"}
    assert event["lead_lag"]["symbol"] == "JP225"
    assert "summary" in event["lead_lag"]


def test_pipeline_pool_payload_uses_stable_stage_symbol_id_order() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    groups = [
        HedgeGroup(id=5, symbol="ZINC", direction="long_mt5_short_hyperliquid", status="manual_intervention", execution_mode="paper", notional=1, quantity=1),
        HedgeGroup(id=3, symbol="OIL", direction="long_mt5_short_hyperliquid", status="open", execution_mode="paper", notional=1, quantity=1),
        HedgeGroup(id=2, symbol="EUR", direction="long_mt5_short_hyperliquid", status="opening", execution_mode="paper", notional=1, quantity=1),
        HedgeGroup(id=1, symbol="BTC", direction="long_mt5_short_hyperliquid", status="pending_open", execution_mode="paper", notional=1, quantity=1),
        HedgeGroup(id=4, symbol="BTC", direction="long_mt5_short_hyperliquid", status="closing", execution_mode="paper", notional=1, quantity=1),
    ]

    items = _pool_payload(groups, now)["items"]

    assert [(item["stage"], item["symbol"], item["id"]) for item in items] == [
        ("pending", "BTC", 1),
        ("opening", "EUR", 2),
        ("open", "OIL", 3),
        ("closing", "BTC", 4),
        ("manual", "ZINC", 5),
    ]


def test_pipeline_pool_payload_calculates_runtime_unrealized_pnl() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    group = HedgeGroup(
        id=9,
        symbol="POOL-PNL",
        direction="long_hyperliquid_short_mt5",
        status="open",
        execution_mode="paper",
        notional=1000,
        quantity=1,
        hyperliquid_quantity=2,
        mt5_quantity=2,
        entry_spread=20,
        unrealized_pnl=0,
    )
    quote_cache.put("hyperliquid", "POOL-PNL", bid=100, ask=101, depth_notional=1000, source="test")
    quote_cache.put("mt5", "POOL-PNL", bid=115, ask=116, depth_notional=1000, source="test")

    item = _pool_payload([group], now)["items"][0]

    assert item["current_close_spread"] == 16
    assert item["unrealized_pnl"] == 8


def test_pipeline_pool_payload_accepts_hedge_group_snapshot() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    snapshot = HedgeGroupSnapshot(
        id=10,
        symbol="POOL-SNAPSHOT",
        direction="long_hyperliquid_short_mt5",
        status="open",
        execution_mode="paper",
        notional=1000,
        quantity=1,
        mt5_quantity=1,
        hyperliquid_quantity=1,
        open_cost=0,
        fees=0,
        funding=0,
        swap=0,
        realized_pnl=0,
        unrealized_pnl=0,
        trigger_spread=20,
        entry_spread=20,
        entry_threshold=20,
        exit_target=10,
        overheat_threshold=0,
        close_reason="",
        opened_at=now,
        closed_at=None,
        source="auto_paper",
    )

    item = _pool_payload([snapshot], now)["items"][0]

    assert item["id"] == 10
    assert item["age_ms"] == 0


def test_xyz_growth_mode_uses_effective_fee_multiplier() -> None:
    taker, maker, source = _hyperliquid_effective_fee_rates(
        "xyz:JP225",
        0.00045,
        0.00015,
        {"xyz:JP225": {"growthMode": "enabled"}},
    )
    assert taker == pytest.approx(0.00009)
    assert maker == pytest.approx(0.00003)
    assert "xyz_growth" in source


def test_xyz_missing_meta_falls_back_to_growth_fee_multiplier() -> None:
    taker, maker, source = _hyperliquid_effective_fee_rates("xyz:JPY", 0.00045, 0.00015, {})
    assert taker == pytest.approx(0.00009)
    assert maker == pytest.approx(0.00003)
    assert "xyz_growth" in source


def test_spread_analytics_empty_summary() -> None:
    summary = summarize_spreads([], "1h")
    assert summary["analytics_status"] == "no_data"
    assert summary["sample_count"] == 0


def test_spread_and_opportunity_apis_prefer_memory_scan_state() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="BTC", hyperliquid_symbol="BTC", mt5_symbol="BTCUSD", enabled=True))
    db.add(SymbolMapping(symbol="ETH", hyperliquid_symbol="ETH", mt5_symbol="ETHUSD", enabled=True))
    db.commit()
    scan_state_store.update(
        [
            {
                "id": 10,
                "symbol": "BTC",
                "direction": "long_mt5_short_hyperliquid",
                "hyperliquid_bid": 100.0,
            }
        ],
        [
            {
                "id": 20,
                "symbol": "ETH",
                "direction": "long_hyperliquid_short_mt5",
                "status": "candidate",
                "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
            }
        ],
    )

    spreads_payload = api_router.spreads(SimpleNamespace(), db, page=1, page_size=20)
    opportunities_payload = api_router.opportunities(SimpleNamespace(), db, page=1, page_size=20)

    assert spreads_payload["items"][0]["symbol"] == "BTC"
    assert opportunities_payload["items"][0]["symbol"] == "ETH"


def test_scan_state_spread_dict_includes_compute_timings() -> None:
    scanner_module._scan_timings["BTC"] = {
        "symbol_scan_duration_ms": 2.5,
        "signal_duration_ms": 0.4,
        "candidate_sync_duration_ms": 0.2,
    }
    row = SimpleNamespace(
        symbol="BTC",
        status="rejected",
        __table__=SimpleNamespace(columns=[SimpleNamespace(name="symbol"), SimpleNamespace(name="status")]),
    )

    data = scanner_module._spread_state_dict(row)

    assert data["symbol"] == "BTC"
    assert data["symbol_scan_duration_ms"] == 2.5
    assert data["signal_duration_ms"] == 0.4
    assert data["candidate_sync_duration_ms"] == 0.2


def test_delete_symbol_mapping_clears_current_scan_state() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    mapping = SymbolMapping(symbol="BTC", hyperliquid_symbol="BTC", mt5_symbol="BTCUSD", enabled=True)
    db.add(mapping)
    db.flush()
    db.add(SpreadCurrent(symbol="BTC", direction="none", hyperliquid_bid=1, hyperliquid_ask=1, mt5_bid=1, mt5_ask=1, quantity=1, gross_spread=0, unit_cost=0, unit_net_profit=0, total_cost=0, net_profit=0, annualized_return=0, status="rejected"))
    db.add(ArbitrageOpportunity(symbol="BTC", direction="long_mt5_short_hyperliquid", notional=1, quantity=1, gross_spread=1, total_cost=0, net_profit=1, annualized_return=1, status="candidate"))
    db.commit()
    scan_state_store.update([{"symbol": "BTC"}], [{"symbol": "BTC", "status": "candidate"}])

    api_router.delete_symbol_mapping(mapping.id, SimpleNamespace(id=1), db)

    state = scan_state_store.snapshot()
    assert state["spreads"] == []
    assert state["opportunities"] == []
    assert db.query(SpreadCurrent).filter(SpreadCurrent.symbol == "BTC").count() == 0
    assert db.query(ArbitrageOpportunity).filter(ArbitrageOpportunity.symbol == "BTC", ArbitrageOpportunity.status == "candidate").count() == 0


def test_spread_analytics_detects_mean_reversion_shape() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    values = [1.0 + (0.4 * (0.92 ** index)) for index in range(160)]
    points = [
        SpreadPoint(created_at=now + timedelta(seconds=index * 10), spread=value, total_cost=0.1, net_profit=value - 0.1)
        for index, value in enumerate(values)
    ]
    summary = summarize_spreads(points, "1h")
    assert summary["sample_count"] == 160
    assert summary["half_life_seconds"] is not None
    assert summary["opportunity_score"] >= 0


def test_spread_series_downsamples_large_window() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    points = [
        SpreadPoint(created_at=now + timedelta(seconds=index), spread=float(index), total_cost=0.1, net_profit=0.0)
        for index in range(3600)
    ]
    series = downsample_spreads(points, "1h")
    assert len(series) <= 720
    assert series[0]["count"] >= 1


def test_spread_analytics_uses_raw_snapshots_through_4h() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        from app.db.models import SpreadBucket, SpreadSnapshot

        db.add(
            SpreadBucket(
                symbol="BTC",
                direction="long_mt5_short_hyperliquid",
                bucket_start=now - timedelta(minutes=10),
                bucket_seconds=5,
                open_spread=100,
                high_spread=100,
                low_spread=100,
                close_spread=100,
                avg_spread=100,
                avg_unit_cost=10,
                avg_unit_net_profit=90,
                sample_count=1,
            )
        )
        db.add(
            SpreadSnapshot(
                symbol="BTC",
                direction="long_mt5_short_hyperliquid",
                hyperliquid_bid=1,
                hyperliquid_ask=1,
                mt5_bid=1,
                mt5_ask=1,
                gross_spread=200,
                unit_cost=20,
                unit_net_profit=180,
                total_cost=20,
                net_profit=180,
                annualized_return=0,
                status="candidate",
                created_at=now - timedelta(minutes=5),
            )
        )
        db.commit()

        points = load_spread_points(db, "BTC", "long_mt5_short_hyperliquid", "4h")

    assert [point.spread for point in points] == [200]


def test_spread_analytics_uses_buckets_for_24h_and_7d() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        from app.db.models import SpreadBucket, SpreadSnapshot

        db.add(
            SpreadBucket(
                symbol="BTC",
                direction="long_mt5_short_hyperliquid",
                bucket_start=now - timedelta(hours=6),
                bucket_seconds=5,
                open_spread=100,
                high_spread=100,
                low_spread=100,
                close_spread=100,
                avg_spread=100,
                avg_unit_cost=10,
                avg_unit_net_profit=90,
                sample_count=1,
            )
        )
        db.add(
            SpreadSnapshot(
                symbol="BTC",
                direction="long_mt5_short_hyperliquid",
                hyperliquid_bid=1,
                hyperliquid_ask=1,
                mt5_bid=1,
                mt5_ask=1,
                gross_spread=200,
                unit_cost=20,
                unit_net_profit=180,
                total_cost=20,
                net_profit=180,
                annualized_return=0,
                status="candidate",
                created_at=now - timedelta(hours=6),
            )
        )
        db.commit()

        points = load_spread_points(db, "BTC", "long_mt5_short_hyperliquid", "7d")

    assert [point.spread for point in points] == [100]


def test_direction_spreads_separate_entry_close_and_mid() -> None:
    long_hl = spreads_for_direction("long_hyperliquid_short_mt5", hl_bid=99, hl_ask=101, mt5_bid=110, mt5_ask=111)
    long_mt5 = spreads_for_direction("long_mt5_short_hyperliquid", hl_bid=99, hl_ask=101, mt5_bid=110, mt5_ask=111)

    assert long_hl.entry_spread == 9
    assert long_hl.close_spread == 12
    assert long_hl.mid_spread == 10.5
    assert long_hl.spread_cost == 3
    assert long_mt5.entry_spread == -12
    assert long_mt5.close_spread == -9
    assert long_mt5.spread_cost == 3


def test_load_spread_points_supports_close_and_mid_basis() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        from app.db.models import SpreadSnapshot

        db.add(
            SpreadSnapshot(
                symbol="BTC",
                direction="long_mt5_short_hyperliquid",
                hyperliquid_bid=99,
                hyperliquid_ask=101,
                mt5_bid=110,
                mt5_ask=111,
                gross_spread=9,
                entry_spread=9,
                close_spread=12,
                mid_spread=10.5,
                spread_cost=3,
                total_cost=0,
                net_profit=0,
                annualized_return=0,
                status="candidate",
                created_at=now,
            )
        )
        db.commit()

        entry = load_spread_points(db, "BTC", "long_mt5_short_hyperliquid", "1h", basis="entry")
        close = load_spread_points(db, "BTC", "long_mt5_short_hyperliquid", "1h", basis="close")
        mid = load_spread_points(db, "BTC", "long_mt5_short_hyperliquid", "1h", basis="mid")

    assert [point.spread for point in entry] == [9]
    assert [point.spread for point in close] == [12]
    assert [point.spread for point in mid] == [10.5]


def test_statistical_exit_target_uses_close_spread_distribution() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        from app.db.models import SpreadBucket

        strategy = StrategySetting(
            signal_mode="statistical",
            statistical_lookback_range="1h",
            statistical_min_samples=20,
            reachable_entry_percentile=0.75,
            reachable_entry_zscore=0.0,
            exit_target_percentile=0.25,
            cost_guard_percentile=0.5,
            min_total_profit=0,
        )
        for index in range(30):
            db.add(
                SpreadBucket(
                    symbol="JP225",
                    direction="long_hyperliquid_short_mt5",
                    bucket_start=now - timedelta(seconds=30 - index),
                    bucket_seconds=1,
                    open_spread=100 + index,
                    high_spread=100 + index,
                    low_spread=100 + index,
                    close_spread=100 + index,
                    avg_spread=100 + index,
                    avg_entry_spread=100 + index,
                    avg_close_basis_spread=20 + index,
                    avg_unit_cost=0,
                    avg_unit_net_profit=100 + index,
                    sample_count=1,
                )
            )
        db.commit()

        signal = evaluate_entry_signal(db, strategy, "JP225", "long_hyperliquid_short_mt5", 126, 0, 126, 1, 1)

    assert signal.reachable_entry > 100
    assert signal.exit_target < 30


def test_symbol_spread_limits_tighten_statistical_thresholds() -> None:
    mapping = SymbolMapping(symbol="JP225", hyperliquid_symbol="xyz:JP225", mt5_symbol="JP225", min_entry_spread=150, max_close_spread=12)

    assert scanner_module._effective_entry_threshold(mapping, 120) == 150
    assert scanner_module._effective_entry_threshold(mapping, 180) == 180
    assert scanner_module._effective_exit_target(mapping, 20) == 12
    assert scanner_module._effective_exit_target(mapping, 0) == 12


def test_symbol_negative_close_spread_limit_tightens_exit_target() -> None:
    mapping = SymbolMapping(symbol="SPCX", hyperliquid_symbol="xyz:SPCX", mt5_symbol="SPCX", max_close_spread=-0.11)

    assert scanner_module._effective_exit_target(mapping, 0.047) == pytest.approx(-0.11)
    assert scanner_module._effective_exit_target(mapping, 0) == pytest.approx(-0.11)


def test_scanner_gate_combination_keeps_blockers_separate_from_signal() -> None:
    signal_gate = scanner_module.GateResult("executable", "signal ok", "signal")
    liquidity_gate = scanner_module.GateResult("candidate", "depth low", "liquidity", "liquidity")
    market_gate = scanner_module.GateResult("rejected", "mt5 blocked", "market", "market")

    assert scanner_module._combine_gates(signal_gate, liquidity_gate, market_gate) == market_gate
    assert scanner_module._combine_gates(signal_gate, liquidity_gate, scanner_module.GateResult("pass", "", "market")) == liquidity_gate
    assert scanner_module._combine_gates(signal_gate, scanner_module.GateResult("pass", "", "liquidity"), scanner_module.GateResult("pass", "", "market")).status == "executable"


def test_auto_close_fallback_uses_symbol_max_close_spread_without_samples(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        strategy = StrategySetting(statistical_min_samples=20, auto_close_min_profit=0)
        mapping = SymbolMapping(symbol="JP225", hyperliquid_symbol="xyz:JP225", mt5_symbol="JP225", max_close_spread=10)
        group = HedgeGroup(
            symbol="JP225",
            direction="long_hyperliquid_short_mt5",
            notional=1000,
            quantity=1,
            hyperliquid_quantity=1,
            entry_spread=100,
            entry_threshold=100,
            exit_target=0,
            open_cost=0,
            opened_at=now,
            status="open",
            unrealized_pnl=20,
        )
        db.add_all([strategy, mapping, group])
        db.commit()
        synced = SimpleNamespace(
            hyperliquid=SimpleNamespace(bid=110, ask=111),
            mt5=SimpleNamespace(bid=100, ask=101),
        )
        monkeypatch.setattr("app.execution.auto_closer.quote_synchronizer.synchronized", lambda *args, **kwargs: (synced, ""))

        evaluation = evaluate_auto_close(db, strategy, group)

    assert evaluation.exit_target == 10
    assert evaluation.should_close is True


def test_auto_close_final_check_uses_current_symbol_max_close_spread(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        strategy = StrategySetting(auto_close_min_profit=0)
        mapping = SymbolMapping(symbol="SPCX", hyperliquid_symbol="xyz:SPCX", mt5_symbol="SPCX", max_close_spread=-0.11)
        group = HedgeGroup(
            symbol="SPCX",
            direction="long_mt5_short_hyperliquid",
            status="open",
            execution_mode="paper",
            notional=2000,
            quantity=0.13,
            mt5_quantity=0.13,
            hyperliquid_quantity=13,
            entry_spread=0.14,
            exit_target=0.047,
            fees=0.18,
            opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add_all([strategy, mapping, group])
        db.commit()
        synced = SimpleNamespace(
            hyperliquid=SimpleNamespace(bid=157.58, ask=157.59, local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)),
            mt5=SimpleNamespace(bid=157.59, ask=157.65, local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)),
            time_diff_ms=0,
        )
        monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: (synced, ""))

        ok, reason = _final_close_still_executable(db, group, mapping, strategy, "平仓价差回归至退出线: 0.00 <= 0.05")

    assert _effective_close_exit_target(group, mapping) == pytest.approx(-0.11)
    assert ok is False
    assert "平仓价差 0.000000 > 退出线 -0.110000" in reason


def test_scanner_records_two_direction_current_rows(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(signal_mode="fixed_profit", min_net_profit=-999, min_annualized_return=-999, default_notional=1000))
    db.add(SymbolMapping(symbol="DUAL", hyperliquid_symbol="DUAL", mt5_symbol="DUAL", mt5_min_lot=1, mt5_volume_step=1, mt5_contract_size=1, enabled=True))
    db.commit()
    quote_cache.put("hyperliquid", "DUAL", bid=99, ask=101, depth_notional=100000, source="test")
    quote_cache.put("mt5", "DUAL", bid=110, ask=111, depth_notional=100000, source="test")
    synced = SimpleNamespace(
        hyperliquid=SimpleNamespace(bid=99, ask=101, mid=100, depth_notional=100000, local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)),
        mt5=SimpleNamespace(bid=110, ask=111, mid=110.5, depth_notional=100000, local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)),
        time_diff_ms=0,
    )
    monkeypatch.setattr(scanner_module.quote_synchronizer, "synchronized", lambda *args, **kwargs: (synced, ""))
    monkeypatch.setattr(scanner_module, "mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr(scanner_module, "hyperliquid_cost_inputs", lambda symbol: SimpleNamespace(source="test", maker_fee_rate=0, taker_fee_rate=0, funding_rate=0))
    monkeypatch.setattr(scanner_module, "mt5_cost_inputs", lambda *args, **kwargs: SimpleNamespace(source="test", commission_rate=0, swap_cost=0))
    monkeypatch.setattr(scanner_module.mt5_tradability_cache, "is_fresh_allowed", lambda *args, **kwargs: (True, "ok"))

    scanner_module.clear_strategy_setting_cache()
    symbol_module.clear_symbol_mapping_cache()
    scanner_module.run_scan(db)
    state = scan_state_store.snapshot()
    assert state["ready"] is True
    assert {row["direction"] for row in state["direction_spreads"] if row["symbol"] == "DUAL"} == {"long_hyperliquid_short_mt5", "long_mt5_short_hyperliquid"}
    scanner_module.persist_scan_state(db)
    rows = db.query(SpreadDirectionCurrent).filter(SpreadDirectionCurrent.symbol == "DUAL").all()
    current = db.query(SpreadCurrent).filter(SpreadCurrent.symbol == "DUAL").one()
    opportunity = db.query(ArbitrageOpportunity).filter(ArbitrageOpportunity.symbol == "DUAL", ArbitrageOpportunity.status == "executable").first()

    assert {row.direction for row in rows} == {"long_hyperliquid_short_mt5", "long_mt5_short_hyperliquid"}
    assert current.entry_spread == current.gross_spread
    assert current.close_spread != current.entry_spread
    assert opportunity is not None
    assert opportunity.trigger_hyperliquid_bid == 99
    assert opportunity.trigger_hyperliquid_ask == 101
    assert opportunity.trigger_mt5_bid == 110
    assert opportunity.trigger_mt5_ask == 111


def test_funding_day_bucket_and_positive_bias() -> None:
    now = datetime(2026, 1, 1)
    points = [
        FundingPoint(time=now + timedelta(hours=index), funding_rate=0.00001 if index < 6 else -0.000005)
        for index in range(8)
    ]
    summary = summarize_funding(points, "24h")
    buckets = bucket_funding_points(points, "day")
    assert summary["bias"] == "positive"
    assert summary["positive_count"] == 6
    assert summary["negative_count"] == 2
    assert buckets[0]["sum_funding_rate"] == pytest.approx(0.00005)
    assert buckets[0]["count"] == 8


def test_statistical_signal_uses_reachable_entry() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        strategy = StrategySetting(
            signal_mode="statistical",
            statistical_lookback_range="1h",
            statistical_min_samples=20,
            reachable_entry_percentile=0.75,
            reachable_entry_zscore=1.0,
            cost_guard_percentile=0.90,
            min_total_profit=0.1,
        )
        db.add(strategy)
        from app.db.models import SpreadBucket

        for index in range(30):
            spread = 100 + index
            db.add(
                SpreadBucket(
                    symbol="JP225",
                    direction="long_hyperliquid_short_mt5",
                    bucket_start=now + timedelta(seconds=index),
                    bucket_seconds=5,
                    open_spread=spread,
                    high_spread=spread,
                    low_spread=spread,
                    close_spread=spread,
                    avg_spread=spread,
                    avg_unit_cost=20,
                    avg_unit_net_profit=spread - 20,
                    sample_count=1,
                )
            )
        db.commit()
        signal = evaluate_entry_signal(db, strategy, "JP225", "long_hyperliquid_short_mt5", 126, 20, 106, 1, 1)
        assert signal.result.status == "executable"
        assert signal.reachable_entry > 0


def test_overheat_marks_risk_without_blocking_executable_entry() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        strategy = StrategySetting(
            signal_mode="statistical",
            statistical_lookback_range="1h",
            statistical_min_samples=20,
            reachable_entry_percentile=0.85,
            reachable_entry_zscore=1.0,
            cost_guard_percentile=0.90,
            min_total_profit=0.5,
        )
        db.add(strategy)
        db.add(SymbolMapping(symbol="JP225", hyperliquid_symbol="xyz:JP225", mt5_symbol="JP225", min_entry_spread=200))
        from app.db.models import SpreadBucket

        for index in range(30):
            spread = 100 + index
            db.add(
                SpreadBucket(
                    symbol="JP225",
                    direction="long_hyperliquid_short_mt5",
                    bucket_start=now + timedelta(seconds=index),
                    bucket_seconds=5,
                    open_spread=spread,
                    high_spread=spread,
                    low_spread=spread,
                    close_spread=spread,
                    avg_spread=spread,
                    avg_unit_cost=20,
                    avg_unit_net_profit=spread - 20,
                    sample_count=1,
                )
            )
        db.commit()

        signal = evaluate_entry_signal(db, strategy, "JP225", "long_hyperliquid_short_mt5", 263.1, 20, 243.1, 9.14, 1)

    mapping = SimpleNamespace(min_entry_spread=200)
    assert scanner_module._effective_entry_threshold(mapping, signal.reachable_entry) == 200
    assert signal.overheat < 200
    assert signal.result.status == "executable"
    assert "超过过热线" not in signal.result.reason
    tags = scanner_module._risk_tags(263.1, signal)
    assert tags == [
        {
            "type": "overheat",
            "message": f"价差超过过热线 {signal.overheat:.2f}",
            "value": 263.1,
            "threshold": signal.overheat,
        }
    ]


def test_statistical_signal_blocks_entry_when_samples_are_insufficient() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        strategy = StrategySetting(
            signal_mode="statistical",
            statistical_lookback_range="1h",
            statistical_min_samples=20,
            min_total_profit=0.1,
        )
        db.add(strategy)
        from app.db.models import SpreadBucket

        for index in range(5):
            db.add(
                SpreadBucket(
                    symbol="OIL",
                    direction="long_hyperliquid_short_mt5",
                    bucket_start=now + timedelta(seconds=index),
                    bucket_seconds=5,
                    open_spread=0.8,
                    high_spread=0.8,
                    low_spread=0.8,
                    close_spread=0.8,
                    avg_spread=0.8,
                    avg_unit_cost=0.02,
                    avg_unit_net_profit=0.78,
                    sample_count=1,
                )
            )
        db.commit()

        signal = evaluate_entry_signal(db, strategy, "OIL", "long_hyperliquid_short_mt5", 0.8, 0.02, 0.78, 50, 1)

        assert signal.result.status == "candidate"
        assert "统计样本不足" in signal.result.reason
        assert signal.reachable_entry == 0.0


def test_statistical_signal_reuses_stats_cache(monkeypatch) -> None:
    from app.strategy import statistical_signal as statistical_signal_module

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    strategy = StrategySetting(
        signal_mode="statistical",
        statistical_lookback_range="1h",
        statistical_min_samples=20,
        min_total_profit=0,
    )
    points = [SpreadPoint(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=index), 100 + index, 20, 80 + index) for index in range(30)]
    calls = {"count": 0}

    def fake_load_points(db, symbol, direction, range_value):
        calls["count"] += 1
        return points

    statistical_signal_module.clear_signal_stats_cache()
    monkeypatch.setattr(statistical_signal_module, "load_spread_points", fake_load_points)
    try:
        first = evaluate_entry_signal(db, strategy, "JP225", "long_hyperliquid_short_mt5", 126, 20, 106, 1, 1)
        second = evaluate_entry_signal(db, strategy, "JP225", "long_hyperliquid_short_mt5", 127, 20, 107, 1, 1)
    finally:
        statistical_signal_module.clear_signal_stats_cache()

    assert calls["count"] == 1
    assert first.reachable_entry == second.reachable_entry


def test_statistical_signal_reads_background_refreshed_stats(monkeypatch) -> None:
    from app.strategy import statistical_signal as statistical_signal_module

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    strategy = StrategySetting(
        signal_mode="statistical",
        statistical_lookback_range="1h",
        statistical_min_samples=20,
        min_total_profit=0,
    )
    db.add(strategy)
    db.add(SymbolMapping(symbol="JP225", hyperliquid_symbol="xyz:JP225", mt5_symbol="JP225", enabled=True))
    db.commit()
    points = [SpreadPoint(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=index), 100 + index, 20, 80 + index) for index in range(30)]
    calls = {"count": 0}

    def fake_load_points(db, symbol, direction, range_value):
        calls["count"] += 1
        return points

    statistical_signal_module.clear_signal_stats_cache()
    monkeypatch.setattr(statistical_signal_module, "load_spread_points", fake_load_points)
    try:
        assert refresh_signal_stats_cache(db) == 2
        monkeypatch.setattr(
            statistical_signal_module,
            "load_spread_points",
            lambda *args, **kwargs: pytest.fail("扫描热路径不应重新读取历史样本"),
        )
        signal = evaluate_entry_signal(db, strategy, "JP225", "long_hyperliquid_short_mt5", 126, 20, 106, 1, 1)
    finally:
        statistical_signal_module.clear_signal_stats_cache()

    assert calls["count"] == 2
    assert signal.reachable_entry > 0


def test_statistical_exit_target_uses_low_percentile_and_profit_buffer() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        strategy = StrategySetting(
            signal_mode="statistical",
            statistical_lookback_range="1h",
            statistical_min_samples=20,
            exit_target_percentile=0.25,
            cost_guard_percentile=0.90,
            auto_close_unit_profit_buffer=20,
            min_total_profit=0,
        )
        db.add(strategy)
        from app.db.models import SpreadBucket

        for index in range(30):
            spread = 80 + index * 10
            db.add(
                SpreadBucket(
                    symbol="JP225",
                    direction="long_hyperliquid_short_mt5",
                    bucket_start=now + timedelta(seconds=index),
                    bucket_seconds=5,
                    open_spread=spread,
                    high_spread=spread,
                    low_spread=spread,
                    close_spread=spread,
                    avg_spread=spread,
                    avg_unit_cost=70,
                    avg_unit_net_profit=spread - 70,
                    sample_count=1,
                )
            )
        db.commit()
        signal = evaluate_entry_signal(db, strategy, "JP225", "long_hyperliquid_short_mt5", 360, 70, 290, 10, 1)
        assert signal.exit_target == pytest.approx(152.5)
        assert signal.exit_target <= 360 - signal.cost_guard - strategy.auto_close_unit_profit_buffer


def test_statistical_exit_target_rejects_oversized_unit_buffer() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        strategy = StrategySetting(
            signal_mode="statistical",
            statistical_lookback_range="1h",
            statistical_min_samples=20,
            exit_target_percentile=0.25,
            cost_guard_percentile=0.90,
            auto_close_unit_profit_buffer=20,
            min_total_profit=0,
        )
        db.add(strategy)
        from app.db.models import SpreadBucket

        for index in range(30):
            spread = 0.08 + index * 0.001
            db.add(
                SpreadBucket(
                    symbol="OIL",
                    direction="long_mt5_short_hyperliquid",
                    bucket_start=now + timedelta(seconds=index),
                    bucket_seconds=5,
                    open_spread=spread,
                    high_spread=spread,
                    low_spread=spread,
                    close_spread=spread,
                    avg_spread=spread,
                    avg_unit_cost=0.03,
                    avg_unit_net_profit=spread - 0.03,
                    sample_count=1,
                )
            )
        db.commit()
        signal = evaluate_entry_signal(db, strategy, "OIL", "long_mt5_short_hyperliquid", 0.115, 0.03, 0.085, 0.85, 1)
        assert signal.exit_target == 0.0


def test_auto_close_uses_saved_exit_target() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        strategy = StrategySetting(auto_close_enabled=True, auto_close_min_profit=0.0)
        db.add(strategy)
        db.add(SymbolMapping(symbol="JP225", hyperliquid_symbol="xyz:JP225", mt5_symbol="JP225"))
        group = HedgeGroup(
            symbol="JP225",
            direction="long_hyperliquid_short_mt5",
            status="open",
            execution_mode="paper",
            notional=500,
            quantity=1,
            mt5_quantity=1,
            hyperliquid_quantity=1,
            open_cost=10,
            entry_spread=250,
            exit_target=170,
            opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(group)
        db.commit()
        quote_cache.put("hyperliquid", "JP225", 71330, 71331, 10000, "test")
        quote_cache.put("mt5", "JP225", 71490, 71495, 10000, "test")
        evaluation = evaluate_auto_close(db, strategy, group)
        assert evaluation.should_close
        assert evaluation.close_spread == 165
        assert evaluation.estimated_profit == 85


def test_auto_close_allows_zero_axis_close_without_exit_target() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        strategy = StrategySetting(auto_close_enabled=True, auto_close_min_profit=0.0, statistical_min_samples=200)
        db.add(strategy)
        group = HedgeGroup(
            symbol="OIL-ZERO",
            direction="long_hyperliquid_short_mt5",
            status="open",
            execution_mode="paper",
            notional=5000,
            quantity=0.07,
            mt5_quantity=0.07,
            hyperliquid_quantity=70,
            entry_spread=0.847,
            exit_target=0.0,
            fees=0.45,
            opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(group)
        db.commit()
        quote_cache.put("hyperliquid", "OIL-ZERO", 72.69, 72.70, 10000, "test")
        quote_cache.put("mt5", "OIL-ZERO", 72.55, 72.55, 10000, "test")

        evaluation = evaluate_auto_close(db, strategy, group)

        assert evaluation.should_close
        assert evaluation.close_spread == pytest.approx(-0.14)
        assert "零轴" in evaluation.reason


def test_auto_close_zero_axis_still_requires_min_profit() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        strategy = StrategySetting(auto_close_enabled=True, auto_close_min_profit=100.0, statistical_min_samples=200)
        db.add(strategy)
        group = HedgeGroup(
            symbol="OIL-ZERO-MIN",
            direction="long_hyperliquid_short_mt5",
            status="open",
            execution_mode="paper",
            notional=5000,
            quantity=0.07,
            mt5_quantity=0.07,
            hyperliquid_quantity=70,
            entry_spread=0.847,
            exit_target=0.0,
            fees=0.45,
            opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(group)
        db.commit()
        quote_cache.put("hyperliquid", "OIL-ZERO-MIN", 72.69, 72.70, 10000, "test")
        quote_cache.put("mt5", "OIL-ZERO-MIN", 72.55, 72.55, 10000, "test")

        evaluation = evaluate_auto_close(db, strategy, group)

        assert not evaluation.should_close
        assert "利润不足" in evaluation.reason


def test_paper_hyperliquid_funding_cost_uses_actual_rates(monkeypatch) -> None:
    group = HedgeGroup(
        symbol="JP225",
        direction="long_hyperliquid_short_mt5",
        status="open",
        execution_mode="paper",
        notional=1000,
        quantity=1,
        mt5_quantity=1,
        hyperliquid_quantity=1,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2),
    )
    mapping = SymbolMapping(symbol="JP225", hyperliquid_symbol="xyz:JP225", mt5_symbol="JP225")

    monkeypatch.setattr(
        "app.execution.carry_costs._post_hyperliquid_info",
        lambda payload: [{"time": payload["startTime"] + 1, "fundingRate": "0.0001"}, {"time": payload["startTime"] + 2, "fundingRate": "-0.00005"}],
    )

    assert round(_paper_hyperliquid_funding_cost(group, mapping), 8) == 0.05
    group.direction = "long_mt5_short_hyperliquid"
    assert round(_paper_hyperliquid_funding_cost(group, mapping), 8) == -0.05


def test_mt5_swap_cost_uses_position_swap_sign(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    group = HedgeGroup(
        symbol="OIL",
        direction="long_hyperliquid_short_mt5",
        status="open",
        execution_mode="paper",
        notional=1000,
        quantity=1,
        mt5_quantity=0.5,
        hyperliquid_quantity=1,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1),
    )
    db.add(group)
    db.commit()
    mapping = SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL")

    class FakeMT5:
        POSITION_TYPE_SELL = 1
        POSITION_TYPE_BUY = 0

        @staticmethod
        def positions_get(symbol=None):
            return [SimpleNamespace(symbol="USOIL", type=1, volume=0.5, swap=-1.25)]

    monkeypatch.setattr("app.execution.carry_costs._initialize_mt5", lambda mt5, settings: True)
    monkeypatch.setitem(sys.modules, "MetaTrader5", FakeMT5)

    assert _mt5_swap_cost(db, group, mapping) == 1.25


def test_lead_lag_detects_following_move() -> None:
    symbol = "LLTEST"
    quote_cache.put("hyperliquid", symbol, 100, 101, 10000, "test")
    quote_cache.put("mt5", symbol, 100, 101, 10000, "test")
    time.sleep(0.001)
    quote_cache.put("hyperliquid", symbol, 102, 103, 10000, "test")
    time.sleep(0.001)
    quote_cache.put("mt5", symbol, 102, 103, 10000, "test")
    report = lead_lag_report(symbol, window_seconds=60, threshold_bps=50, follow_ratio=0.5, max_lag_ms=2000)
    summary = report["summary"]["hyperliquid_to_mt5"]
    assert summary["event_count"] >= 1
    assert summary["follow_count"] >= 1


def test_mt5_pre_close_blocks_open_but_allows_close() -> None:
    state = MT5SessionState(
        symbol="BTC",
        status="pre_close_no_open",
        reason="MT5 临近休市，禁止新开仓但允许平仓",
        can_quote=True,
        can_open_long=False,
        can_open_short=False,
        can_close_long=True,
        can_close_short=True,
    )
    can_open, open_reason = mt5_action_allowed(state, "long_mt5_short_hyperliquid", "open")
    can_close, close_reason = mt5_action_allowed(state, "long_mt5_short_hyperliquid", "close")
    assert not can_open
    assert "不允许" in open_reason
    assert can_close
    assert close_reason == ""


def test_local_mt5_stock_close_only_blocks_open_but_allows_close() -> None:
    mapping = SymbolMapping(symbol="SPCX", hyperliquid_symbol="xyz:SPCX", mt5_symbol="SPCXz")
    apply_mt5_session_template(mapping, "stock_us_close_only")
    state = mt5_session_state(mapping, datetime(2026, 6, 23, 10, 30, tzinfo=timezone.utc))

    can_open, open_reason = mt5_action_allowed(state, "long_hyperliquid_short_mt5", "open")
    can_close, close_reason = mt5_action_allowed(state, "long_hyperliquid_short_mt5", "close")

    assert state.status == "reduce_only"
    assert state.session_source == "exness_template"
    assert not can_open
    assert "只平仓" in state.reason
    assert "不允许" in open_reason
    assert can_close
    assert close_reason == ""


def test_local_mt5_quote_only_blocks_close_for_indices() -> None:
    mapping = SymbolMapping(symbol="JP225", hyperliquid_symbol="JP225", mt5_symbol="JP225")
    apply_mt5_session_template(mapping, "index_us_jp")
    state = local_schedule_state(mapping, datetime(2026, 6, 23, 21, 30, tzinfo=timezone.utc))

    assert state is not None
    assert state.status == "quote_only"
    assert not state.can_open_long
    assert not state.can_close_long


def test_mt5_session_template_infers_spcx_as_stock() -> None:
    mapping = SymbolMapping(symbol="SPCX", hyperliquid_symbol="xyz:SPCX", mt5_symbol="SPCXz")
    assert infer_template(mapping) == "stock_us_close_only"


def test_live_execution_readiness_blocks_missing_live_prerequisites(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL", enabled=True))
    db.commit()

    def fake_import(name):
        raise ImportError(name)

    settings = SimpleNamespace(
        hyperliquid_account_address="",
        mt5_live_order_enabled=False,
        mt5_login="",
        mt5_server="",
    )
    monkeypatch.setattr("app.execution.readiness.import_module", fake_import)

    result = live_execution_readiness(db, settings)

    assert result["status"] == "blocked"
    blocked = {item["component"] for item in result["checks"] if item["status"] == "block"}
    assert {"global_live_switch", "hyperliquid_account_address", "hyperliquid_live_order_submit", "metatrader5_import"} <= blocked


def test_paper_execution_readiness_allows_demo_account(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL", mt5_volume_step=0.01, mt5_contract_size=100, enabled=True))
    db.commit()

    settings = SimpleNamespace(
        mt5_demo_order_enabled=True,
        mt5_login="123",
        mt5_password="pwd",
        mt5_server="broker-demo",
    )

    class FakeMT5:
        ACCOUNT_TRADE_MODE_DEMO = 0

        def initialize(self, **kwargs):
            return True

        def account_info(self):
            return SimpleNamespace(login=123, server="broker-demo", trade_mode=self.ACCOUNT_TRADE_MODE_DEMO)

        def last_error(self):
            return (0, "")

        def shutdown(self):
            return True

    def fake_import(name):
        if name == "MetaTrader5":
            return FakeMT5()
        return object()

    monkeypatch.setattr("app.execution.readiness.import_module", fake_import)

    result = paper_execution_readiness(db, settings)

    assert result["status"] == "ready"
    assert result["ready"] is True


def test_paper_execution_readiness_blocks_real_mt5_account(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL", mt5_volume_step=0.01, mt5_contract_size=100, enabled=True))
    db.commit()

    settings = SimpleNamespace(
        mt5_demo_order_enabled=True,
        mt5_login="",
        mt5_password="",
        mt5_server="",
    )

    class FakeMT5:
        ACCOUNT_TRADE_MODE_DEMO = 0

        def initialize(self, **kwargs):
            return True

        def account_info(self):
            return SimpleNamespace(login=123, server="broker-demo", trade_mode=2)

        def last_error(self):
            return (0, "")

        def shutdown(self):
            return True

    def fake_import(name):
        if name == "MetaTrader5":
            return FakeMT5()
        return object()

    monkeypatch.setattr("app.execution.readiness.import_module", fake_import)

    result = paper_execution_readiness(db, settings)

    assert result["status"] == "blocked"
    blocked = {item["component"] for item in result["checks"] if item["status"] == "block"}
    assert "mt5_demo_account" in blocked


def test_live_execution_readiness_blocks_hyperliquid_live_submit_after_sdk_removal(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SystemSetting(key="live_trading_enabled", value="true"))
    db.add(
        SymbolMapping(
            symbol="OIL",
            hyperliquid_symbol="OIL",
            mt5_symbol="USOIL",
            mt5_volume_step=0.01,
            mt5_contract_size=100,
            single_leg_action="manual_intervention",
            enabled=True,
        )
    )
    db.commit()

    settings = SimpleNamespace(
        hyperliquid_account_address="0xabc",
        hyperliquid_info_url="https://example.test/info",
        mt5_live_order_enabled=True,
        mt5_login="123",
        mt5_password="pwd",
        mt5_server="broker",
    )

    class FakeMT5:
        def initialize(self, **kwargs):
            return True

        def account_info(self):
            return SimpleNamespace(login=123, server="broker")

        def last_error(self):
            return (0, "")

        def shutdown(self):
            return True

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"marginSummary": {}, "assetPositions": []}).encode("utf-8")

    def fake_import(name):
        return FakeMT5() if name == "MetaTrader5" else object()

    monkeypatch.setattr("app.execution.readiness.import_module", fake_import)
    monkeypatch.setattr("app.execution.readiness.request.urlopen", lambda req, timeout: FakeResponse())

    result = live_execution_readiness(db, settings)

    assert result["status"] == "blocked"
    assert result["ready"] is False
    blocked = {item["component"] for item in result["checks"] if item["status"] == "block"}
    assert "hyperliquid_live_order_submit" in blocked


def test_live_execution_readiness_blocks_failed_read_probes(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SystemSetting(key="live_trading_enabled", value="true"))
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL", mt5_volume_step=0.01, mt5_contract_size=100, enabled=True))
    db.commit()

    class FakeMT5:
        def initialize(self, **kwargs):
            return True

        def account_info(self):
            return None

        def last_error(self):
            return (500, "no account")

        def shutdown(self):
            return True

    def fake_import(name):
        return FakeMT5() if name == "MetaTrader5" else object()

    def failing_urlopen(req, timeout):
        raise TimeoutError("timeout")

    settings = SimpleNamespace(
        hyperliquid_account_address="0xabc",
        hyperliquid_info_url="https://example.test/info",
        mt5_live_order_enabled=True,
        mt5_login="123",
        mt5_password="pwd",
        mt5_server="broker",
    )
    monkeypatch.setattr("app.execution.readiness.import_module", fake_import)
    monkeypatch.setattr("app.execution.readiness.request.urlopen", failing_urlopen)

    result = live_execution_readiness(db, settings)

    assert result["status"] == "blocked"
    blocked = {item["component"] for item in result["checks"] if item["status"] == "block"}
    assert {"hyperliquid_read_probe", "mt5_read_probe"} <= blocked


def test_live_execution_readiness_blocks_unmanaged_live_positions(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SystemSetting(key="live_trading_enabled", value="true"))
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL", mt5_volume_step=0.01, mt5_contract_size=100, enabled=True))
    db.add(Position(platform="hyperliquid", symbol="OIL", side="long", quantity=1, entry_price=70, mark_price=71))
    db.commit()

    class FakeMT5:
        def initialize(self, **kwargs):
            return True

        def account_info(self):
            return SimpleNamespace(login=123, server="broker")

        def last_error(self):
            return (0, "")

        def shutdown(self):
            return True

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"marginSummary": {}, "assetPositions": []}).encode("utf-8")

    settings = SimpleNamespace(
        hyperliquid_account_address="0xabc",
        hyperliquid_info_url="https://example.test/info",
        mt5_live_order_enabled=True,
        mt5_login="123",
        mt5_password="pwd",
        mt5_server="broker",
    )
    monkeypatch.setattr("app.execution.readiness.import_module", lambda name: FakeMT5() if name == "MetaTrader5" else object())
    monkeypatch.setattr("app.execution.readiness.request.urlopen", lambda req, timeout: FakeResponse())

    result = live_execution_readiness(db, settings)

    assert result["status"] == "blocked"
    blocked = {item["component"] for item in result["checks"] if item["status"] == "block"}
    assert "live_orphan_positions" in blocked


def test_live_execution_readiness_requires_managed_position_side_and_quantity(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SystemSetting(key="live_trading_enabled", value="true"))
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL", mt5_volume_step=0.01, mt5_contract_size=100, enabled=True))
    db.add(
        HedgeGroup(
            symbol="OIL",
            direction="long_hyperliquid_short_mt5",
            status="open",
            execution_mode="live",
            notional=1000,
            quantity=1,
            hyperliquid_quantity=1.0,
            mt5_quantity=0.1,
        )
    )
    db.add(Position(platform="mt5", symbol="USOIL", side="short", quantity=0.2, entry_price=70, mark_price=71))
    db.add(Position(platform="hyperliquid", symbol="OIL", side="short", quantity=1.0, entry_price=70, mark_price=71))
    db.commit()

    class FakeMT5:
        def initialize(self, **kwargs):
            return True

        def account_info(self):
            return SimpleNamespace(login=123, server="broker")

        def last_error(self):
            return (0, "")

        def shutdown(self):
            return True

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"marginSummary": {}, "assetPositions": []}).encode("utf-8")

    settings = SimpleNamespace(
        hyperliquid_account_address="0xabc",
        hyperliquid_info_url="https://example.test/info",
        mt5_live_order_enabled=True,
        mt5_login="123",
        mt5_password="pwd",
        mt5_server="broker",
    )
    monkeypatch.setattr("app.execution.readiness.import_module", lambda name: FakeMT5() if name == "MetaTrader5" else object())
    monkeypatch.setattr("app.execution.readiness.request.urlopen", lambda req, timeout: FakeResponse())

    result = live_execution_readiness(db, settings)

    assert result["status"] == "blocked"
    orphan = next(item for item in result["checks"] if item["component"] == "live_orphan_positions")
    assert "mt5:USOIL:short:0.2" in orphan["message"]
    assert "hyperliquid:OIL:short:1.0" in orphan["message"]


def test_live_execution_readiness_blocks_residual_closed_group_position(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SystemSetting(key="live_trading_enabled", value="true"))
    db.add(SymbolMapping(symbol="OIL", hyperliquid_symbol="OIL", mt5_symbol="USOIL", mt5_volume_step=0.01, mt5_contract_size=100, enabled=True))
    db.add(
        HedgeGroup(
            symbol="OIL",
            direction="long_hyperliquid_short_mt5",
            status="closed",
            execution_mode="live",
            notional=1000,
            quantity=1,
        )
    )
    db.add(Position(platform="mt5", symbol="USOIL", side="short", quantity=1, entry_price=70, mark_price=71))
    db.commit()

    class FakeMT5:
        def initialize(self, **kwargs):
            return True

        def account_info(self):
            return SimpleNamespace(login=123, server="broker")

        def last_error(self):
            return (0, "")

        def shutdown(self):
            return True

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"marginSummary": {}, "assetPositions": []}).encode("utf-8")

    settings = SimpleNamespace(
        hyperliquid_account_address="0xabc",
        hyperliquid_info_url="https://example.test/info",
        mt5_live_order_enabled=True,
        mt5_login="123",
        mt5_password="pwd",
        mt5_server="broker",
    )
    monkeypatch.setattr("app.execution.readiness.import_module", lambda name: FakeMT5() if name == "MetaTrader5" else object())
    monkeypatch.setattr("app.execution.readiness.request.urlopen", lambda req, timeout: FakeResponse())

    result = live_execution_readiness(db, settings)

    assert result["status"] == "blocked"
    blocked = {item["component"] for item in result["checks"] if item["status"] == "block"}
    assert "live_residual_positions" in blocked


def test_execution_reconcile_api_runs_reconciler_and_audits(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    user = User(username="admin", password_hash="x", role="admin")
    db.add(user)
    db.commit()
    db.refresh(user)
    monkeypatch.setattr(api_router, "run_execution_reconcile", lambda session: 3)

    result = api_router.execution_reconcile(user=user, db=db)

    assert result == {"status": "ok", "changed": 3, "cost_changed": 0}
    assert db.query(AuditLog).filter(AuditLog.action == "run_execution_reconcile", AuditLog.detail == "3").count() == 1


def test_symbol_mapping_file_seeds_missing_without_overwriting_existing(tmp_path, monkeypatch) -> None:
    mapping_file = tmp_path / "symbol_mappings.yaml"
    mapping_file.write_text(
        """
symbols:
  - symbol: BTC
    hyperliquid_symbol: BTC
    mt5_symbol: BTCUSD
    min_order_size: 1.23
    enabled: true
  - symbol: ETH
    hyperliquid_symbol: ETH
    mt5_symbol: ETHUSD
    min_order_size: 2.34
    enabled: true
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(symbol_module, "get_settings", lambda: type("Settings", (), {"symbol_mapping_path": str(mapping_file)})())
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        db.add(SymbolMapping(symbol="BTC", hyperliquid_symbol="BTC-PERP", mt5_symbol="BTCUSD", min_order_size=0.5))
        db.commit()
        seeded = symbol_module.seed_symbol_mappings_from_file(db)
        btc = db.query(SymbolMapping).filter(SymbolMapping.symbol == "BTC").one()
        eth = db.query(SymbolMapping).filter(SymbolMapping.symbol == "ETH").one()
        assert seeded == 1
        assert btc.hyperliquid_symbol == "BTC-PERP"
        assert btc.min_order_size == 0.5
        assert eth.min_order_size == 2.34


def test_enabled_mappings_cache_requires_explicit_clear() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        symbol_module.clear_symbol_mapping_cache()
        db.add(SymbolMapping(symbol="BTC", hyperliquid_symbol="BTC", mt5_symbol="BTCUSD", enabled=True))
        db.commit()

        first = symbol_module.enabled_mappings(db)
        db.add(SymbolMapping(symbol="ETH", hyperliquid_symbol="ETH", mt5_symbol="ETHUSD", enabled=True))
        db.commit()
        cached = symbol_module.enabled_mappings(db)
        symbol_module.clear_symbol_mapping_cache()
        refreshed = symbol_module.enabled_mappings(db)

        assert [row.symbol for row in first] == ["BTC"]
        assert [row.symbol for row in cached] == ["BTC"]
        assert [row.symbol for row in refreshed] == ["BTC", "ETH"]


def test_strategy_setting_cache_requires_explicit_clear() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        scanner_module.clear_strategy_setting_cache()
        db.add(StrategySetting(min_total_profit=1.0))
        db.commit()

        first = scanner_module.get_strategy_setting(db)
        row = db.query(StrategySetting).first()
        row.min_total_profit = 2.0
        db.commit()
        cached = scanner_module.get_strategy_setting(db)
        scanner_module.clear_strategy_setting_cache()
        refreshed = scanner_module.get_strategy_setting(db)

        assert first.min_total_profit == 1.0
        assert cached.min_total_profit == 1.0
        assert refreshed.min_total_profit == 2.0


def test_hedge_pool_loads_and_cas_groups() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        group = HedgeGroup(
            symbol="POOL",
            direction="long_hyperliquid_short_mt5",
            status="open",
            execution_mode="paper",
            notional=1000,
            quantity=1,
            mt5_quantity=1,
            hyperliquid_quantity=1,
            entry_spread=20,
            exit_target=2,
            opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(group)
        db.commit()

        assert hedge_pool.load_from_db(db) == 1
        first = hedge_pool.try_mark_closing(group.id, "close", 10)
        second = hedge_pool.try_mark_closing(group.id, "close again", 10)

        assert first is not None
        assert first.status == "closing"
        assert second is None
        closed = hedge_pool.mark_closed(group.id, realized_pnl=9, fees_delta=0.1, reason="done")
        assert closed is not None
        assert hedge_pool.get(group.id) is None


def test_hedge_pool_load_preserves_runtime_unrealized_pnl() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        group = HedgeGroup(
            symbol="POOL-PRESERVE",
            direction="long_hyperliquid_short_mt5",
            status="open",
            execution_mode="paper",
            notional=1000,
            quantity=1,
            mt5_quantity=1,
            hyperliquid_quantity=1,
            entry_spread=20,
            unrealized_pnl=0,
        )
        db.add(group)
        db.commit()
        hedge_pool.load_from_db(db)
        snapshot = hedge_pool.get(group.id)
        assert snapshot is not None
        hedge_pool.upsert_group(snapshot.with_updates(unrealized_pnl=12.5))

        assert hedge_pool.load_from_db(db) == 1

        reloaded = hedge_pool.get(group.id)
        assert reloaded is not None
        assert reloaded.unrealized_pnl == 12.5


def test_run_auto_close_uses_pool_without_hedge_group_query(monkeypatch) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    group = HedgeGroupSnapshot(
        id=999,
        symbol="POOL-AUTO",
        direction="long_hyperliquid_short_mt5",
        status="open",
        execution_mode="paper",
        notional=1000,
        quantity=1,
        mt5_quantity=1,
        hyperliquid_quantity=1,
        open_cost=0,
        fees=0,
        funding=0,
        swap=0,
        realized_pnl=0,
        unrealized_pnl=0,
        trigger_spread=20,
        entry_spread=20,
        entry_threshold=10,
        exit_target=2,
        overheat_threshold=0,
        close_reason="",
        opened_at=now,
        closed_at=None,
        source="test",
    )
    hedge_pool.upsert_group(group)
    quote_cache.put("hyperliquid", "POOL-AUTO", bid=100, ask=101, depth_notional=10000, source="test")
    quote_cache.put("mt5", "POOL-AUTO", bid=100, ask=101, depth_notional=10000, source="test")
    strategy = SimpleNamespace(
        auto_close_enabled=True,
        auto_close_live_enabled=False,
        auto_close_min_profit=0,
        max_holding_minutes=240,
        paper_hyperliquid_latency_ms_min=0,
        paper_hyperliquid_latency_ms_max=0,
        paper_mt5_latency_ms_min=0,
        paper_mt5_latency_ms_max=0,
    )
    mapping = SimpleNamespace(
        symbol="POOL-AUTO",
        hyperliquid_symbol="POOL-AUTO",
        mt5_symbol="POOL-AUTO",
        max_close_spread=2,
        allow_hold_through_mt5_close=True,
        execution_style="taker_taker",
        hl_close_order_type="market",
        mt5_close_order_type="market",
    )
    submitted = []

    class FakeDb:
        def query(self, *args, **kwargs):
            raise AssertionError("auto close hot path must not query HedgeGroup")

        def add(self, item):
            return None

        def commit(self):
            return None

        def rollback(self):
            return None

    class FakeGateway:
        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, f"{intent.platform}-pool", "filled", intent.quantity, 100.0, 0.0)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.0, 0.0)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.0, 0.0, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.auto_closer.get_strategy_setting", lambda db: strategy)
    monkeypatch.setattr("app.execution.auto_closer.enabled_mappings", lambda db: [mapping])
    monkeypatch.setattr("app.execution.auto_closer.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.auto_closer.build_execution_gateway", lambda adapter: FakeGateway())
    monkeypatch.setattr("app.execution.auto_closer.prune_table_by_id", lambda *args, **kwargs: None)

    closed = run_auto_close(FakeDb())

    assert closed == 1
    assert [intent.reduce_only for intent in submitted] == [True, True]
    assert hedge_pool.get(group.id) is None


def test_execution_maintenance_job_does_not_run_carry_cost(monkeypatch) -> None:
    from app.workers import scheduler as scheduler_module

    calls = []

    class FakeDb:
        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(scheduler_module, "SessionLocal", lambda: FakeDb())
    monkeypatch.setattr(scheduler_module, "run_auto_execute", lambda db: calls.append("auto_execute"))
    monkeypatch.setattr(scheduler_module, "run_auto_close", lambda db: calls.append("auto_close"))
    monkeypatch.setattr(scheduler_module, "run_execution_reconcile", lambda db: calls.append("reconcile"))
    monkeypatch.setattr(scheduler_module, "run_carry_cost_sync", lambda db: calls.append("carry_cost"))
    scheduler_module._running = False
    scheduler_module._execution_running = False

    scheduler_module.execution_maintenance_job()

    assert calls == ["auto_execute", "auto_close", "reconcile"]
