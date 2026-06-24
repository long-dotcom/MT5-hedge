from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean, median, pstdev
from typing import Iterable

from sqlalchemy.orm import Session

from app.db.models import SpreadBucket, SpreadSnapshot


RANGE_SECONDS = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
}

TARGET_POINTS = {
    "15m": 900,
    "1h": 720,
    "4h": 960,
    "24h": 1440,
    "7d": 2016,
}

REVERSION_HORIZONS = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "60m": 60 * 60,
}

RAW_POINT_RANGES = {"15m", "1h", "4h"}


@dataclass(frozen=True)
class SpreadPoint:
    created_at: datetime
    spread: float
    total_cost: float
    net_profit: float


def parse_range(range_value: str) -> tuple[str, datetime, int]:
    key = range_value if range_value in RANGE_SECONDS else "1h"
    seconds = RANGE_SECONDS[key]
    return key, datetime.utcnow() - timedelta(seconds=seconds), seconds


def load_spread_points(db: Session, symbol: str, direction: str, range_value: str, basis: str = "entry") -> list[SpreadPoint]:
    range_key, start_at, _ = parse_range(range_value)
    if range_key in RAW_POINT_RANGES:
        snapshot_points = _load_snapshot_points(db, symbol, direction, start_at, basis)
        return snapshot_points or _load_bucket_points(db, symbol, direction, start_at, basis)
    bucket_points = _load_bucket_points(db, symbol, direction, start_at, basis)
    return bucket_points or _load_snapshot_points(db, symbol, direction, start_at, basis)


def _load_snapshot_points(db: Session, symbol: str, direction: str, start_at: datetime, basis: str) -> list[SpreadPoint]:
    rows = (
        db.query(SpreadSnapshot)
        .filter(
            SpreadSnapshot.symbol == symbol.upper(),
            SpreadSnapshot.direction == direction,
            SpreadSnapshot.created_at >= start_at,
        )
        .order_by(SpreadSnapshot.created_at)
        .all()
    )
    return [
        SpreadPoint(
            created_at=row.created_at,
            spread=float(_snapshot_spread(row, basis)),
            total_cost=float(row.unit_cost),
            net_profit=float(row.unit_net_profit),
        )
        for row in rows
    ]


def _load_bucket_points(db: Session, symbol: str, direction: str, start_at: datetime, basis: str) -> list[SpreadPoint]:
    bucket_rows = (
        db.query(SpreadBucket)
        .filter(
            SpreadBucket.symbol == symbol.upper(),
            SpreadBucket.direction == direction,
            SpreadBucket.bucket_start >= start_at,
        )
        .order_by(SpreadBucket.bucket_start)
        .all()
    )
    if bucket_rows:
        return [
            SpreadPoint(
                created_at=row.bucket_start,
                spread=float(_bucket_spread(row, basis)),
                total_cost=float(row.avg_unit_cost),
                net_profit=float(row.avg_unit_net_profit),
            )
            for row in bucket_rows
        ]
    return []


def _snapshot_spread(row: SpreadSnapshot, basis: str) -> float:
    if basis == "close":
        return float(getattr(row, "close_spread", 0.0) or row.gross_spread)
    if basis == "mid":
        return float(getattr(row, "mid_spread", 0.0) or row.gross_spread)
    return float(getattr(row, "entry_spread", 0.0) or row.gross_spread)


def _bucket_spread(row: SpreadBucket, basis: str) -> float:
    if basis == "close":
        return float(getattr(row, "avg_close_basis_spread", 0.0) or row.avg_spread)
    if basis == "mid":
        return float(getattr(row, "avg_mid_spread", 0.0) or row.avg_spread)
    return float(getattr(row, "avg_entry_spread", 0.0) or row.avg_spread)


def summarize_spreads(points: list[SpreadPoint], range_value: str) -> dict[str, object]:
    spreads = [point.spread for point in points]
    sample_count = len(spreads)
    if sample_count == 0:
        return _empty_summary(range_value)

    current = spreads[-1]
    avg = mean(spreads)
    std = pstdev(spreads) if sample_count > 1 else 0.0
    z_score = (current - avg) / std if std > 0 else 0.0
    percentile = sum(1 for value in spreads if value <= current) / sample_count
    cost_line = mean(point.total_cost for point in points)
    half_life = estimate_half_life_seconds(points, avg)
    reversion_probability = estimate_reversion_probabilities(points, current, avg)
    max_adverse_spread = estimate_max_adverse_spread(spreads, current, avg)
    status, reason = classify_analytics(sample_count, z_score, half_life, reversion_probability.get("15m"))

    return {
        "range": range_value,
        "sample_count": sample_count,
        "current_spread": current,
        "mean": avg,
        "std": std,
        "z_score": z_score,
        "percentile": percentile,
        "half_life_seconds": half_life,
        "reversion_probability": reversion_probability,
        "max_adverse_spread": max_adverse_spread,
        "avg_total_cost": cost_line,
        "analytics_status": status,
        "reason": reason,
        "opportunity_score": score_opportunity(z_score, reversion_probability.get("15m"), half_life, sample_count),
    }


