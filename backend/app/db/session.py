from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import ROOT_DIR, get_settings


settings = get_settings()
database_url = settings.database_url

IS_SQLITE = database_url.startswith("sqlite")
IS_POSTGRESQL = database_url.startswith("postgresql")

if IS_SQLITE:
    if database_url.startswith("sqlite:///"):
        db_path = database_url.replace("sqlite:///", "")
        path = Path(db_path)
        if not path.is_absolute():
            path = ROOT_DIR / path
            database_url = f"sqlite:///{path.as_posix()}"
            db_path = str(path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    connect_args = {"check_same_thread": False, "timeout": 30}
    engine_kwargs = {"connect_args": connect_args, "future": True}
elif IS_POSTGRESQL:
    connect_args = {}
    engine_kwargs = {
        "connect_args": connect_args,
        "future": True,
        "pool_size": settings.database_pool_size,
        "max_overflow": settings.database_max_overflow,
        "pool_recycle": settings.database_pool_recycle,
        "pool_pre_ping": True,
    }
else:
    connect_args = {}
    engine_kwargs = {"connect_args": connect_args, "future": True}

engine = create_engine(database_url, **engine_kwargs)


if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
