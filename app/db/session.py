from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.config import settings

DATABASE_URL = f"sqlite+aiosqlite:///{settings.sqlite_path}"
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"timeout": 30},
)

@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
