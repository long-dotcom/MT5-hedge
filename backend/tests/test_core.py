from datetime import datetime, timedelta
import json
import time
from importlib import reload
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analytics.spreads import SpreadPoint, downsample_spreads, load_spread_points, summarize_spreads
from app.analytics.funding import FundingPoint, bucket_funding_points, summarize_funding
from app.analytics.lead_lag import lead_lag_report
from app.market import scanner as scanner_module
from app.market import symbols as symbol_module
from app.db.models import Alert, ArbitrageOpportunity, AuditLog, Base, Fill, HedgeGroup, HedgeGroupEvent, Order, Position, RiskSetting, SpreadCurrent, StrategySetting, SymbolMapping, SystemSetting, User
from app.execution.auto_closer import evaluate_auto_close, run_auto_close
from app.execution import gateway as gateway_module
from app.execution.engine import _has_position_effect, _is_pending_result, close_hedge_group, open_hedge_group
from app.execution.gateway import AdapterExecutionGateway, FillEvent, GatewayOrderResult, LegOrderIntent, OrderEvent, build_execution_gateway
from app.execution.nautilus_hyperliquid import NautilusHyperliquidGateway, NautilusSubmitResult, NautilusTradingNodeSubmitter, hyperliquid_instrument_id
from app.execution import nautilus_hyperliquid as nautilus_module
from app.execution.readiness import live_execution_readiness
from app.execution.reconciler import reconcile_hedge_group, reconcile_orphan_positions, reconcile_residual_positions, sync_live_positions
from app.config.settings import HYPERLIQUID_MAINNET_INFO_URL, HYPERLIQUID_TESTNET_INFO_URL, hyperliquid_execution_info_url
from app.api import router as api_router
from app.market.mt5_sessions import MT5SessionState, mt5_action_allowed
from app.market.nautilus_hyperliquid import market_symbols_from_mappings, write_all_dexs_asset_ctxs_to_quote_cache, write_cached_order_book_to_quote_cache, write_depth_to_quote_cache, write_quote_tick_to_quote_cache
from app.market.scan_state import scan_state_store
from app.risk.engine import pre_trade_check
from app.market.quotes import QuoteCache, QuoteSynchronizer, quote_cache
from app.adapters.paper import PaperAdapter
from app.adapters.base import AdapterOrder, AdapterOrderResult
from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.mt5 import MT5Adapter
from app.schemas import AdoptPositionIn
from app.schemas import SymbolMappingIn
from app.strategy.cost import estimate_cost
from app.strategy.live_costs import _estimate_mt5_swap_cost, _hyperliquid_effective_fee_rates
from app.strategy.statistical_signal import evaluate_entry_signal
from app.strategy.signals import evaluate_signal
from app.workers.market_data import MarketDataManager, _exchange_time_from_hyperliquid_ms, hyperliquid_symbol_map, l2book_subscription


def test_cost_model_positive_total() -> None:
    cost = estimate_cost(1000, 64990, 65010, 8)
    assert cost.total > 0
    assert cost.mt5_spread > 0


def test_relative_sqlite_database_url_resolves_from_project_root(monkeypatch) -> None:
    import app.db.session as session_module

    monkeypatch.setattr(session_module, "get_settings", lambda: type("Settings", (), {"database_url": "sqlite:///data/mt5_hedge.db"})())
    reloaded = reload(session_module)
    expected = (reloaded.ROOT_DIR / "data" / "mt5_hedge.db").as_posix()
    assert expected in str(reloaded.engine.url).replace("\\", "/")


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
        decision = pre_trade_check(db, "BTC", 1000, 1, datetime.utcnow())
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


def test_nautilus_depth_bridge_writes_hyperliquid_quote_cache() -> None:
    cache = QuoteCache()
    depth = SimpleNamespace(
        instrument_id="BTC-USD-PERP.HYPERLIQUID",
        bids=[SimpleNamespace(price=100.0, size=2.0)],
        asks=[SimpleNamespace(price=101.0, size=3.0)],
        ts_event=1_700_000_000_000_000_000,
    )

    ok = write_depth_to_quote_cache(depth, {"BTC-USD-PERP.HYPERLIQUID": "BTC"}, cache)

    quote = cache.latest("hyperliquid", "BTC")
    assert ok
    assert quote is not None
    assert quote.bid == 100
    assert quote.ask == 101
    assert quote.depth_notional == 200
    assert quote.source == "nautilus_order_book_depth"


