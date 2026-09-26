MOBILE BUSINESS HUB — FINAL RAILWAY BUILD

Main runtime:
  python3 bot.py

Required Railway Variables:
  BOT_TOKEN
  ADMIN_IDS
  GRIZZLY_API_KEY
  GRIZZLY_BASE_URL (recommended: https://api.grizzlysms.com/stubs/handler_api.php)
  mobile_DB_PATH (recommended: /app/data/mobile.db)

Features included:
- USDT-only user wallet
- No user-to-user transfer system
- Referral rewards in USDT
- Fund Wallet with admin-configured currencies/rates/payment destinations
- Funding proof routed to Submission/Bank Store channels when configured
- Admin approve/decline with reason
- Configurable minimum USDT withdrawal
- User Group + User Channel join gate; admins bypass it
- Submission / Bank Store / Support are internal admin destinations
- Admin can add another admin by numeric Telegram ID
- Broadcast to all users, User Group, or User Channel
- Quick OTP using the same USDT wallet and Grizzly SMS
- Multiple/same-country activations
- Live stopwatch-based OTP cancellation: manual cancel after 5 minutes, auto-cancel after 20 minutes
- OTP arrival closes cancellation and refunds happen on cancellation/expiry/provider failure
- Quick OTP prices/countries/stock controlled separately in Quick OTP Settings

Important:
- Never paste BOT_TOKEN or GRIZZLY_API_KEY into this ZIP.
- Add the bot as admin/member of every configured Telegram group/channel.