def downsample_spreads(points: list[SpreadPoint], range_value: str) -> list[dict[str, float | str | int]]:
    range_key, _, seconds = parse_range(range_value)
    if not points:
        return []
    target = TARGET_POINTS[range_key]
    bucket_seconds = max(1, math.ceil(seconds / target))
    buckets: dict[int, list[SpreadPoint]] = {}
    start_ts = points[0].created_at.timestamp()
    for point in points:
        bucket_index = int((point.created_at.timestamp() - start_ts) // bucket_seconds)
        buckets.setdefault(bucket_index, []).append(point)

    result = []
    for bucket_index in sorted(buckets):
        bucket = buckets[bucket_index]
        values = [point.spread for point in bucket]
        result.append(
            {
                "time": bucket[-1].created_at.isoformat(),
                "open": values[0],
                "high": max(values),
                "low": min(values),
                "close": values[-1],
                "avg": mean(values),
                "avg_total_cost": mean(point.total_cost for point in bucket),
                "avg_net_profit": mean(point.net_profit for point in bucket),
                "count": len(bucket),
            }
        )
    return result


def estimate_half_life_seconds(points: list[SpreadPoint], avg: float) -> float | None:
    if len(points) < 20:
        return None
    x_values = [point.spread - avg for point in points[:-1]]
    y_values = [point.spread - avg for point in points[1:]]
    denominator = sum(value * value for value in x_values)
    if denominator <= 0:
        return None
    phi = sum(x * y for x, y in zip(x_values, y_values)) / denominator
    if phi <= 0 or phi >= 1:
        return None
    intervals = [
        (later.created_at - earlier.created_at).total_seconds()
        for earlier, later in zip(points[:-1], points[1:])
        if (later.created_at - earlier.created_at).total_seconds() > 0
    ]
    if not intervals:
        return None
    return -math.log(2) / math.log(phi) * median(intervals)


def estimate_reversion_probabilities(points: list[SpreadPoint], current_spread: float, avg: float) -> dict[str, float | None]:
    current_deviation = abs(current_spread - avg)
    if len(points) < 20 or current_deviation <= 0:
        return {key: None for key in REVERSION_HORIZONS}
    threshold = max(current_deviation * 0.5, 1e-12)
    candidates = [index for index, point in enumerate(points[:-1]) if abs(point.spread - avg) >= current_deviation * 0.8]
    probabilities: dict[str, float | None] = {}
    for key, horizon_seconds in REVERSION_HORIZONS.items():
        hits = 0
        valid = 0
        for index in candidates:
            start_time = points[index].created_at
            window = (
                point
                for point in points[index + 1 :]
                if 0 < (point.created_at - start_time).total_seconds() <= horizon_seconds
            )
            window_points = list(window)
            if not window_points:
                continue
            valid += 1
            if any(abs(point.spread - avg) <= threshold for point in window_points):
                hits += 1
        probabilities[key] = hits / valid if valid else None
    return probabilities


def estimate_max_adverse_spread(spreads: Iterable[float], current_spread: float, avg: float) -> float:
    values = list(spreads)
    if not values:
        return 0.0
    if current_spread >= avg:
        return max(values) - current_spread
    return current_spread - min(values)


def classify_analytics(sample_count: int, z_score: float, half_life: float | None, probability_15m: float | None) -> tuple[str, str]:
    if sample_count < 100:
        return "watch_only", "样本数量不足，仅用于观察"
    if abs(z_score) < 2:
        return "normal_range", "当前价差未显著偏离历史分布"
    if probability_15m is not None and probability_15m < 0.4:
        return "too_risky", "历史同类偏离在 15 分钟内回归概率偏低"
    if half_life is not None and half_life > 60 * 60:
        return "slow_reversion", "估算半衰期超过 1 小时"
    return "mean_reversion", "价差偏离具备均值回归观察价值"


def score_opportunity(z_score: float, probability_15m: float | None, half_life: float | None, sample_count: int) -> float:
    if sample_count < 100:
        return 0.0
    z_component = min(abs(z_score) / 4, 1.0) * 40
    probability_component = (probability_15m or 0.0) * 40
    half_life_component = 20
    if half_life is None:
        half_life_component = 8
    elif half_life > 0:
        half_life_component = max(0.0, min(20.0, 20.0 * (1 - half_life / (2 * 60 * 60))))
    return round(z_component + probability_component + half_life_component, 2)


def _empty_summary(range_value: str) -> dict[str, object]:
    return {
        "range": range_value,
        "sample_count": 0,
        "current_spread": 0.0,
        "mean": 0.0,
        "std": 0.0,
        "z_score": 0.0,
        "percentile": 0.0,
        "half_life_seconds": None,
        "reversion_probability": {key: None for key in REVERSION_HORIZONS},
        "max_adverse_spread": 0.0,
        "avg_total_cost": 0.0,
        "analytics_status": "no_data",
        "reason": "暂无价差快照",
        "opportunity_score": 0.0,
    }