def test_nautilus_quote_tick_bridge_writes_hyperliquid_quote_cache() -> None:
    cache = QuoteCache()
    tick = SimpleNamespace(
        instrument_id="ETH-USD-PERP.HYPERLIQUID",
        bid_price=2000.0,
        ask_price=2001.0,
        bid_size=0.5,
        ask_size=0.25,
    )

    ok = write_quote_tick_to_quote_cache(tick, {"ETH-USD-PERP.HYPERLIQUID": "ETH"}, cache)

    quote = cache.latest("hyperliquid", "ETH")
    assert ok
    assert quote is not None
    assert quote.bid == 2000
    assert quote.ask == 2001
    assert quote.depth_notional == 500.25
    assert quote.source == "nautilus_quote_tick"


def test_nautilus_managed_book_bridge_writes_hyperliquid_quote_cache() -> None:
    cache = QuoteCache()
    event = SimpleNamespace(instrument_id="BTC-USD-PERP.HYPERLIQUID", ts_event=1_700_000_000_000_000_000)

    class FakeNautilusCache:
        def order_book(self, instrument_id):
            assert str(instrument_id) == "BTC-USD-PERP.HYPERLIQUID"
            return SimpleNamespace(
                best_bid_price=lambda: 100.0,
                best_ask_price=lambda: 101.0,
                best_bid_size=lambda: 4.0,
                best_ask_size=lambda: 2.0,
            )

    ok = write_cached_order_book_to_quote_cache(event, {"BTC-USD-PERP.HYPERLIQUID": "BTC"}, FakeNautilusCache(), cache)

    quote = cache.latest("hyperliquid", "BTC")
    assert ok
    assert quote is not None
    assert quote.bid == 100
    assert quote.ask == 101
    assert quote.depth_notional == 202
    assert quote.source == "nautilus_order_book_deltas"


def test_nautilus_all_dexs_asset_ctxs_bridge_writes_hyperliquid_quote_cache() -> None:
    cache = QuoteCache()
    payload = SimpleNamespace(
        entries=[
            SimpleNamespace(
                instrument_id="xyz:JP225-USD-PERP.HYPERLIQUID",
                impact_prices=SimpleNamespace(bid=39000.0, ask=39002.0),
                open_interest=10.0,
            ),
            SimpleNamespace(
                instrument_id="xyz:SP500-USD-PERP.HYPERLIQUID",
                impact_prices=SimpleNamespace(bid=5500.0, ask=5501.0),
                open_interest=1.0,
            ),
        ],
        ts_event=1_700_000_000_000_000_000,
    )

    updated = write_all_dexs_asset_ctxs_to_quote_cache(
        payload,
        {"xyz:JP225-USD-PERP.HYPERLIQUID": "JP225"},
        cache,
    )

    quote = cache.latest("hyperliquid", "JP225")
    assert updated == 1
    assert quote is not None
    assert quote.bid == 39000
    assert quote.ask == 39002
    assert quote.depth_notional == 390010
    assert quote.source == "nautilus_all_dexs_asset_ctxs"


def test_nautilus_all_dexs_does_not_override_fresh_fast_l2book_quote() -> None:
    cache = QuoteCache()
    cache.put("hyperliquid", "JP225", 72301, 72499, 36.97, "hyperliquid_l2Book_fast")
    payload = SimpleNamespace(
        entries=[
            SimpleNamespace(
                instrument_id="xyz:JP225-USD-PERP.HYPERLIQUID",
                impact_prices=SimpleNamespace(bid=39000.0, ask=39002.0),
                open_interest=10.0,
            ),
        ],
        ts_event=1_700_000_000_000_000_000,
    )

    updated = write_all_dexs_asset_ctxs_to_quote_cache(
        payload,
        {"xyz:JP225-USD-PERP.HYPERLIQUID": "JP225"},
        cache,
    )

    quote = cache.latest("hyperliquid", "JP225")
    assert updated == 0
    assert quote is not None
    assert quote.bid == 72301
    assert quote.source == "hyperliquid_l2Book_fast"


def test_nautilus_market_symbols_use_hyperliquid_instrument_ids() -> None:
    symbols = market_symbols_from_mappings(
        [
            SimpleNamespace(symbol="BTC", hyperliquid_symbol="BTC"),
            SimpleNamespace(symbol="JP225", hyperliquid_symbol="xyz:JP225"),
        ]
    )

    assert symbols[0].instrument_id == "BTC-USD-PERP.HYPERLIQUID"
    assert symbols[1].instrument_id == "xyz:JP225-USD-PERP.HYPERLIQUID"


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
        nautilus_hyperliquid_vault_address="",
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
        nautilus_hyperliquid_vault_address="",
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


