import threading
from typing import Optional

from loguru import logger

from app.config.settings import get_settings
from app.db.session import SessionLocal
from app.execution.auto_closer import run_auto_close
from app.execution.auto_executor import run_auto_execute
from app.execution.carry_costs import run_carry_cost_sync
from app.execution.persistence import persist_hedge_pool_events
from app.execution.reconciler import run_execution_reconcile
from app.market.scanner import persist_scan_state, run_scan
from app.market.mt5_schedule import sync_mt5_session_templates
from app.market.mt5_tradability import refresh_mt5_tradability_cache
from app.strategy.statistical_signal import refresh_signal_stats_cache


_timer: Optional[threading.Timer] = None
_stats_timer: Optional[threading.Timer] = None
_tradability_timer: Optional[threading.Timer] = None
_session_template_timer: Optional[threading.Timer] = None
_scan_persistence_timer: Optional[threading.Timer] = None
_execution_timer: Optional[threading.Timer] = None
_carry_cost_timer: Optional[threading.Timer] = None
_execution_persistence_timer: Optional[threading.Timer] = None
_running = False
_stats_refreshing = False
_tradability_refreshing = False
_session_template_refreshing = False
_scan_persisting = False
_execution_running = False
_carry_cost_running = False
_execution_persisting = False


def scanner_job() -> None:
    db = SessionLocal()
    try:
        run_scan(db)
    except Exception as exc:
        db.rollback()
        logger.exception(f"扫描任务失败: {exc}")
    finally:
        db.close()
    _schedule_next()


def execution_maintenance_job() -> None:
    global _execution_running
    if _execution_running:
        _schedule_next_execution()
        return
    _execution_running = True
    db = SessionLocal()
    try:
        run_auto_execute(db)
        run_auto_close(db)
        run_execution_reconcile(db)
    except Exception as exc:
        db.rollback()
        logger.exception(f"执行维护任务失败: {exc}")
    finally:
        db.close()
        _execution_running = False
    _schedule_next_execution()


def carry_cost_job() -> None:
    global _carry_cost_running
    if _carry_cost_running:
        _schedule_next_carry_cost()
        return
    _carry_cost_running = True
    db = SessionLocal()
    try:
        run_carry_cost_sync(db)
    except Exception as exc:
        db.rollback()
        logger.exception(f"资金费/过夜费同步任务失败: {exc}")
    finally:
        db.close()
        _carry_cost_running = False
    _schedule_next_carry_cost()


def execution_persistence_job() -> None:
    global _execution_persisting
    if _execution_persisting:
        _schedule_next_execution_persistence()
        return
    _execution_persisting = True
    db = SessionLocal()
    try:
        persist_hedge_pool_events(db)
    except Exception as exc:
        db.rollback()
        logger.exception(f"对冲池执行事件持久化失败: {exc}")
    finally:
        db.close()
        _execution_persisting = False
    _schedule_next_execution_persistence()


def scan_persistence_job() -> None:
    global _scan_persisting
    if _scan_persisting:
        _schedule_next_scan_persistence()
        return
    _scan_persisting = True
    db = SessionLocal()
    try:
        persist_scan_state(db)
    except Exception as exc:
        db.rollback()
        logger.exception(f"扫描状态持久化失败: {exc}")
    finally:
        db.close()
        _scan_persisting = False
    _schedule_next_scan_persistence()


def signal_stats_job() -> None:
    global _stats_refreshing
    if _stats_refreshing:
        _schedule_next_stats()
        return
    _stats_refreshing = True
    db = SessionLocal()
    try:
        refresh_signal_stats_cache(db)
    except Exception as exc:
        db.rollback()
        logger.exception(f"统计线刷新任务失败: {exc}")
    finally:
        db.close()
        _stats_refreshing = False
    _schedule_next_stats()


def mt5_tradability_job() -> None:
    global _tradability_refreshing
    if _tradability_refreshing:
        _schedule_next_tradability()
        return
    _tradability_refreshing = True
    db = SessionLocal()
    try:
        refresh_mt5_tradability_cache(db)
    except Exception as exc:
        db.rollback()
        logger.exception(f"MT5 交易能力刷新任务失败: {exc}")
    finally:
        db.close()
        _tradability_refreshing = False
    _schedule_next_tradability()


