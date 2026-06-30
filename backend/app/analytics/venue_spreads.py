from __future__ import annotations

import math
from datetime import datetime
from statistics import mean, median, pstdev
from typing import Any

from sqlalchemy.orm import Session

from app.analytics.spreads import RANGE_SECONDS, TARGET_POINTS, parse_range
from app.db.models import SpreadSnapshot
from app.market.quotes import quote_cache


def load_venue_spread_series(
    db: Session, symbol: str, range_value: str, range_key: str | None = None
) -> list[dict[str, Any]]:
    """Load per-venue bid-ask spread time-series for *symbol*.

    *range_key* is the normalised range identifier (e.g. ``"1h"``).
    When provided it is used for the quote-cache short-range check;
    otherwise the value is derived from *range_value* via :func:`parse_range`.
    """
    key, start_at, seconds = parse_range(range_value)
    effective_key = range_key or key

    # For short ranges try live quote_cache first
    if effective_key in ("15m", "1h"):
        series = _series_from_quote_cache(symbol, start_at)
        if len(series) >= 30:
            return series

    # Fallback / primary source: SpreadSnapshot table
    return _series_from_db(db, symbol, start_at)


def _series_from_quote_cache(
    symbol: str, start_at: datetime
) -> list[dict[str, Any]]:
    hl_quotes = quote_cache.history("hyperliquid", symbol)
    mt5_quotes = quote_cache.history("mt5", symbol)

    hl_quotes = [q for q in hl_quotes if q.local_recv_ts >= start_at]
    mt5_quotes = [q for q in mt5_quotes if q.local_recv_ts >= start_at]

    if not hl_quotes or not mt5_quotes:
        return []

    # Build a time-aligned series by matching hl/mt5 quotes within the same second
    mt5_by_ts: dict[int, list[Any]] = {}
    for q in mt5_quotes:
        bucket = int(q.local_recv_ts.timestamp())
        mt5_by_ts.setdefault(bucket, []).append(q)

    series: list[dict[str, Any]] = []
    for hl_q in hl_quotes:
        ts_bucket = int(hl_q.local_recv_ts.timestamp())
        mt5_candidates = mt5_by_ts.get(ts_bucket)
        if not mt5_candidates:
            # try neighbouring buckets (±1s)
            for delta in (-1, 1):
                mt5_candidates = mt5_by_ts.get(ts_bucket + delta)
                if mt5_candidates:
                    break
        if not mt5_candidates:
            continue
        mt5_q = mt5_candidates[-1]  # latest in bucket
        series.append(
            {
                "time": hl_q.local_recv_ts.isoformat(),
                "hl_spread": hl_q.ask - hl_q.bid,
                "mt5_spread": mt5_q.ask - mt5_q.bid,
            }
        )
    return series


def _series_from_db(
    db: Session, symbol: str, start_at: datetime
) -> list[dict[str, Any]]:
    rows = (
        db.query(SpreadSnapshot)
        .filter(
            SpreadSnapshot.symbol == symbol.upper(),
            SpreadSnapshot.created_at >= start_at,
        )
        .order_by(SpreadSnapshot.created_at)
        .all()
    )
    return [
        {
            "time": row.created_at.isoformat(),
            "hl_spread": float(row.hyperliquid_ask) - float(row.hyperliquid_bid),
            "mt5_spread": float(row.mt5_ask) - float(row.mt5_bid),
        }
        for row in rows
    ]


def summarize_venue_spreads(values: list[float]) -> dict[str, Any]:
    """Compute statistical summary for a list of spread values."""
    n = len(values)
    if n == 0:
        return {
            "current": 0.0,
            "mean": 0.0,
            "std": 0.0,
            "min": 0.0,
            "max": 0.0,
            "median": 0.0,
            "p95": 0.0,
            "cv": 0.0,
            "anomaly_pct": 0.0,
            "sample_count": 0,
        }

    current = values[-1]
    avg = mean(values)
    std = pstdev(values) if n > 1 else 0.0
    sorted_vals = sorted(values)
    med = median(sorted_vals)
    p95_index = min(int(math.ceil(0.95 * n)) - 1, n - 1)
    p95 = sorted_vals[p95_index]
    cv = std / avg if avg != 0 else 0.0

    anomaly_count = 0
    if std > 0:
        threshold = 3 * std
        anomaly_count = sum(1 for v in values if abs(v - avg) > threshold)
    anomaly_pct = anomaly_count / n * 100

    return {
        "current": current,
        "mean": round(avg, 6),
        "std": round(std, 6),
        "min": sorted_vals[0],
        "max": sorted_vals[-1],
        "median": round(med, 6),
        "p95": round(p95, 6),
        "cv": round(cv, 6),
        "anomaly_pct": round(anomaly_pct, 2),
        "sample_count": n,
    }


def downsample_venue_spreads(
    series: list[dict[str, Any]], range_value: str
) -> list[dict[str, Any]]:
    """Reduce series to ~TARGET_POINTS using bucket aggregation."""
    if not series:
        return []

    key = range_value if range_value in TARGET_POINTS else "1h"
    target = TARGET_POINTS[key]
    seconds = RANGE_SECONDS[key]
    bucket_seconds = max(1, math.ceil(seconds / target))

    start_ts = datetime.fromisoformat(series[0]["time"]).timestamp()
    buckets: dict[int, list[dict[str, Any]]] = {}
    for point in series:
        ts = datetime.fromisoformat(point["time"]).timestamp()
        idx = int((ts - start_ts) // bucket_seconds)
        buckets.setdefault(idx, []).append(point)

    result: list[dict[str, Any]] = []
    for idx in sorted(buckets):
        bucket = buckets[idx]
        hl_vals = [p["hl_spread"] for p in bucket]
        mt5_vals = [p["mt5_spread"] for p in bucket]
        result.append(
            {
                "time": bucket[-1]["time"],
                "hl_open": hl_vals[0],
                "hl_close": hl_vals[-1],
                "hl_high": max(hl_vals),
                "hl_low": min(hl_vals),
                "hl_avg": round(mean(hl_vals), 6),
                "mt5_open": mt5_vals[0],
                "mt5_close": mt5_vals[-1],
                "mt5_high": max(mt5_vals),
                "mt5_low": min(mt5_vals),
                "mt5_avg": round(mean(mt5_vals), 6),
                "count": len(bucket),
            }
        )
    return result


def venue_spread_report(
    db: Session, symbol: str, range_value: str, range_key: str | None = None
) -> dict[str, Any]:
    """Main entry: build the full venue-spreads report.

    *range_key* is forwarded to :func:`load_venue_spread_series` so that
    callers who have already normalised the range string can avoid
    re-parsing.
    """
    series = load_venue_spread_series(db, symbol, range_value, range_key=range_key)

    hl_values = [p["hl_spread"] for p in series]
    mt5_values = [p["mt5_spread"] for p in series]

    return {
        "symbol": symbol,
        "range": range_value,
        "summary": {
            "hl": summarize_venue_spreads(hl_values),
            "mt5": summarize_venue_spreads(mt5_values),
        },
        "series": downsample_venue_spreads(series, range_value),
    }