def test_hyperliquid_execution_info_url_follows_nautilus_environment() -> None:
    settings = SimpleNamespace(
        hyperliquid_info_url=HYPERLIQUID_MAINNET_INFO_URL,
        nautilus_hyperliquid_environment="testnet",
    )

    assert hyperliquid_execution_info_url(settings) == HYPERLIQUID_TESTNET_INFO_URL


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
        nautilus_hyperliquid_vault_address="",
        hyperliquid_info_url=HYPERLIQUID_MAINNET_INFO_URL,
        nautilus_hyperliquid_environment="testnet",
    )

    assert adapter.get_positions() == []
    assert urls == [HYPERLIQUID_TESTNET_INFO_URL, HYPERLIQUID_TESTNET_INFO_URL]


def test_nautilus_hyperliquid_symbol_mapping() -> None:
    assert hyperliquid_instrument_id("BTC") == "BTC-USD-PERP.HYPERLIQUID"
    assert hyperliquid_instrument_id("xyz:JP225") == "xyz:JP225-USD-PERP.HYPERLIQUID"
    assert hyperliquid_instrument_id("HYPE-USDC-SPOT") == "HYPE-USDC-SPOT.HYPERLIQUID"
    assert hyperliquid_instrument_id("ETH-USD-PERP.HYPERLIQUID") == "ETH-USD-PERP.HYPERLIQUID"


def test_symbol_mapping_rejects_mt5_style_hyperliquid_symbol() -> None:
    with pytest.raises(ValueError, match="Hyperliquid 标准永续"):
        SymbolMappingIn(symbol="BTC", hyperliquid_symbol="BTCUSD", mt5_symbol="BTCUSD")


def test_nautilus_hyperliquid_gateway_maps_submit_result() -> None:
    class FakeSubmitter:
        def submit_order(self, intent, instrument_id):
            assert instrument_id == "BTC-USD-PERP.HYPERLIQUID"
            return NautilusSubmitResult(status="filled", external_order_id="nt-1", filled_quantity=0.01, average_price=65000, fee=0.1)

        def cancel_order(self, external_order_id):
            return True

        def query_order(self, external_order_id):
            return {"status": "filled"}

    gateway = NautilusHyperliquidGateway(submitter=FakeSubmitter())
    result = gateway.submit_order(LegOrderIntent(platform="hyperliquid", symbol="BTC", side="buy", quantity=0.01))
    assert result.success
    assert result.order_event.external_order_id == "nt-1"
    assert len(result.fill_events) == 1
    assert result.adapter_result.average_price == 65000


def test_nautilus_submitter_requires_live_submit_switch() -> None:
    settings = SimpleNamespace(nautilus_hyperliquid_submit_enabled=False)
    submitter = NautilusTradingNodeSubmitter(settings)
    result = submitter.submit_order(LegOrderIntent(platform="hyperliquid", symbol="BTC", side="buy", quantity=0.01), "BTC-USD-PERP.HYPERLIQUID")
    assert not result.success
    assert "开关未开启" in result.message


def test_nautilus_reduce_only_type_error_is_detected() -> None:
    assert nautilus_module._type_error_mentions_keyword(TypeError("market() got an unexpected keyword argument 'reduce_only'"), "reduce_only")
    assert not nautilus_module._type_error_mentions_keyword(TypeError("market() got an unexpected keyword argument 'client_order_id'"), "reduce_only")


def test_nautilus_submitter_delegates_to_bridge_strategy(monkeypatch) -> None:
    class FakeNode:
        pass

    class FakeStrategy:
        def __init__(self) -> None:
            self.calls = []

        def submit_intent(self, intent, instrument_id, timeout_seconds):
            self.calls.append((intent, instrument_id, timeout_seconds))
            return NautilusSubmitResult(status="accepted", external_order_id="nt-live-1")

        def cancel_external_order(self, external_order_id):
            return external_order_id == "nt-live-1"

        def query_external_order(self, external_order_id):
            return {"status": "accepted", "external_order_id": external_order_id}

    fake_strategy = FakeStrategy()
    settings = SimpleNamespace(nautilus_hyperliquid_submit_enabled=True, nautilus_hyperliquid_order_timeout_seconds=3.0)
    submitter = NautilusTradingNodeSubmitter(settings)
    monkeypatch.setattr(submitter, "_build_node", lambda: (FakeNode(), fake_strategy))

    intent = LegOrderIntent(platform="hyperliquid", symbol="BTC", side="sell", quantity=0.02, order_type="market")
    result = submitter.submit_order(intent, "BTC-USD-PERP.HYPERLIQUID")

    assert result.success
    assert result.external_order_id == "nt-live-1"
    assert fake_strategy.calls == [(intent, "BTC-USD-PERP.HYPERLIQUID", 3.0)]
    assert submitter.cancel_order("nt-live-1")
    assert submitter.query_order("nt-live-1")["status"] == "accepted"


