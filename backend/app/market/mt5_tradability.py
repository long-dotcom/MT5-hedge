from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import threading
import time

from sqlalchemy.orm import Session

from app.adapters.mt5 import MT5OrderCheck, mt5_market_order_check
from app.config.settings import get_settings
from app.db.models import StrategySetting, SystemSetting
from app.market.symbols import enabled_mappings


@dataclass(frozen=True)
class TradabilityState:
    symbol: str
    mt5_symbol: str
    side: str
    allowed: bool
    message: str
    checked_at: float
    quantity: float
    retcode: int | None = None
    source: str = "unknown"

    @property
    def age_ms(self) -> float:
        return (time.time() - self.checked_at) * 1000


class MT5TradabilityCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[tuple[str, str], TradabilityState] = {}
        self._blocked: dict[tuple[str, str], tuple[float, str]] = {}
        self._initialized = False
        self._last_refresh_at = 0.0

    def initialized(self) -> bool:
        with self._lock:
            return self._initialized

    def mark_not_initialized(self) -> None:
        with self._lock:
            self._initialized = False

    def get(self, symbol: str, side: str) -> TradabilityState | None:
        key = (symbol.upper(), side.lower())
        with self._lock:
            return self._states.get(key)

    def is_fresh_allowed(self, symbol: str, side: str, ttl_ms: int | None = None) -> tuple[bool, str]:
        blocked = self._active_block(symbol, side)
        if blocked:
            return False, blocked
        state = self.get(symbol, side)
        if not state:
            return False, "MT5 交易能力缓存缺失"
        max_age = ttl_ms if ttl_ms is not None else get_settings().mt5_tradability_cache_ttl_ms
        if state.age_ms > max_age:
            return False, f"MT5 交易能力缓存过期: {state.age_ms:.0f}ms > {max_age}ms"
        if not state.allowed:
            return False, state.message
        return True, ""

    def update(self, symbol: str, mt5_symbol: str, side: str, quantity: float, check: MT5OrderCheck, source: str) -> TradabilityState:
        blocked = self._active_block(symbol, side)
        allowed = check.allowed and not blocked
        message = blocked or check.message
        state = TradabilityState(
            symbol=symbol.upper(),
            mt5_symbol=mt5_symbol,
            side=side.lower(),
            allowed=allowed,
            message=message,
            checked_at=time.time(),
            quantity=quantity,
            retcode=getattr(check, "retcode", None),
            source=source,
        )
        with self._lock:
            self._states[(state.symbol, state.side)] = state
        return state

    def refresh(self, db: Session) -> dict[str, int]:
        self.load_persistent_blocks(db)
        strategy = db.query(StrategySetting).first() or StrategySetting()
        simulated = strategy.execution_mode == "paper"
        checked = 0
        allowed = 0
        for mapping in enabled_mappings(db):
            quantity = _probe_quantity(mapping)
            for side in ("buy", "sell"):
                check = mt5_market_order_check(mapping.mt5_symbol, side, quantity, demo=simulated)
                state = self.update(mapping.symbol, mapping.mt5_symbol, side, quantity, check, "background")
                checked += 1
                if state.allowed:
                    allowed += 1
        with self._lock:
            self._initialized = True
            self._last_refresh_at = time.time()
        return {"checked": checked, "allowed": allowed}

    def block(self, db: Session, symbol: str, mt5_symbol: str, side: str, quantity: float, message: str, *, seconds: int | None = None, source: str = "runtime_reject") -> TradabilityState:
        duration = seconds if seconds is not None else get_settings().mt5_trade_reject_quarantine_seconds
        until = time.time() + max(duration, 1)
        key = (symbol.upper(), side.lower())
        with self._lock:
            self._blocked[key] = (until, message)
        _persist_block(db, key[0], key[1], mt5_symbol, quantity, message, until)
        return self.update(symbol, mt5_symbol, side, quantity, MT5OrderCheck(False, message), source)

    def load_persistent_blocks(self, db: Session) -> None:
        now = time.time()
        rows = db.query(SystemSetting).filter(SystemSetting.key.like("mt5_tradability_block:%")).all()
        active: dict[tuple[str, str], tuple[float, str]] = {}
        stale_keys: list[str] = []
        for row in rows:
            try:
                payload = json.loads(row.value or "{}")
                until = float(payload.get("until", 0.0) or 0.0)
                message = str(payload.get("message", "") or "MT5 交易方向临时隔离")
                _, symbol, side = row.key.split(":", 2)
            except Exception:
                stale_keys.append(row.key)
                continue
            if until <= now:
                stale_keys.append(row.key)
                continue
            active[(symbol.upper(), side.lower())] = (until, message)
        with self._lock:
            self._blocked.update(active)
        for key in stale_keys:
            db.query(SystemSetting).filter(SystemSetting.key == key).delete()
        if stale_keys:
            db.commit()

    def snapshot(self) -> list[dict]:
        with self._lock:
            states = list(self._states.values())
            initialized = self._initialized
            last_refresh_at = self._last_refresh_at
        return [
            {
                "symbol": state.symbol,
                "mt5_symbol": state.mt5_symbol,
                "side": state.side,
                "allowed": state.allowed,
                "message": state.message,
                "age_ms": state.age_ms,
                "quantity": state.quantity,
                "retcode": state.retcode,
                "source": state.source,
                "initialized": initialized,
                "last_refresh_at": last_refresh_at,
            }
            for state in states
        ]

    def _active_block(self, symbol: str, side: str) -> str:
        key = (symbol.upper(), side.lower())
        with self._lock:
            blocked = self._blocked.get(key)
            if not blocked:
                return ""
            until, message = blocked
            if until <= time.time():
                self._blocked.pop(key, None)
                return ""
            remaining = until - time.time()
            return f"{message}; quarantine_remaining={remaining:.0f}s"


def _probe_quantity(mapping) -> float:
    lot_min = float(mapping.mt5_min_lot or mapping.min_order_size or 0.01)
    if lot_min <= 0:
        return 0.01
    return lot_min


mt5_tradability_cache = MT5TradabilityCache()


def refresh_mt5_tradability_cache(db: Session) -> dict[str, int]:
    return mt5_tradability_cache.refresh(db)


def block_mt5_tradability(db: Session, symbol: str, mt5_symbol: str, side: str, quantity: float, message: str, *, seconds: int | None = None, source: str = "runtime_reject") -> TradabilityState:
    return mt5_tradability_cache.block(db, symbol, mt5_symbol, side, quantity, message, seconds=seconds, source=source)


def _persist_block(db: Session, symbol: str, side: str, mt5_symbol: str, quantity: float, message: str, until: float) -> None:
    key = f"mt5_tradability_block:{symbol.upper()}:{side.lower()}"
    row = db.get(SystemSetting, key)
    if not row:
        row = SystemSetting(key=key)
        db.add(row)
    row.value = json.dumps(
        {
            "symbol": symbol.upper(),
            "side": side.lower(),
            "mt5_symbol": mt5_symbol,
            "quantity": quantity,
            "message": message,
            "until": until,
            "blocked_at": datetime.utcnow().isoformat(),
        },
        ensure_ascii=False,
    )
