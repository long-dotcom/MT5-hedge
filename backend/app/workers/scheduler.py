import threading
from typing import Optional

from loguru import logger

from app.config.settings import get_settings
from app.db.session import SessionLocal
from app.execution.auto_closer import run_auto_close
from app.execution.auto_executor import run_auto_execute
from app.execution.reconciler import run_execution_reconcile
from app.market.scanner import run_scan


_timer: Optional[threading.Timer] = None
_running = False


def scanner_job() -> None:
    db = SessionLocal()
    try:
        run_scan(db)
        run_auto_execute(db)
        run_auto_close(db)
        run_execution_reconcile(db)
    except Exception as exc:
        logger.exception(f"扫描任务失败: {exc}")
    finally:
        db.close()
    _schedule_next()


def _schedule_next() -> None:
    global _timer
    if not _running:
        return
    settings = get_settings()
    interval = settings.scanner_interval_ms / 1000 if settings.scanner_interval_ms > 0 else settings.scanner_interval_seconds
    _timer = threading.Timer(max(interval, 0.05), scanner_job)
    _timer.daemon = True
    _timer.start()


def start_scheduler() -> None:
    global _running
    if not _running:
        _running = True
        _schedule_next()


def stop_scheduler() -> None:
    global _running
    _running = False
    if _timer:
        _timer.cancel()