def test_nautilus_build_node_passes_account_address(monkeypatch) -> None:
    captured = {}

    class FakeConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeExecConfig(FakeConfig):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured["exec_config"] = kwargs

    class FakeEnum:
        MAINNET = "mainnet"
        TESTNET = "testnet"
        PERP = "perp"
        PERP_HIP3 = "perp_hip3"

    class FakeTrader:
        def add_strategy(self, strategy):
            captured["strategy"] = strategy

    class FakeNode:
        def __init__(self, config):
            captured["node_config"] = config
            self.trader = FakeTrader()

        def add_data_client_factory(self, venue, factory):
            captured["data_factory"] = (venue, factory)

        def add_exec_client_factory(self, venue, factory):
            captured["exec_factory"] = (venue, factory)

        def build(self):
            captured["built"] = True

        def is_running(self):
            return False

        def run_async(self):
            captured["running"] = True

    import nautilus_trader.adapters.hyperliquid as hyperliquid_mod
    import nautilus_trader.config as config_mod
    import nautilus_trader.core.nautilus_pyo3 as pyo3_mod
    import nautilus_trader.live.node as node_mod
    import nautilus_trader.model.identifiers as identifiers_mod

    monkeypatch.setattr(hyperliquid_mod, "HYPERLIQUID", "HYPERLIQUID")
    monkeypatch.setattr(hyperliquid_mod, "HyperliquidDataClientConfig", FakeConfig)
    monkeypatch.setattr(hyperliquid_mod, "HyperliquidExecClientConfig", FakeExecConfig)
    monkeypatch.setattr(hyperliquid_mod, "HyperliquidLiveDataClientFactory", "data_factory")
    monkeypatch.setattr(hyperliquid_mod, "HyperliquidLiveExecClientFactory", "exec_factory")
    monkeypatch.setattr(hyperliquid_mod, "HyperliquidProductType", FakeEnum)
    monkeypatch.setattr(config_mod, "InstrumentProviderConfig", FakeConfig)
    monkeypatch.setattr(config_mod, "TradingNodeConfig", FakeConfig)
    monkeypatch.setattr(pyo3_mod, "HyperliquidEnvironment", FakeEnum)
    monkeypatch.setattr(node_mod, "TradingNode", FakeNode)
    monkeypatch.setattr(identifiers_mod, "TraderId", lambda value: f"trader:{value}")

    settings = SimpleNamespace(
        nautilus_trader_id="MT5-HEDGE-TEST",
        nautilus_hyperliquid_environment="testnet",
        nautilus_hyperliquid_product_types="PERP,PERP_HIP3",
        nautilus_hyperliquid_private_key="agent-secret",
        nautilus_hyperliquid_vault_address="",
        hyperliquid_account_address="0xmain",
    )
    submitter = NautilusTradingNodeSubmitter(settings)

    node, strategy = submitter._build_node()

    assert isinstance(node, FakeNode)
    assert strategy is captured["strategy"]
    assert captured["exec_config"]["private_key"] == "agent-secret"
    assert captured["exec_config"]["account_address"] == "0xmain"
    assert captured["exec_config"]["product_types"] == ("perp", "perp_hip3")
    assert captured["built"] is True
    assert captured["running"] is True


def test_nautilus_submitter_queries_hyperliquid_when_local_cache_missing(monkeypatch) -> None:
    class FakeStrategy:
        def query_external_order(self, external_order_id):
            return {"status": "not_found", "external_order_id": external_order_id}

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    calls = []

    def fake_urlopen(req, timeout):
        calls.append(json.loads(req.data.decode("utf-8")))
        payload = {"order": {"status": "open", "order": {"oid": 12345}}}
        return FakeResponse(payload)

    settings = SimpleNamespace(
        hyperliquid_account_address="0xabc",
        nautilus_hyperliquid_vault_address="",
        hyperliquid_info_url="https://example.test/info",
    )
    submitter = NautilusTradingNodeSubmitter(settings)
    submitter._strategy = FakeStrategy()
    monkeypatch.setattr(nautilus_module.request, "urlopen", fake_urlopen)

    result = submitter.query_order("12345")

    assert result["status"] == "accepted"
    assert calls == [{"type": "orderStatus", "user": "0xabc", "oid": 12345}]


