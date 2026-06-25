from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from typing import Any


class ScanStateStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._spreads: list[dict[str, Any]] = []
        self._direction_spreads: list[dict[str, Any]] = []
        self._opportunities: list[dict[str, Any]] = []
        self._updated_at: datetime | None = None

    def update(
        self,
        spreads: list[dict[str, Any]],
        opportunities: list[dict[str, Any]],
        direction_spreads: list[dict[str, Any]] | None = None,
    ) -> None:
        with self._lock:
            self._spreads = deepcopy(spreads)
            self._direction_spreads = deepcopy(direction_spreads if direction_spreads is not None else spreads)
            self._opportunities = deepcopy(opportunities)
            self._updated_at = datetime.now(timezone.utc).replace(tzinfo=None)

    def merge_opportunity_ids(self, ids_by_key: dict[tuple[str, str], int]) -> None:
        if not ids_by_key:
            return
        with self._lock:
            for row in self._opportunities:
                key = (str(row.get("symbol", "")).upper(), str(row.get("direction", "")))
                if key in ids_by_key:
                    row["id"] = ids_by_key[key]
            self._updated_at = datetime.now(timezone.utc).replace(tzinfo=None)

    def remove_symbols(self, symbols: set[str]) -> None:
        if not symbols:
            return
        normalized = {symbol.upper() for symbol in symbols}
        with self._lock:
            self._spreads = [row for row in self._spreads if str(row.get("symbol", "")).upper() not in normalized]
            self._direction_spreads = [row for row in self._direction_spreads if str(row.get("symbol", "")).upper() not in normalized]
            self._opportunities = [row for row in self._opportunities if str(row.get("symbol", "")).upper() not in normalized]
            self._updated_at = datetime.now(timezone.utc).replace(tzinfo=None)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "spreads": deepcopy(self._spreads),
                "direction_spreads": deepcopy(self._direction_spreads),
                "opportunities": deepcopy(self._opportunities),
                "updated_at": self._updated_at,
                "ready": self._updated_at is not None,
            }


scan_state_store = ScanStateStore()
