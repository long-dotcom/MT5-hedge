from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level registry & config cache
# ---------------------------------------------------------------------------

breaker_registry: dict[str, SymbolBreaker] = {}

_cb_config: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Per-direction detection window tracker
# ---------------------------------------------------------------------------

@dataclass
class _DirectionTracker:
    """Holds the detection window for one direction of a symbol."""
    window: list[tuple[datetime, float]] = field(default_factory=list)
    last_jitter: float = 0.0


# ---------------------------------------------------------------------------
# Symbol-level circuit breaker
# ---------------------------------------------------------------------------

@dataclass
class SymbolBreaker:
    symbol: str
    state: str = "CLOSED"  # CLOSED or OPEN
    last_trip_time: datetime | None = None
    cooldown_seconds: float = 3.0

    # Per-direction detection windows
    _direction_trackers: dict[str, _DirectionTracker] = field(default_factory=dict)

    # Baseline (5-15 min) of jitter ratios — one sample per update() call
    baseline_jitters: deque = field(default_factory=lambda: deque(maxlen=2000))

    current_jitter_ratio: float = 0.0
    threshold: float = 0.75  # initial fixed threshold (cold-start)
    baseline_multiplier: float = 2.0  # K
    min_baseline_samples: int = 50
    detection_seconds: float = 5.0  # detection window size

    # -- config update ------------------------------------------------------

    def apply_config(self, config: dict[str, Any]) -> None:
        """Update breaker parameters from *config* dict (DB-sourced)."""
        if not config:
            return
        self.cooldown_seconds = config.get("cooldown_seconds", self.cooldown_seconds)
        self.threshold = config.get("threshold", self.threshold)
        self.baseline_multiplier = config.get("baseline_multiplier", self.baseline_multiplier)
        self.min_baseline_samples = config.get("min_baseline_samples", self.min_baseline_samples)
        self.detection_seconds = config.get("detection_seconds", self.detection_seconds)

    # -- public API ---------------------------------------------------------

    def update(self, direction: str, entry_spread: float, now: datetime) -> None:
        """Called on every new quote for a specific direction."""
        tracker = self._direction_trackers.setdefault(direction, _DirectionTracker())

        # Append to this direction's detection window
        tracker.window.append((now, entry_spread))
        self._prune_window(tracker.window, now)

        # Compute jitter for this direction
        tracker.last_jitter = self._calculate_jitter(tracker.window)

        # Overall jitter = max across all directions
        self.current_jitter_ratio = max(
            (t.last_jitter for t in self._direction_trackers.values()),
            default=0.0,
        )

        # Update baseline & adaptive threshold
        self._update_baseline()

        # Evaluate trip condition
        if self.state == "CLOSED" and self.current_jitter_ratio > self.threshold:
            self.state = "OPEN"
            self.last_trip_time = now
            logger.warning(
                "断路器 OPEN: symbol=%s jitter=%.3f threshold=%.3f",
                self.symbol,
                self.current_jitter_ratio,
                self.threshold,
            )

    def is_blocked(self, now: datetime) -> bool:
        """Return True if the breaker is OPEN (trading forbidden)."""
        if self.state != "OPEN":
            return False
        # Check cooldown expiry
        if self.last_trip_time is not None:
            elapsed = (now - self.last_trip_time).total_seconds()
            if elapsed >= self.cooldown_seconds:
                self.state = "CLOSED"
                logger.info(
                    "断路器 CLOSED (冷却结束): symbol=%s",
                    self.symbol,
                )
                return False
        return True

    # -- internal helpers ---------------------------------------------------

    @staticmethod
    def _calculate_jitter(window: list[tuple[datetime, float]]) -> float:
        spreads = [s for _, s in window]
        if len(spreads) < 3:
            return 0.0

        changes = [spreads[i + 1] - spreads[i] for i in range(len(spreads) - 1)]
        # Ignore zero-magnitude changes (price unchanged)
        non_zero = [(i, c) for i, c in enumerate(changes) if abs(c) > 1e-9]
        if len(non_zero) < 2:
            return 0.0

        alternations = 0
        for j in range(1, len(non_zero)):
            prev_sign = 1 if non_zero[j - 1][1] > 0 else -1
            curr_sign = 1 if non_zero[j][1] > 0 else -1
            if prev_sign != curr_sign:
                alternations += 1

        return alternations / (len(non_zero) - 1)

    def _update_baseline(self) -> None:
        self.baseline_jitters.append(self.current_jitter_ratio)
        if len(self.baseline_jitters) >= self.min_baseline_samples:
            sorted_jitters = sorted(self.baseline_jitters)
            p75_index = int(len(sorted_jitters) * 0.75)
            p75 = sorted_jitters[min(p75_index, len(sorted_jitters) - 1)]
            self.threshold = p75 * self.baseline_multiplier

    def _prune_window(
        self,
        window: list[tuple[datetime, float]],
        now: datetime,
    ) -> None:
        cutoff = now.timestamp() - self.detection_seconds
        while window and window[0][0].timestamp() < cutoff:
            window.pop(0)


# ---------------------------------------------------------------------------
# Public convenience functions
# ---------------------------------------------------------------------------

def _load_cb_settings(db: "Session") -> dict[str, Any]:
    """Load circuit-breaker parameters from StrategySetting row."""
    from app.db.models import StrategySetting
    row = db.query(StrategySetting).first()
    if not row:
        return {}
    return {
        "cooldown_seconds": getattr(row, "cb_cooldown_seconds", 3.0),
        "threshold": getattr(row, "cb_initial_threshold", 0.75),
        "baseline_multiplier": getattr(row, "cb_baseline_multiplier", 2.0),
        "min_baseline_samples": getattr(row, "cb_min_baseline_samples", 50),
        "detection_seconds": getattr(row, "cb_detection_seconds", 5.0),
    }


def reload_config(db: "Session") -> None:
    """Reload CB config from DB and push to all existing breaker instances."""
    global _cb_config
    _cb_config = _load_cb_settings(db)
    for breaker in breaker_registry.values():
        breaker.apply_config(_cb_config)
    logger.debug("断路器配置已刷新: %s", _cb_config)


def get_breaker(symbol: str) -> SymbolBreaker:
    """Get or create a breaker instance for *symbol*."""
    if symbol not in breaker_registry:
        breaker_registry[symbol] = SymbolBreaker(symbol=symbol, **_cb_config)
    return breaker_registry[symbol]


def feed_spread(
    symbol: str,
    direction: str,
    entry_spread: float,
    now: datetime | None = None,
) -> None:
    """External entry point: feed a new spread data-point into the breaker."""
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    get_breaker(symbol).update(direction, entry_spread, now)


def is_blocked(symbol: str, now: datetime | None = None) -> tuple[bool, float, float]:
    """External entry point: check whether *symbol* is blocked.

    Returns ``(blocked, current_jitter, threshold)``.
    """
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    breaker = get_breaker(symbol)
    return breaker.is_blocked(now), breaker.current_jitter_ratio, breaker.threshold


def get_breaker_status(symbol: str) -> dict[str, Any]:
    """Return a snapshot of the breaker state (for logging / debugging)."""
    breaker = get_breaker(symbol)
    return {
        "symbol": breaker.symbol,
        "state": breaker.state,
        "current_jitter_ratio": breaker.current_jitter_ratio,
        "threshold": breaker.threshold,
        "baseline_samples": len(breaker.baseline_jitters),
        "last_trip_time": breaker.last_trip_time,
        "cooldown_seconds": breaker.cooldown_seconds,
    }