def test_hyperliquid_order_status_backfills_fill_details(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(req, timeout):
        payload = json.loads(req.data.decode("utf-8"))
        if payload["type"] == "orderStatus":
            return FakeResponse({"order": {"status": "filled", "order": {"oid": 12345}}})
        return FakeResponse(
            [
                {"oid": 12345, "sz": "0.01", "px": "65000", "fee": "0.1"},
                {"oid": 12345, "sz": "0.02", "px": "65100", "fee": "0.2"},
                {"oid": 99999, "sz": "1", "px": "1", "fee": "1"},
            ]
        )

    settings = SimpleNamespace(
        hyperliquid_account_address="0xabc",
        nautilus_hyperliquid_vault_address="",
        hyperliquid_info_url="https://example.test/info",
    )
    monkeypatch.setattr(nautilus_module.request, "urlopen", fake_urlopen)

    result = nautilus_module._query_hyperliquid_order_status(settings, "12345")

    assert result["status"] == "filled"
    assert result["filled_quantity"] == pytest.approx(0.03)
    assert result["average_price"] == pytest.approx((0.01 * 65000 + 0.02 * 65100) / 0.03)
    assert result["fee"] == pytest.approx(0.3)


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

    class FakeGateway:
        def submit_order(self, intent, *, paper_latency_ms=0):
            result = AdapterOrderResult(True, f"{intent.platform}-accepted", "accepted", 0.0, 0.0, 0.0)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "accepted", result.external_order_id, intent.quantity, 0.0, 0.0, 0.0)
            return GatewayOrderResult(True, event, (), result)

    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway())
    monkeypatch.setattr("app.execution.engine.live_execution_readiness", lambda db: {"checks": []})
    group = close_hedge_group(db, group_id, "manual close")

    assert group.status == "closing"
    assert "待成交" in group.close_reason
    assert db.query(Order).filter(Order.hedge_group_id == group_id, Order.status == "accepted").count() == 2
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
        lambda db: {"checks": [{"component": "nautilus_hyperliquid_submit_enabled", "status": "block", "message": "submit 未开启"}]},
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
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: (SimpleNamespace(hyperliquid=SimpleNamespace(local_recv_ts=datetime.utcnow()), time_diff_ms=0), ""))
    monkeypatch.setattr("app.execution.engine.pre_trade_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, reason=""))

    group = open_hedge_group(db, opportunity.id)

    assert group.status == "open"
    assert [intent.reduce_only for intent in submitted] == [False, False]
    assert {order.reduce_only for order in db.query(Order).filter(Order.hedge_group_id == group.id).all()} == {False}


def test_auto_close_skips_live_group_without_live_switch(monkeypatch) -> None:
    db, group_id = _live_close_test_db(auto_close_live_enabled=True, live_trading_enabled=False)
    _seed_auto_close_quotes()
    called = []
    monkeypatch.setattr("app.execution.auto_closer.close_hedge_group", lambda *args, **kwargs: called.append(args))

    closed = run_auto_close(db)
    group = db.get(HedgeGroup, group_id)

    assert closed == 0
    assert called == []
    assert group.status == "open"


def test_auto_close_live_group_submits_reverse_orders(monkeypatch) -> None:
    db, group_id = _live_close_test_db(auto_close_live_enabled=True, live_trading_enabled=True)
    _seed_auto_close_quotes()
    submitted = []

    class FakeGateway:
        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, f"{intent.platform}-auto-close", "filled", intent.quantity, 100.0, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.0, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.0, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway())
    monkeypatch.setattr("app.execution.engine.live_execution_readiness", lambda db: {"checks": []})

    closed = run_auto_close(db)
    group = db.get(HedgeGroup, group_id)

    assert closed == 1
    assert group.status == "closed"
    assert group.close_reason.startswith("auto_live:")
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
    old_time = datetime.utcnow() - timedelta(seconds=30)
    for order in db.query(Order).filter(Order.hedge_group_id == group.id).all():
        order.created_at = old_time
    db.commit()

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            return {"status": "not_ready", "external_order_id": external_order_id, "message": "Nautilus cache 不包含该订单"}

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
        opened_at=datetime.utcnow(),
        closed_at=datetime.utcnow(),
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
        nautilus_hyperliquid_vault_address="",
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
        opened_at=datetime.utcnow(),
        closed_at=datetime.utcnow(),
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
            opened_at=datetime.utcnow(),
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
        opened_at=datetime.utcnow(),
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
        opened_at=datetime.utcnow() if status == "closing" else None,
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
        opened_at=datetime.utcnow(),
    )
    db.add(group)
    db.commit()
    return db, group.id


