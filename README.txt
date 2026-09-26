MOBILE BUSINESS HUB — RAILWAY

1. Deploy this folder as a Railway service.
2. Set the variables in RAILWAY_ENV.txt in Railway Variables.
3. Do NOT put BOT_TOKEN or GRIZZLY_API_KEY inside bot.py.
4. Start command / Procfile: worker: python3 bot.py
5. Database is SQLite. No PostgreSQL is required.

Required variables:
BOT_TOKEN=your Telegram bot token
ADMIN_IDS=7517279474
GRIZZLY_API_KEY=your Grizzly API key
GRIZZLY_BASE_URL=https://api.grizzlysms.com/stubs/handler_api.php
mobile_DB_PATH=/app/data/mobile.db

Optional:
BOT_LINK=
SUPPORT_GROUP_ID=
WORK_CHANNEL_ID=
BANK_GROUP_ID=
SUPPORT_GROUP_LINK=
WORK_CHANNEL_LINK=

Community Settings can configure:
- User Group
- User Channel
- Submission Channel
- Bank Store Channel
- Support Channel

Wallet is USDT-only. User-to-user transfer is removed.
Quick OTP uses the same main USDT wallet.
