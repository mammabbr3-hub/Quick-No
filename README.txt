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

--- Mobile Digital Hub: Super Admin Controls & Audit Channel ---

This upgrade adds:

1. SUPER ADMIN CONTROL
- ADMIN_IDS are treated as Super Admins.
- Extra admins keep operational admin access but cannot change global settings, bot text, feature controls, funding-method settings, Quick OTP pricing, community destinations, or scheduled/broadcast messages.

2. AUDIT CHANNEL
- Open: ⚙️ Community Settings -> 🔐 Audit Channel.
- Create a Telegram channel manually, add the bot as an administrator, then save its numeric -100... chat ID.
- SQLite audit_log remains the source of truth.
- New audit events are delivered automatically to the Audit Channel and failed deliveries are retried.
- Existing historical rows are not replayed automatically on first activation.

3. QUICK OTP PROFIT ACTIVATION
- Grizzly can sync all available countries, but users only see countries where a Super Admin has configured a profitable price and activated the country.
- Country -> Add Profit % offers 5%, 10%, 15%, 30%, and 50%.
- Manual percentage is also supported.
- Manual selling price is supported only when it is above the live Grizzly cost.
- Super Admin can switch a configured country ACTIVE/OFF.
- User charges use the configured selling price; the difference above provider cost remains in the main Mobile Business Hub wallet economics.

4. AUTO MESSAGE DESTINATIONS
- Auto Messages now support: 🤖 Bot Users, 👥 User Group, and 📢 User Channel.
- Destination can be edited later.
- Scheduled messages are branded with Mobile Business Hub.

5. MANUAL BROADCAST
- Broadcast destination labels now include 🤖 Bot Users, plus User Group and User Channel.
- Messages are branded with Mobile Business Hub.

6. USER EXPERIENCE
- Quick OTP is the first user keyboard button.
- OTP waiting screen has a cleaner status layout.
- Welcome screen has been refreshed with clearer sections and emojis.

QUICK OTP SERVICE CATALOGUE UPGRADE
- Quick OTP now supports Grizzly's dynamic service catalogue instead of WhatsApp-only.
- Super Admin can sync the full Grizzly service list, then configure each service's countries and user pricing.
- User flow: Quick OTP -> Service -> Country -> Buy Number.
- Service/country visibility is controlled by profit_active + enabled; unconfigured provider services remain hidden from users.
- Service-specific pricing is stored independently, so WhatsApp Nigeria can have a different markup from Telegram Nigeria, etc.
- Known services receive recognizable Telegram emojis; unknown/future Grizzly services use a safe puzzle-piece fallback emoji.
- Existing WhatsApp OTP settings are migrated into the new service/country table during startup.


GRIZZLY INTEGRATION NOTES
- GRIZZLY_API_KEY is kept only in Railway environment variables; it is never embedded in bot.py.
- GRIZZLY_BASE_URL defaults to the official handler API endpoint.
- GRIZZLY_USER_AGENT can be customized without changing code.
- Service catalogue and provider stock are synced from Grizzly; users only see service/country rows that a Super Admin has activated with a selling price above provider cost.
- Country flags are generated from ISO-3166 alpha-2 codes when Grizzly supplies them. Where the legacy price response only supplies Grizzly numeric country IDs, the cached country metadata supplies the ISO code and the same flag generator creates the emoji; flags themselves are not stored as one-by-one constants.
- User and admin catalogues are paginated so the UI is not limited to an arbitrary 80/100/120 items.

--- FINAL QUICK OTP PRICING & GRIZZLY ALERTS ---

7. USER QUICK OTP FLOW
- User taps 📱 Quick OTP.
- User selects a Grizzly service (WhatsApp, Telegram, Facebook, etc.).
- User sees ONLY countries that a Super Admin has activated and that currently have a selling price above the live Grizzly cost.
- Every country is displayed with its flag and final USDT selling price.
- User taps a country to see the confirmation screen, then taps ✅ Buy Number to confirm the purchase.
- The purchase is charged from the existing main USDT wallet.

8. SERVICE-WIDE GLOBAL PROFIT
- Super Admin can set a global percentage for an entire service.
- Global controls support +5%, +10%, -5%, -10%, an exact percentage, and Remove/OFF.
- The global percentage applies to every country in that service that does NOT have a manual price.
- When global percentage is enabled/changed, non-manual countries are activated automatically; a country is shown to users only when its computed selling price remains above the live Grizzly cost.

9. MANUAL COUNTRY PRICE OVERRIDE
- A country can be given its own final USDT price.
- A country with a manual price is excluded from the service-wide global percentage.
- Manual price is independent and remains unchanged when the service global percentage is adjusted.
- Super Admin can remove the manual price; once removed, the country returns to the service global pricing rule.
- Manual price must remain above the current Grizzly provider cost.

10. GRIZZLY PRICE-CHANGE NOTIFICATIONS
- The bot records Grizzly provider-cost changes per service/country.
- When a live Grizzly cost increases or decreases, every configured Super Admin in ADMIN_IDS receives a Telegram notification showing the country, old cost, and new cost.
- Quick OTP Settings includes 🔔 Price Change Alerts to review recent changes.
- A background watcher checks configured OTP services every 10 minutes.
- Opening/refreshing a configured service also refreshes its live Grizzly stock/pricing, so changes are detected without waiting for the watcher.

11. SECURITY
- GRIZZLY_API_KEY is NEVER embedded in source code and must remain in Railway Variables.
- BOT_TOKEN and ADMIN_IDS are also environment variables.
- No real credentials are included in the final ZIP.

SECURITY / CONTROL AUDIT UPDATE
- Feature Control now covers built-in and legacy user handles, custom handles, and editable Work/Mail menu options.
- Custom handles and Work/Mail options can be globally enabled/disabled and restricted per user from Feature Control.
- Feature restrictions are enforced at runtime, including stale inline keyboards and active conversation flows.
- Added Admin Roles and Control Audit panels. Super Admin controls role changes and system-wide settings.
- Added server-side authorization checks to sensitive admin callback actions for withdrawals, work approvals/rejections, dashboard queues, and admin funding controls.
- Legacy Bank Details is retained only as a compatibility handle; withdrawal remains the primary payment-details workflow.
