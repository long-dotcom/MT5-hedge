from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.config.settings import Settings
from app.db.models import SystemSetting


PAPER_LIVE_PROBE_ENABLED_KEY = "paper_live_probe_enabled"
PAPER_LIVE_PARALLEL_EXECUTION_KEY = "paper_live_parallel_execution"


def execution_settings_payload(db: Session, settings: Settings) -> dict[str, Any]:
    return {
        "paper_live_probe_enabled": runtime_paper_live_probe_enabled_for_display(db, settings),
        "paper_live_parallel_execution": runtime_paper_live_parallel_execution(db, settings),
        "paper_live_probe_confirmation_required": "ENABLE PAPER LIVE PROBE",
    }


def set_execution_settings(db: Session, *, paper_live_probe_enabled: bool, paper_live_parallel_execution: bool) -> None:
    _set_system_setting(db, PAPER_LIVE_PROBE_ENABLED_KEY, _bool_text(paper_live_probe_enabled))
    _set_system_setting(db, PAPER_LIVE_PARALLEL_EXECUTION_KEY, _bool_text(paper_live_parallel_execution))


def runtime_paper_live_probe_enabled(db: Session, settings: Settings) -> bool:
    value = _get_system_setting(db, PAPER_LIVE_PROBE_ENABLED_KEY)
    if value is not None:
        return _parse_bool(value)
    return bool(getattr(settings, "paper_live_probe_enabled", False))


def runtime_paper_live_probe_enabled_for_display(db: Session, settings: Settings) -> bool:
    value = _get_system_setting(db, PAPER_LIVE_PROBE_ENABLED_KEY)
    if value is not None:
        return _parse_bool(value)
    return bool(getattr(settings, "paper_live_probe_enabled", False) or getattr(settings, "hyperliquid_paper_live_order_enabled", False))


def runtime_paper_live_parallel_execution(db: Session | None, settings: Settings) -> bool:
    if db is not None:
        value = _get_system_setting(db, PAPER_LIVE_PARALLEL_EXECUTION_KEY)
        if value is not None:
            return _parse_bool(value)
    return bool(getattr(settings, "paper_live_parallel_execution", True))


def paper_live_probe_enabled_for_venue(db: Session | None, settings: Settings, venue: str) -> bool:
    venue = str(venue or "").strip().lower()
    if not venue or venue == "mt5":
        return False
    if db is not None:
        value = _get_system_setting(db, PAPER_LIVE_PROBE_ENABLED_KEY)
        if value is not None:
            return _parse_bool(value)
    if venue == "hyperliquid" and bool(getattr(settings, "hyperliquid_paper_live_order_enabled", False)):
        return True
    if not bool(getattr(settings, "paper_live_probe_enabled", False)):
        return False
    venues = _paper_live_probe_venues(settings)
    return "*" in venues or venue in venues


def _get_system_setting(db: Session, key: str) -> str | None:
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    return row.value if row else None


def _set_system_setting(db: Session, key: str, value: str) -> None:
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first() or SystemSetting(key=key)
    row.value = value
    db.add(row)


def _parse_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _paper_live_probe_venues(settings: Settings) -> set[str]:
    raw = str(getattr(settings, "paper_live_probe_venues", "") or "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}
