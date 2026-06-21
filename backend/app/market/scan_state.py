from copy import deepcopy
from datetime import datetime
from threading import Lock
from typing import Any


class ScanStateStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._spreads: list[dict[str, Any]] = []
        self._opportunities: list[dict[str, Any]] = []
        self._updated_at: datetime | None = None

    def update(self, spreads: list[dict[str, Any]], opportunities: list[dict[str, Any]]) -> None:
        with self._lock:
            self._spreads = deepcopy(spreads)
            self._opportunities = deepcopy(opportunities)
            self._updated_at = datetime.utcnow()

    def remove_symbols(self, symbols: set[str]) -> None:
        if not symbols:
            return
        normalized = {symbol.upper() for symbol in symbols}
        with self._lock:
            self._spreads = [row for row in self._spreads if str(row.get("symbol", "")).upper() not in normalized]
            self._opportunities = [row for row in self._opportunities if str(row.get("symbol", "")).upper() not in normalized]
            self._updated_at = datetime.utcnow()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "spreads": deepcopy(self._spreads),
                "opportunities": deepcopy(self._opportunities),
                "updated_at": self._updated_at,
                "ready": self._updated_at is not None,
            }


scan_state_store = ScanStateStore()
