from datetime import datetime, timedelta
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analytics.spreads import SpreadPoint, downsample_spreads, summarize_spreads
from app.analytics.funding import FundingPoint, bucket_funding_points, summarize_funding
from app.analytics.lead_lag import lead_lag_report
from app.market import symbols as symbol_module
from app.db.models import Base, HedgeGroup, RiskSetting, StrategySetting, SymbolMapping
from app.execution.auto_closer import evaluate_auto_close
from app.market.mt5_sessions import MT5SessionState, mt5_action_allowed
from app.risk.engine import pre_trade_check
from app.market.quotes import QuoteCache, QuoteSynchronizer, quote_cache
from app.strategy.cost import estimate_cost
from app.strategy.live_costs import _estimate_mt5_swap_cost, _hyperliquid_effective_fee_rates
from app.strategy.statistical_signal import evaluate_entry_signal
from app.strategy.signals import evaluate_signal


def test_cost_model_positive_total() -> None:
    cost = estimate_cost(1000, 64990, 65010, 8)
    assert cost.total > 0
    assert cost.mt5_spread > 0


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
        assert signal.exit_target >= signal.cost_guard + strategy.auto_close_unit_profit_buffer


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
