# Quick OTP Number — SQLite Edition

Telegram OTP marketplace bot using **SQLite + SQLAlchemy async**. PostgreSQL/asyncpg is not required.

## Runtime
- Python 3.12+
- aiogram 3.x
- SQLAlchemy async + aiosqlite
- SQLite database: `/app/data/quickotp.db`
- Bot + background worker run together in one process (`ROLE=all`)

## Environment variables
```env
ROLE=all
BOT_TOKEN=...
GRIZZLY_API_KEY=...
GRIZZLY_BASE_URL=https://api.grizzlysms.com/stubs/handler_api.php
PERMANENT_ADMIN_ID=...
SQLITE_PATH=/app/data/quickotp.db
```

Do **not** set `DATABASE_URL` or add a PostgreSQL service.

## Railway deployment
1. Create a Railway service from this repository/ZIP.
2. The Dockerfile starts `python -m app.main`.
3. Add the variables above in **Variables**.
4. Add a **persistent volume** mounted at `/app/data`.
5. Redeploy.

The volume is important: without persistent storage, the SQLite file can be lost when the service is recreated.

## Local
```bash
pip install -e .
python -m app.main
```

The app creates all tables automatically on first startup and seeds the configured permanent admin.
