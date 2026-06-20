import json
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib import request

from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.db.models import SymbolMapping


RANGE_SECONDS = {
    "24h": 24 * 3600,
    "7d": 7 * 24 * 3600,
    "30d": 30 * 24 * 3600,
    "90d": 90 * 24 * 3600,
}

BUCKET_SECONDS = {
    "raw": 0,
    "hour": 3600,
    "day": 24 * 3600,
}


@dataclass
class FundingPoint:
    time: datetime
    funding_rate: float
    premium: float | None = None


def funding_history(db: Session, symbol: str, range_value: str, bucket: str) -> dict:
    normalized_range = range_value if range_value in RANGE_SECONDS else "7d"
    normalized_bucket = bucket if bucket in BUCKET_SECONDS else "day"
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == symbol.upper()).first()
    hyperliquid_symbol = mapping.hyperliquid_symbol if mapping else symbol.upper()

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - RANGE_SECONDS[normalized_range] * 1000
    source_error = ""
    try:
        points = fetch_funding_history(hyperliquid_symbol, start_ms, end_ms)
    except Exception as exc:
        source_error = str(exc)
        points = []
    items = bucket_funding_points(points, normalized_bucket)
    return {
        "symbol": symbol.upper(),
        "hyperliquid_symbol": hyperliquid_symbol,
        "range": normalized_range,
        "bucket": normalized_bucket,
        "summary": summarize_funding(points, normalized_range),
        "items": items,
        "source_error": source_error,
    }


def fetch_funding_history(coin: str, start_ms: int, end_ms: int) -> list[FundingPoint]:
    payload = {"type": "fundingHistory", "coin": coin, "startTime": start_ms, "endTime": end_ms}
    data = _post_hyperliquid_info(payload)
    points: list[FundingPoint] = []
    for item in data:
        timestamp_ms = int(item.get("time", 0))
        if not timestamp_ms:
            continue
        points.append(
            FundingPoint(
                time=datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).replace(tzinfo=None),
                funding_rate=float(item.get("fundingRate", 0.0)),
                premium=float(item["premium"]) if item.get("premium") is not None else None,
            )
        )
    return sorted(points, key=lambda point: point.time)


def summarize_funding(points: list[FundingPoint], range_value: str) -> dict:
    if not points:
        return {
            "sample_count": 0,
            "positive_count": 0,
            "negative_count": 0,
            "positive_ratio": 0.0,
            "avg_funding_rate": 0.0,
            "median_funding_rate": 0.0,
            "sum_funding_rate": 0.0,
            "annualized_estimate": 0.0,
            "max_funding_rate": 0.0,
            "min_funding_rate": 0.0,
            "latest_funding_rate": 0.0,
            "bias": "no_data",
        }

    rates = [point.funding_rate for point in points]
    positive_count = sum(1 for value in rates if value > 0)
    negative_count = sum(1 for value in rates if value < 0)
    sum_rate = sum(rates)
    days = max(RANGE_SECONDS.get(range_value, RANGE_SECONDS["7d"]) / 86400, 1)
    annualized = (sum_rate / days) * 365
    positive_ratio = positive_count / len(rates)
    avg_rate = statistics.fmean(rates)
    if positive_ratio >= 0.65 and avg_rate > 0:
        bias = "positive"
    elif positive_ratio <= 0.35 and avg_rate < 0:
        bias = "negative"
    else:
        bias = "mixed"
    return {
        "sample_count": len(rates),
        "positive_count": positive_count,
        "negative_count": negative_count,
        "positive_ratio": positive_ratio,
        "avg_funding_rate": avg_rate,
        "median_funding_rate": statistics.median(rates),
        "sum_funding_rate": sum_rate,
        "annualized_estimate": annualized,
        "max_funding_rate": max(rates),
        "min_funding_rate": min(rates),
        "latest_funding_rate": rates[-1],
        "bias": bias,
    }


def bucket_funding_points(points: list[FundingPoint], bucket: str) -> list[dict]:
    if bucket == "raw":
        return [
            {
                "time": point.time.isoformat(),
                "avg_funding_rate": point.funding_rate,
                "sum_funding_rate": point.funding_rate,
                "positive_count": 1 if point.funding_rate > 0 else 0,
                "negative_count": 1 if point.funding_rate < 0 else 0,
                "count": 1,
                "premium": point.premium,
            }
            for point in points
        ]

    seconds = BUCKET_SECONDS[bucket]
    buckets: dict[datetime, list[FundingPoint]] = defaultdict(list)
    for point in points:
        if bucket == "day":
            bucket_start = datetime(point.time.year, point.time.month, point.time.day)
        else:
            epoch = int(point.time.replace(tzinfo=timezone.utc).timestamp())
            bucket_epoch = epoch - (epoch % seconds)
            bucket_start = datetime.fromtimestamp(bucket_epoch, tz=timezone.utc).replace(tzinfo=None)
        buckets[bucket_start].append(point)

    items = []
    for bucket_start in sorted(buckets):
        rows = buckets[bucket_start]
        rates = [row.funding_rate for row in rows]
        items.append(
            {
                "time": bucket_start.isoformat(),
                "avg_funding_rate": statistics.fmean(rates),
                "sum_funding_rate": sum(rates),
                "positive_count": sum(1 for value in rates if value > 0),
                "negative_count": sum(1 for value in rates if value < 0),
                "count": len(rates),
                "premium": None,
            }
        )
    return items


def _post_hyperliquid_info(payload: dict):
    settings = get_settings()
    req = request.Request(
        settings.hyperliquid_info_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))
