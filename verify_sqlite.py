import asyncio
from app.db.base import Base
from app.db.session import engine
import app.db.models  # noqa: F401

async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        tables = await conn.run_sync(lambda c: c.dialect.get_table_names(c))
        print(f"SQLite OK: {len(tables)} tables")
        print("\n".join(sorted(tables)))
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
