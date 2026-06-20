from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean

from app.market.quotes import Quote, quote_cache


@dataclass(frozen=True)
class LeadLagEvent:
    symbol: str
    leader_platform: str
    follower_platform: str
    leader_time: datetime
    follower_time: datetime | None
    lag_ms: float | None
    leader_move: float
    follower_move: float
    leader_move_bps: float
    follower_move_bps: float
    max_mid_diff: float
    direction: str
    followed: bool


def lead_lag_report(
    symbol: str,
    window_seconds: int = 300,
    threshold_bps: float = 3.0,
    min_move: float = 0.0,
    follow_ratio: float = 0.5,
    max_lag_ms: int = 2000,
) -> dict[str, object]:
    symbol = symbol.upper()
    since = datetime.utcnow() - timedelta(seconds=max(window_seconds, 10))
    hl = [quote for quote in quote_cache.history("hyperliquid", symbol) if quote.local_recv_ts >= since]
    mt5 = [quote for quote in quote_cache.history("mt5", symbol) if quote.local_recv_ts >= since]
    events = _events_for_direction(symbol, "hyperliquid", "mt5", hl, mt5, threshold_bps, min_move, follow_ratio, max_lag_ms)
    events += _events_for_direction(symbol, "mt5", "hyperliquid", mt5, hl, threshold_bps, min_move, follow_ratio, max_lag_ms)
    events.sort(key=lambda item: item.leader_time, reverse=True)
    summary = {
        "hyperliquid_to_mt5": _summary(events, "hyperliquid", "mt5"),
        "mt5_to_hyperliquid": _summary(events, "mt5", "hyperliquid"),
    }
    latest_hl = hl[-1] if hl else None
    latest_mt5 = mt5[-1] if mt5 else None
    return {
        "symbol": symbol,
        "window_seconds": max(window_seconds, 10),
        "threshold_bps": threshold_bps,
        "min_move": min_move,
        "follow_ratio": follow_ratio,
        "max_lag_ms": max_lag_ms,
        "latest": {
            "hyperliquid": _quote_dict(latest_hl),
            "mt5": _quote_dict(latest_mt5),
        },
        "summary": summary,
        "items": [_event_dict(event) for event in events[:200]],
        "series": _series(hl, mt5),
    }


def _events_for_direction(
    symbol: str,
    leader: str,
    follower: str,
    leader_quotes: list[Quote],
    follower_quotes: list[Quote],
    threshold_bps: float,
    min_move: float,
    follow_ratio: float,
    max_lag_ms: int,
) -> list[LeadLagEvent]:
    if len(leader_quotes) < 2 or not follower_quotes:
        return []
    events: list[LeadLagEvent] = []
    cooldown_until = datetime.min
    for prev_quote, quote in zip(leader_quotes[:-1], leader_quotes[1:]):
        if quote.local_recv_ts < cooldown_until:
            continue
        move = quote.mid - prev_quote.mid
        move_bps = move / prev_quote.mid * 10_000 if prev_quote.mid else 0.0
        threshold = max(abs(prev_quote.mid) * threshold_bps / 10_000, min_move)
        if abs(move) < threshold:
            continue
        direction = "up" if move > 0 else "down"
        follower_base = _latest_at_or_before(follower_quotes, quote.local_recv_ts)
        if not follower_base:
            continue
        deadline = quote.local_recv_ts + timedelta(milliseconds=max_lag_ms)
        target_bps = abs(move_bps) * max(min(follow_ratio, 1.0), 0.0)
        follower_hit = None
        for candidate in follower_quotes:
            if candidate.local_recv_ts <= quote.local_recv_ts:
                continue
            if candidate.local_recv_ts > deadline:
                break
            follower_move = candidate.mid - follower_base.mid
            follower_move_bps = follower_move / follower_base.mid * 10_000 if follower_base.mid else 0.0
            same_direction = (move > 0 and follower_move > 0) or (move < 0 and follower_move < 0)
            if same_direction and abs(follower_move_bps) >= target_bps:
                follower_hit = candidate
                break
        end_time = follower_hit.local_recv_ts if follower_hit else deadline
        follower_move = (follower_hit.mid - follower_base.mid) if follower_hit else 0.0
        follower_move_bps = follower_move / follower_base.mid * 10_000 if follower_base.mid else 0.0
        events.append(
            LeadLagEvent(
                symbol=symbol,
                leader_platform=leader,
                follower_platform=follower,
                leader_time=quote.local_recv_ts,
                follower_time=follower_hit.local_recv_ts if follower_hit else None,
                lag_ms=(follower_hit.local_recv_ts - quote.local_recv_ts).total_seconds() * 1000 if follower_hit else None,
                leader_move=move,
                follower_move=follower_move,
                leader_move_bps=move_bps,
                follower_move_bps=follower_move_bps,
                max_mid_diff=_max_mid_diff(symbol, quote.local_recv_ts, end_time),
                direction=direction,
                followed=bool(follower_hit),
            )
        )
        cooldown_until = quote.local_recv_ts + timedelta(milliseconds=max_lag_ms)
    return events


