from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.api.router import router
from app.config.settings import enforce_runtime_security, get_settings
from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.execution.auto_closer import run_auto_close
from app.execution.carry_costs import run_carry_cost_sync
from app.execution.hedge_pool import hedge_pool
from app.execution.persistence import persist_hedge_pool_events
from app.execution.reconciler import run_execution_reconcile
from app.market.scanner import persist_scan_state, run_scan
from app.market.mt5_schedule import sync_mt5_session_templates
from app.market.mt5_tradability import refresh_mt5_tradability_cache
from app.strategy.statistical_signal import refresh_signal_stats_cache
from app.workers.market_data import market_data_manager
from app.workers.scheduler import start_scheduler, stop_scheduler


app = FastAPI(title="Hyperliquid + MT5 Hedge API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.on_event("startup")
def on_startup() -> None:
    enforce_runtime_security(get_settings())
    init_db()
    market_data_manager.start()
    market_data_manager.wait_until_seeded()
    # 中文注释：启动时先执行一次扫描，让前端首次打开就能看到样例数据。
    db = SessionLocal()
    try:
        _startup_step(db, "sync_mt5_session_templates", lambda: sync_mt5_session_templates(db, only_auto=True))
        _startup_step(db, "refresh_signal_stats_cache", lambda: refresh_signal_stats_cache(db))
        _startup_step(db, "refresh_mt5_tradability_cache", lambda: refresh_mt5_tradability_cache(db))
        _startup_step(db, "load_hedge_pool", lambda: hedge_pool.load_from_db(db))
        _startup_step(db, "run_scan", lambda: run_scan(db))
        _startup_step(db, "persist_scan_state", lambda: persist_scan_state(db))
        _startup_step(db, "run_carry_cost_sync", lambda: run_carry_cost_sync(db, force=True))
        _startup_step(db, "run_auto_close", lambda: run_auto_close(db))
        _startup_step(db, "persist_hedge_pool_events", lambda: persist_hedge_pool_events(db))
        _startup_step(db, "run_execution_reconcile", lambda: run_execution_reconcile(db))
    finally:
        db.close()
    start_scheduler()


@app.on_event("shutdown")
def on_shutdown() -> None:
    stop_scheduler()
    market_data_manager.stop()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _startup_step(db, name: str, func) -> None:
    try:
        func()
    except Exception as exc:
        db.rollback()
        logger.exception(f"启动任务失败，已跳过: {name}; {exc}")
