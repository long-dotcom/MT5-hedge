import threading
from typing import Optional

from loguru import logger

from app.config.settings import get_settings
from app.db.session import SessionLocal
from app.execution.auto_closer import run_auto_close
from app.execution.auto_executor import run_auto_execute
from app.execution.carry_costs import run_carry_cost_sync
from app.execution.reconciler import run_execution_reconcile
from app.market.scanner import run_scan
from app.strategy.statistical_signal import refresh_signal_stats_cache


_timer: Optional[threading.Timer] = None
_stats_timer: Optional[threading.Timer] = None
_running = False
_stats_refreshing = False


def scanner_job() -> None:
    db = SessionLocal()
    try:
        run_scan(db)
        run_auto_execute(db)
        run_carry_cost_sync(db)
        run_auto_close(db)
        run_execution_reconcile(db)
    except Exception as exc:
        logger.exception(f"扫描任务失败: {exc}")
    finally:
        db.close()
    _schedule_next()


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
        logger.exception(f"统计线刷新任务失败: {exc}")
    finally:
        db.close()
        _stats_refreshing = False
    _schedule_next_stats()


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


def start_scheduler() -> None:
    global _running
    if not _running:
        _running = True
        _schedule_next()
        _schedule_next_stats()


def stop_scheduler() -> None:
    global _running
    _running = False
    if _timer:
        _timer.cancel()
    if _stats_timer:
        _stats_timer.cancel()