def mt5_session_template_job() -> None:
    global _session_template_refreshing
    if _session_template_refreshing:
        _schedule_next_session_templates()
        return
    _session_template_refreshing = True
    db = SessionLocal()
    try:
        sync_mt5_session_templates(db, only_auto=True)
    except Exception as exc:
        db.rollback()
        logger.exception(f"MT5 交易时段模板刷新任务失败: {exc}")
    finally:
        db.close()
        _session_template_refreshing = False
    _schedule_next_session_templates()


def _schedule_next() -> None:
    global _timer
    if not _running:
        return
    settings = get_settings()
    interval = settings.scanner_interval_ms / 1000 if settings.scanner_interval_ms > 0 else settings.scanner_interval_seconds
    _timer = threading.Timer(max(interval, 0.05), scanner_job)
    _timer.daemon = True
    _timer.start()


def _schedule_next_stats() -> None:
    global _stats_timer
    if not _running:
        return
    settings = get_settings()
    interval = max(settings.signal_stats_cache_ttl_ms / 1000, 1.0)
    _stats_timer = threading.Timer(interval, signal_stats_job)
    _stats_timer.daemon = True
    _stats_timer.start()


def _schedule_next_scan_persistence() -> None:
    global _scan_persistence_timer
    if not _running:
        return
    settings = get_settings()
    interval = max(settings.scan_persist_interval_ms / 1000, 0.1)
    _scan_persistence_timer = threading.Timer(interval, scan_persistence_job)
    _scan_persistence_timer.daemon = True
    _scan_persistence_timer.start()


def _schedule_next_execution() -> None:
    global _execution_timer
    if not _running:
        return
    settings = get_settings()
    interval = max(settings.execution_maintenance_interval_ms / 1000, 0.2)
    _execution_timer = threading.Timer(interval, execution_maintenance_job)
    _execution_timer.daemon = True
    _execution_timer.start()


def _schedule_next_carry_cost() -> None:
    global _carry_cost_timer
    if not _running:
        return
    settings = get_settings()
    interval = max(settings.carry_cost_sync_interval_seconds, 1)
    _carry_cost_timer = threading.Timer(interval, carry_cost_job)
    _carry_cost_timer.daemon = True
    _carry_cost_timer.start()


def _schedule_next_execution_persistence() -> None:
    global _execution_persistence_timer
    if not _running:
        return
    _execution_persistence_timer = threading.Timer(1.0, execution_persistence_job)
    _execution_persistence_timer.daemon = True
    _execution_persistence_timer.start()


def _schedule_next_tradability() -> None:
    global _tradability_timer
    if not _running:
        return
    settings = get_settings()
    interval = max(settings.mt5_tradability_refresh_seconds, 1)
    _tradability_timer = threading.Timer(interval, mt5_tradability_job)
    _tradability_timer.daemon = True
    _tradability_timer.start()


def _schedule_next_session_templates() -> None:
    global _session_template_timer
    if not _running:
        return
    settings = get_settings()
    interval = max(settings.mt5_session_template_refresh_hours, 1) * 3600
    _session_template_timer = threading.Timer(interval, mt5_session_template_job)
    _session_template_timer.daemon = True
    _session_template_timer.start()


def start_scheduler() -> None:
    global _running
    if not _running:
        _running = True
        _schedule_next()
        _schedule_next_scan_persistence()
        _schedule_next_execution()
        _schedule_next_carry_cost()
        _schedule_next_execution_persistence()
        _schedule_next_stats()
        _schedule_next_tradability()
        _schedule_next_session_templates()


def stop_scheduler() -> None:
    global _running
    _running = False
    if _timer:
        _timer.cancel()
    if _stats_timer:
        _stats_timer.cancel()
    if _tradability_timer:
        _tradability_timer.cancel()
    if _session_template_timer:
        _session_template_timer.cancel()
    if _scan_persistence_timer:
        _scan_persistence_timer.cancel()
    if _execution_timer:
        _execution_timer.cancel()
    if _carry_cost_timer:
        _carry_cost_timer.cancel()
    if _execution_persistence_timer:
        _execution_persistence_timer.cancel()
