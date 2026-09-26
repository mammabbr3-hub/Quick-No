import asyncio
import os
from aiogram import Bot, Dispatcher
from app.config import settings
from app.logging import setup_logging
from app.bot.router import router as user_router
from app.admin.router import router as admin_router
from app.db.session import engine, SessionLocal
from app.db.base import Base
from app.db.seed import seed
import app.db.models  # noqa: F401 - register all models with Base

async def migrate_and_seed() -> None:
    # SQLite is file-based. create_all is intentionally used here instead of
    # database/Alembic-specific startup locking. The bot and worker run in
    # one process, so they share one database file and do not race migrations.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        await seed(session)
        await session.commit()

async def run_all() -> None:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required")
    if not settings.grizzly_api_key:
        raise RuntimeError("GRIZZLY_API_KEY is required")

    await migrate_and_seed()
    bot = Bot(settings.bot_token)
    dp = Dispatcher()
    dp.include_router(admin_router)
    dp.include_router(user_router)

    from app.worker import poll_orders, expire_deposits, scheduled_messages, daily_reports, process_broadcasts
    tasks = [
        asyncio.create_task(poll_orders(bot)),
        asyncio.create_task(expire_deposits()),
        asyncio.create_task(scheduled_messages(bot)),
        asyncio.create_task(daily_reports(bot)),
        asyncio.create_task(process_broadcasts(bot)),
        asyncio.create_task(dp.start_polling(bot)),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()
        await engine.dispose()

async def main() -> None:
    setup_logging(settings.log_level)
    role = os.getenv("ROLE", "all").lower()
    if role not in {"all", "bot", "worker"}:
        raise RuntimeError("ROLE must be all, bot or worker")
    # SQLite deployment uses ROLE=all by default. Legacy bot/worker modes are
    # retained for local compatibility, but a single process is recommended.
    if role == "all":
        await run_all()
    elif role == "bot":
        if not settings.bot_token:
            raise RuntimeError("BOT_TOKEN is required")
        await migrate_and_seed()
        bot = Bot(settings.bot_token)
        dp = Dispatcher()
        dp.include_router(admin_router)
        dp.include_router(user_router)
        try:
            await dp.start_polling(bot)
        finally:
            await bot.session.close()
            await engine.dispose()
    else:
        if not settings.bot_token or not settings.grizzly_api_key:
            raise RuntimeError("BOT_TOKEN and GRIZZLY_API_KEY are required")
        await migrate_and_seed()
        bot = Bot(settings.bot_token)
        from app.worker import poll_orders, expire_deposits, scheduled_messages, daily_reports, process_broadcasts
        tasks = [asyncio.create_task(fn(bot) if fn is not expire_deposits else fn()) for fn in [poll_orders, expire_deposits, scheduled_messages, daily_reports, process_broadcasts]]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks: task.cancel()
            await bot.session.close()
            await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