def _seed_auto_close_quotes() -> None:
    quote_cache.put("hyperliquid", "OIL", bid=100.0, ask=101.0, depth_notional=10000, source="test")
    quote_cache.put("mt5", "OIL", bid=100.5, ask=101.0, depth_notional=10000, source="test")


def test_gateway_factory_uses_nautilus_for_enabled_hyperliquid(monkeypatch) -> None:
    settings = type("Settings", (), {"nautilus_hyperliquid_enabled": True})()
    monkeypatch.setattr(gateway_module, "get_settings", lambda: settings)
    built = build_execution_gateway(PaperAdapter("hyperliquid"))
    assert isinstance(built, NautilusHyperliquidGateway)
    fallback = build_execution_gateway(PaperAdapter("mt5"))
    assert isinstance(fallback, AdapterExecutionGateway)


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
                "created_at": datetime.utcnow(),
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
    now = datetime.utcnow()
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
    now = datetime.utcnow()
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
    now = datetime.utcnow()
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
    now = datetime.utcnow()
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
    now = datetime.utcnow()
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


def test_statistical_exit_target_uses_low_percentile_and_profit_buffer() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.utcnow()
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
    now = datetime.utcnow()
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
            opened_at=datetime.utcnow(),
        )
        db.add(group)
        db.commit()
        quote_cache.put("hyperliquid", "JP225", 71330, 71331, 10000, "test")
        quote_cache.put("mt5", "JP225", 71490, 71495, 10000, "test")
        evaluation = evaluate_auto_close(db, strategy, group)
        assert evaluation.should_close
        assert evaluation.close_spread == 165
        assert evaluation.estimated_profit == 75


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
        nautilus_hyperliquid_enabled=False,
        nautilus_hyperliquid_submit_enabled=False,
        nautilus_hyperliquid_private_key="",
        hyperliquid_account_address="",
        nautilus_hyperliquid_vault_address="",
        mt5_live_order_enabled=False,
        mt5_login="",
        mt5_server="",
    )
    monkeypatch.setattr("app.execution.readiness.import_module", fake_import)

    result = live_execution_readiness(db, settings)

    assert result["status"] == "blocked"
    blocked = {item["component"] for item in result["checks"] if item["status"] == "block"}
    assert {"global_live_switch", "nautilus_hyperliquid_enabled", "nautilus_hyperliquid_private_key", "metatrader5_import"} <= blocked


def test_live_execution_readiness_allows_ready_with_complete_config(monkeypatch) -> None:
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
        nautilus_hyperliquid_enabled=True,
        nautilus_hyperliquid_submit_enabled=True,
        nautilus_hyperliquid_private_key="secret",
        hyperliquid_account_address="0xabc",
        nautilus_hyperliquid_vault_address="",
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

    assert result["status"] == "ready"
    assert result["ready"] is True


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
        nautilus_hyperliquid_enabled=True,
        nautilus_hyperliquid_submit_enabled=True,
        nautilus_hyperliquid_private_key="secret",
        hyperliquid_account_address="0xabc",
        nautilus_hyperliquid_vault_address="",
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
        nautilus_hyperliquid_enabled=True,
        nautilus_hyperliquid_submit_enabled=True,
        nautilus_hyperliquid_private_key="secret",
        hyperliquid_account_address="0xabc",
        nautilus_hyperliquid_vault_address="",
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
        nautilus_hyperliquid_enabled=True,
        nautilus_hyperliquid_submit_enabled=True,
        nautilus_hyperliquid_private_key="secret",
        hyperliquid_account_address="0xabc",
        nautilus_hyperliquid_vault_address="",
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
        nautilus_hyperliquid_enabled=True,
        nautilus_hyperliquid_submit_enabled=True,
        nautilus_hyperliquid_private_key="secret",
        hyperliquid_account_address="0xabc",
        nautilus_hyperliquid_vault_address="",
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

    assert result == {"status": "ok", "changed": 3}
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