def _latest_at_or_before(quotes: list[Quote], timestamp: datetime) -> Quote | None:
    result = None
    for quote in quotes:
        if quote.local_recv_ts <= timestamp:
            result = quote
        else:
            break
    return result


def _max_mid_diff(symbol: str, start: datetime, end: datetime) -> float:
    hl = [quote for quote in quote_cache.history("hyperliquid", symbol) if start <= quote.local_recv_ts <= end]
    mt5 = [quote for quote in quote_cache.history("mt5", symbol) if start <= quote.local_recv_ts <= end]
    if not hl or not mt5:
        return 0.0
    values: list[float] = []
    for quote in hl:
        other = _latest_at_or_before(mt5, quote.local_recv_ts)
        if other:
            values.append(abs(quote.mid - other.mid))
    return max(values) if values else 0.0


def _summary(events: list[LeadLagEvent], leader: str, follower: str) -> dict[str, object]:
    selected = [event for event in events if event.leader_platform == leader and event.follower_platform == follower]
    followed = [event for event in selected if event.followed and event.lag_ms is not None]
    lags = sorted(float(event.lag_ms) for event in followed if event.lag_ms is not None)
    return {
        "leader": leader,
        "follower": follower,
        "event_count": len(selected),
        "follow_count": len(followed),
        "follow_rate": len(followed) / len(selected) if selected else 0.0,
        "avg_lag_ms": mean(lags) if lags else None,
        "p50_lag_ms": _percentile(lags, 0.50),
        "p90_lag_ms": _percentile(lags, 0.90),
        "avg_max_mid_diff": mean([event.max_mid_diff for event in selected]) if selected else 0.0,
    }


def _series(hl: list[Quote], mt5: list[Quote]) -> list[dict[str, object]]:
    merged = sorted([(quote.local_recv_ts, quote.platform, quote.mid) for quote in hl + mt5], key=lambda item: item[0])
    latest: dict[str, float] = {}
    rows: list[dict[str, object]] = []
    for timestamp, platform, mid in merged:
        latest[platform] = mid
        rows.append(
            {
                "time": timestamp.isoformat(),
                "hyperliquid_mid": latest.get("hyperliquid"),
                "mt5_mid": latest.get("mt5"),
                "mid_diff": (latest["hyperliquid"] - latest["mt5"]) if "hyperliquid" in latest and "mt5" in latest else None,
            }
        )
    if len(rows) <= 1000:
        return rows
    step = max(len(rows) // 1000, 1)
    return rows[::step]


def _quote_dict(quote: Quote | None) -> dict[str, object] | None:
    if not quote:
        return None
    return {
        "bid": quote.bid,
        "ask": quote.ask,
        "mid": quote.mid,
        "source": quote.source,
        "local_recv_ts": quote.local_recv_ts.isoformat(),
        "exchange_ts": quote.exchange_ts.isoformat() if quote.exchange_ts else None,
        "sequence": quote.sequence,
    }


def _event_dict(event: LeadLagEvent) -> dict[str, object]:
    return {
        "symbol": event.symbol,
        "leader_platform": event.leader_platform,
        "follower_platform": event.follower_platform,
        "leader_time": event.leader_time.isoformat(),
        "follower_time": event.follower_time.isoformat() if event.follower_time else None,
        "lag_ms": event.lag_ms,
        "leader_move": event.leader_move,
        "follower_move": event.follower_move,
        "leader_move_bps": event.leader_move_bps,
        "follower_move_bps": event.follower_move_bps,
        "max_mid_diff": event.max_mid_diff,
        "direction": event.direction,
        "followed": event.followed,
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    pct = min(max(percentile, 0.0), 1.0)
    index = pct * (len(values) - 1)
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    weight = index - lower
    return values[lower] * (1 - weight) + values[upper] * weight
