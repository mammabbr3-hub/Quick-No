 #!/usr/bin/env python3
# ================================================================
#  ✦ 𝑴𝒐𝒃𝒊𝒍𝒆 𝑫𝒊𝒈𝒊𝒕𝒂𝒍 𝑯𝒖𝒃 🤖 — TELEGRAM BOT (single-file build)
#  Digital Work + Wallet + Referral + Transaction + Withdrawal + Support
#  Config, database, financial engine, and bot handlers merged into
#  one file, as requested.
# ================================================================
#  Run:  python3 mobilebot.py
#  Config: environment variables BOT_TOKEN and ADMIN_IDS are required.
# ================================================================

# ================================================================
#  ✦ MOBILE DIGITAL HUB — CONFIG, DATABASE & CORE FINANCIAL ENGINE
# ================================================================
#  This module has NO Telegram-specific code in it. It only deals with
#  configuration, persistent storage (SQLite) and the money-moving
#  logic (wallets, ledger, withdrawals, audit log).
#
#  Keeping this separate from the bot handlers means the financial
#  engine can be unit-tested on its own, without needing a live
#  Telegram connection.
# ================================================================

import os
import json
import sqlite3
import secrets
import logging
import html
import threading
import time
import urllib.request
import urllib.parse

from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
try:
    from zoneinfo import ZoneInfo
    LAGOS_TZ = ZoneInfo("Africa/Lagos")
except Exception:
    # Some minimal / mobile Python builds ship without an IANA tzdata
    # database. Africa/Lagos has no DST, so a fixed UTC+1 offset is an
    # accurate fallback.
    LAGOS_TZ = timezone(timedelta(hours=1))

# ---------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------
# No more "except: pass" anywhere in this project. Every error is
# logged here, with tracebacks, so it can be investigated later.
# Ordinary users only ever see a friendly generic message.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("mobile_bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("mobile")


# ---------------------------------------------------------------
# CONFIG (environment variables — NEVER hardcode secrets in code)
# ---------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
BOT_LINK = os.environ.get("BOT_LINK", "").strip()
DB_PATH = os.environ.get("mobile_DB_PATH", "mobile.db").strip()

_admin_ids_raw = os.environ.get("ADMIN_IDS", "").strip()
ADMIN_IDS = {a.strip() for a in _admin_ids_raw.split(",") if a.strip()}

# Quick OTP / Grizzly integration uses the SAME SQLite database and SAME wallet as Mobile Business Hub.
GRIZZLY_API_KEY = os.environ.get("GRIZZLY_API_KEY", "").strip()
GRIZZLY_BASE_URL = os.environ.get("GRIZZLY_BASE_URL", "https://api.grizzlysms.com/stubs/handler_api.php").strip()

# ---------------------------------------------------------------
# SUPPORT GROUP / APPROVED-WORK CHANNEL
# ---------------------------------------------------------------
# Telegram bots can only send into a chat by its numeric chat_id, not
# by an invite link (t.me/+xxxx). The human-facing links below are
# shown to users; for the bot to actually DELIVER support tickets and
# approved-work posts there, the bot must be added as a member/admin
# of that group/channel, and its numeric chat_id must be set here:
#   export SUPPORT_GROUP_ID='-1001234567890'
#   export WORK_CHANNEL_ID='-1009876543210'
# If unset (or if delivery fails), the bot safely falls back to
# notifying the admins directly instead of losing the message.
SUPPORT_GROUP_LINK = os.environ.get("SUPPORT_GROUP_LINK", "").strip()
WORK_CHANNEL_LINK = os.environ.get("WORK_CHANNEL_LINK", "").strip()
_support_group_raw = os.environ.get("SUPPORT_GROUP_ID", "").strip()
SUPPORT_GROUP_ID = int(_support_group_raw) if _support_group_raw.lstrip("-").isdigit() else None
_work_channel_raw = os.environ.get("WORK_CHANNEL_ID", "").strip()
WORK_CHANNEL_ID = int(_work_channel_raw) if _work_channel_raw.lstrip("-").isdigit() else None

# Group where every ADMIN-APPROVED bank/wallet/crypto submission is
# collected in one place, exactly like SUPPORT_GROUP_ID / WORK_CHANNEL_ID
# above. Nothing is posted here until an admin taps "✅ Approve".
#   export BANK_GROUP_ID='-1004427124826'
_bank_group_raw = os.environ.get("BANK_GROUP_ID", "").strip()
BANK_GROUP_ID = int(_bank_group_raw) if _bank_group_raw.lstrip("-").isdigit() else None

if not BOT_TOKEN:
    raise SystemExit(
        "❌ BOT_TOKEN is not set.\n"
        "   Create the bot token with @BotFather and set it as an environment\n"
        "   variable, e.g.:  export BOT_TOKEN='123456:ABC-your-token'\n"
        "   (See README.md for full setup instructions.)"
    )

if not ADMIN_IDS:
    raise SystemExit(
        "❌ ADMIN_IDS is not set.\n"
        "   Set at least one Telegram numeric user ID as an admin, e.g.:\n"
        "   export ADMIN_IDS='7517279474'\n"
        "   Multiple admins can be comma-separated: '111,222,333'\n"
        "   (See README.md for full setup instructions.)"
    )

# The first admin listed is treated as the "primary" admin who receives
# system-wide notifications (new users, error alerts, bank details, etc).
PRIMARY_ADMIN = int(sorted(ADMIN_IDS)[0]) if False else int(next(iter(ADMIN_IDS)))
# NOTE: for predictable behaviour, prefer putting your main admin ID FIRST
# in the ADMIN_IDS env var and keep ADMIN_IDS ordering stable.
_admin_list_ordered = [a.strip() for a in _admin_ids_raw.split(",") if a.strip()]
if _admin_list_ordered:
    PRIMARY_ADMIN = int(_admin_list_ordered[0])

VALID_CURRENCIES = ("usdt",)
CURRENCY_LABELS = {"usdt": "USDT"}

MIN_WITHDRAWAL = {"usdt": 0.50}


def is_admin(chat_id) -> bool:
    sid = str(chat_id)
    if sid in ADMIN_IDS:
        return True
    try:
        row = fetchone("SELECT user_id FROM extra_admins WHERE user_id=?", (sid,))
        return bool(row)
    except Exception:
        return False


def is_super_admin(chat_id) -> bool:
    """Only primary/configuration admins can change system controls.
    ADMIN_IDS are the trusted super-admin list; extra_admins remain
    operational admins but cannot change global settings/messages.
    """
    return str(chat_id) in ADMIN_IDS


def super_admin_only(chat_id) -> bool:
    return is_super_admin(chat_id)


def add_extra_admin(user_id, added_by):
    sid = str(user_id).strip()
    if not sid.isdigit():
        raise ValueError("Admin ID must be a numeric Telegram user ID.")
    if sid in ADMIN_IDS:
        return False
    with db_tx() as conn:
        conn.execute("INSERT OR IGNORE INTO extra_admins(user_id,added_by,added_at) VALUES(?,?,?)", (sid,str(added_by),now_iso()))
    return True


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def lagos_parts() -> dict:
    """Current date/day/time in Africa/Lagos, for user-facing messages.
    Never show raw UTC to users — this is the single source of truth
    for 'Nigeria time' across Support, Bans, Work, etc."""
    dt = datetime.now(LAGOS_TZ)
    return {
        "date": dt.strftime("%d %b %Y"),
        "day": dt.strftime("%A"),
        "time": dt.strftime("%I:%M %p").lstrip("0") or dt.strftime("%I:%M %p"),
        "tz": "Africa/Lagos",
    }


def gen_id(prefix: str) -> str:
    """Short, unique-enough, human-shareable ID e.g. TXN-4F2A9C1B"""
    return f"{prefix}-{secrets.token_hex(4).upper()}"


# ---------------------------------------------------------------
# CUSTOM EXCEPTIONS — used so Telegram handlers can show clean,
# specific error messages instead of generic failures.
# ---------------------------------------------------------------

class InsufficientFundsError(Exception):
    def __init__(self, user_id, currency, available, requested):
        self.user_id = user_id
        self.currency = currency
        self.available = available
        self.requested = requested
        super().__init__(
            f"Insufficient {currency} balance for user {user_id}: "
            f"has {available}, needs {requested}"
        )


class WithdrawalStateError(Exception):
    """Raised when a withdrawal is not in the expected state
    (e.g. already approved/declined by someone else)."""
    pass


class SubmissionStateError(Exception):
    """Raised when a work submission is not in the expected state."""
    pass


class ActionStateError(Exception):
    """Raised when an admin/user action cannot be applied in its current state."""
    pass


# ---------------------------------------------------------------
# DATABASE CONNECTION HELPERS
# ---------------------------------------------------------------
# We open a short-lived connection per operation rather than sharing
# one connection across threads (pyTelegramBotAPI's infinity_polling
# runs handlers on worker threads). WAL mode + busy_timeout lets
# reads and writes coexist safely. Every state-changing operation
# runs inside db_tx(), which issues BEGIN IMMEDIATE to take a write
# lock up front — this is what makes "check status, then update"
# operations (like approving a withdrawal) safe against two admins
# clicking buttons at the same time.

def _connect():
    db_parent = os.path.dirname(os.path.abspath(DB_PATH))
    if db_parent:
        os.makedirs(db_parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def db_ro():
    """Connection for read-only queries. Caller must close it."""
    return _connect()


def fetchone(query, params=()):
    conn = db_ro()
    try:
        return conn.execute(query, params).fetchone()
    finally:
        conn.close()


def fetchall(query, params=()):
    conn = db_ro()
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


@contextmanager
def db_tx():
    """
    Context manager for an atomic write transaction.
    Usage:
        with db_tx() as conn:
            conn.execute(...)
            ... more statements ...
        # auto-committed on success, auto-rolled-back on any exception
    """
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------
# SCHEMA
# ---------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wallets (
    user_id     TEXT PRIMARY KEY REFERENCES users(user_id),
    usdt        REAL NOT NULL DEFAULT 0,
    approved    INTEGER NOT NULL DEFAULT 0,
    pending     INTEGER NOT NULL DEFAULT 0,
    ref_count   INTEGER NOT NULL DEFAULT 0,
    ref_usdt    REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS submissions (
    sub_id          TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    work_type       TEXT,
    sub_type        TEXT,
    file_id         TEXT,
    file_type       TEXT,
    status          TEXT NOT NULL DEFAULT 'PENDING',   -- PENDING/APPROVED/REJECTED
    created_at      TEXT NOT NULL,
    processed_at    TEXT,
    processed_by    TEXT,
    reject_reason   TEXT
);
CREATE INDEX IF NOT EXISTS idx_submissions_user ON submissions(user_id);

-- Tracks the copy of a submission's proof (photo/video/document) that
-- was sent to each admin's chat, so that once the submission is
-- approved and posted to the work channel, every admin's copy can be
-- deleted automatically instead of piling up.
CREATE TABLE IF NOT EXISTS submission_admin_msgs (
    sub_id      TEXT NOT NULL,
    admin_id    TEXT NOT NULL,
    message_id  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sub_admin_msgs_sub ON submission_admin_msgs(sub_id);

CREATE TABLE IF NOT EXISTS reject_counts (
    user_id TEXT PRIMARY KEY,
    count   INTEGER NOT NULL DEFAULT 0
);

-- Holds each user's single CURRENT, ADMIN-APPROVED payment method —
-- the "gathered in one place" record. Only ever written by
-- approve_bank_submission(), so anything reading this table (e.g. the
-- withdrawal gate) only ever sees approved details.
CREATE TABLE IF NOT EXISTS bank_details (
    user_id     TEXT PRIMARY KEY,
    category    TEXT,          -- bank / wallet / crypto
    method      TEXT,
    details     TEXT,
    updated_at  TEXT
);

-- Every bank/wallet/crypto detail a user submits, PENDING until an
-- admin approves or declines it. Mirrors the submissions/work-proof
-- approval flow above.
CREATE TABLE IF NOT EXISTS bank_submissions (
    bank_id         TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    category        TEXT NOT NULL,   -- bank / wallet / crypto
    method          TEXT NOT NULL,   -- e.g. "GTBank", "OPay", "Binance"
    details         TEXT NOT NULL,   -- account number & name, or wallet/ID
    status          TEXT NOT NULL DEFAULT 'PENDING',   -- PENDING/APPROVED/DECLINED
    created_at      TEXT NOT NULL,
    processed_at    TEXT,
    processed_by    TEXT,
    decline_reason  TEXT
);
CREATE INDEX IF NOT EXISTS idx_bank_submissions_user ON bank_submissions(user_id);

-- Tracks the copy of a pending bank submission sent to each admin's
-- chat, so once it's approved/declined every admin's copy can be
-- deleted automatically instead of piling up (same idea as
-- submission_admin_msgs above).
CREATE TABLE IF NOT EXISTS bank_admin_msgs (
    bank_id     TEXT NOT NULL,
    admin_id    TEXT NOT NULL,
    message_id  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bank_admin_msgs_bank ON bank_admin_msgs(bank_id);

CREATE TABLE IF NOT EXISTS paid_referrals (
    claim_id      TEXT PRIMARY KEY,   -- "<referrer_id>_<referred_id>"
    referrer_id   TEXT NOT NULL,
    referred_id   TEXT NOT NULL,
    currency      TEXT NOT NULL,
    amount        REAL NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS withdrawals (
    withdrawal_id   TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    amount          REAL NOT NULL,
    currency        TEXT NOT NULL,
    method          TEXT,
    status          TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING/APPROVED/DECLINED
    created_at      TEXT NOT NULL,
    processed_at    TEXT,
    processed_by    TEXT,
    reason          TEXT,
    txn_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals(user_id);
CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status);


CREATE TABLE IF NOT EXISTS ledger (
    txn_id          TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    type            TEXT NOT NULL,
    amount          REAL NOT NULL,
    currency        TEXT NOT NULL,
    status          TEXT NOT NULL,
    balance_before  REAL,
    balance_after   REAL,
    related_user    TEXT,
    related_txn     TEXT,
    reason          TEXT,
    processed_by    TEXT,
    created_at      TEXT NOT NULL,
    processed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id);

CREATE TABLE IF NOT EXISTS extra_admins (
    user_id TEXT PRIMARY KEY,
    added_by TEXT NOT NULL,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id           TEXT NOT NULL,
    action             TEXT NOT NULL,
    target_user        TEXT,
    amount             REAL,
    txn_id             TEXT,
    reason             TEXT,
    created_at         TEXT NOT NULL,
    channel_sent       INTEGER NOT NULL DEFAULT 0,
    channel_message_id INTEGER
);

CREATE TABLE IF NOT EXISTS fsm_state (
    chat_id     TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS support_tickets (
    ticket_id     TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    complaint     TEXT,
    media_type    TEXT,
    media_file_id TEXT,
    status        TEXT NOT NULL DEFAULT 'OPEN',
    created_at    TEXT NOT NULL,
    resolved_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_support_tickets_user ON support_tickets(user_id);

-- Admin-adjustable limits (minimum withdrawal, etc.)
-- so they can be changed anytime via the "⚙️ Settings" panel instead
-- of being fixed in code.
CREATE TABLE IF NOT EXISTS fund_methods (
    method_id     TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    currency_code TEXT NOT NULL,
    rate_usdt     REAL NOT NULL,
    destination   TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_fund_methods_active ON fund_methods(active);

CREATE TABLE IF NOT EXISTS fund_requests (
    request_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    method_id       TEXT NOT NULL,
    currency_code   TEXT NOT NULL,
    amount_currency REAL NOT NULL,
    usdt_amount     REAL NOT NULL,
    proof_file_id   TEXT NOT NULL,
    proof_type      TEXT NOT NULL DEFAULT 'photo',
    status          TEXT NOT NULL DEFAULT 'PENDING',
    created_at      TEXT NOT NULL,
    processed_at    TEXT,
    processed_by    TEXT,
    decline_reason  TEXT,
    credited_txn_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_fund_requests_status ON fund_requests(status);
CREATE INDEX IF NOT EXISTS idx_fund_requests_user ON fund_requests(user_id);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT,
    updated_by  TEXT
);

-- Scheduled/recurring broadcast messages the admin manages from the
-- "⏰ Auto Messages" panel: each has a title (for the admin's own
-- reference), the message body, and a daily send time.
CREATE TABLE IF NOT EXISTS auto_messages (
    auto_id        TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    body           TEXT NOT NULL,
    hour           INTEGER NOT NULL,
    minute         INTEGER NOT NULL,
    active         INTEGER NOT NULL DEFAULT 1,
    created_by     TEXT,
    created_at     TEXT NOT NULL,
    last_sent_date TEXT,
    target_type    TEXT NOT NULL DEFAULT 'bot_users',
    target_value   TEXT
);

-- Admin-defined extra reply-keyboard buttons, added and edited entirely
-- from inside the bot (see "➕ Add Custom Handle" in the admin menu).
-- config_json holds whatever action_type needs: {"text": "..."} for
-- static_message, {"prompt": "..."} for forward_to_admin, or
-- {"url": "...", "text": "..."} for link_button — plus, for every
-- type, {"audience": "all"|"specific", "user_ids": [...]}.
CREATE TABLE IF NOT EXISTS custom_handles (
    handle_id   TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    action_type TEXT NOT NULL,
    config_json TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_by  TEXT,
    created_at  TEXT NOT NULL
);

-- Admin-editable options inside the "📤 Submit Work" and
-- "🛒 BUY OR SELL MAIL" menus. section is one of:
--   'work_category'  (top-level work type, e.g. "🔵 Facebook Work")
--   'work_subtype'   (a sub-choice under a work_category — parent_key
--                     holds that category's option_id)
--   'mail_option'    (a BUY/SELL MAIL choice)
-- The admin can add, rename, disable, or delete any row from inside
-- the bot — no code changes, no redeploy. Seeded with the original
-- built-in choices the first time the bot starts (see init_db).
CREATE TABLE IF NOT EXISTS menu_options (
    option_id   TEXT PRIMARY KEY,
    section     TEXT NOT NULL,
    parent_key  TEXT,
    label       TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1,
    created_by  TEXT,
    created_at  TEXT NOT NULL
);
"""

# ---------------------------------------------------------------
# BACKWARD-COMPATIBLE MIGRATIONS
# ---------------------------------------------------------------
# These ALTER TABLE statements add new columns to tables that may
# already exist (and already contain data) from before this upgrade.
# Each is wrapped so that running it again — or against a brand-new
# database that already has the column via CREATE TABLE — is a safe
# no-op. Existing balances/rows are never touched.
_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN username TEXT",
    "ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN ban_reason TEXT",
    "ALTER TABLE users ADD COLUMN banned_by TEXT",
    "ALTER TABLE users ADD COLUMN banned_by_username TEXT",
    "ALTER TABLE users ADD COLUMN banned_at TEXT",

    "ALTER TABLE bank_details ADD COLUMN category TEXT",
    "ALTER TABLE auto_messages ADD COLUMN interval_minutes INTEGER",
    "ALTER TABLE auto_messages ADD COLUMN last_sent_at TEXT",
    "ALTER TABLE auto_messages ADD COLUMN target_type TEXT NOT NULL DEFAULT 'bot_users'",
    "ALTER TABLE auto_messages ADD COLUMN target_value TEXT",
    "ALTER TABLE audit_log ADD COLUMN channel_sent INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE audit_log ADD COLUMN channel_message_id INTEGER",
]


def _migrate_legacy_finance_schema(conn):
    # Remove legacy Naira wallet fields while preserving USDT/referral balances.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(wallets)").fetchall()}
    if "naira" in cols or "ref_naira" in cols:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("ALTER TABLE wallets RENAME TO wallets_legacy_mbh")
        conn.execute("""CREATE TABLE wallets (
            user_id TEXT PRIMARY KEY REFERENCES users(user_id),
            usdt REAL NOT NULL DEFAULT 0,
            approved INTEGER NOT NULL DEFAULT 0,
            pending INTEGER NOT NULL DEFAULT 0,
            ref_count INTEGER NOT NULL DEFAULT 0,
            ref_usdt REAL NOT NULL DEFAULT 0
        )""")
        conn.execute("""INSERT INTO wallets(user_id,usdt,approved,pending,ref_count,ref_usdt)
                       SELECT user_id,COALESCE(usdt,0),COALESCE(approved,0),COALESCE(pending,0),
                              COALESCE(ref_count,0),COALESCE(ref_usdt,0)
                       FROM wallets_legacy_mbh""")
        conn.execute("DROP TABLE wallets_legacy_mbh")
        conn.execute("PRAGMA foreign_keys=ON")
    # Transfer system is removed; old transfer records are not used by this bot.
    conn.execute("DROP TABLE IF EXISTS transfers")


def init_db():
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
                conn.commit()
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    logger.exception("Migration failed: %s", stmt)
        _migrate_legacy_finance_schema(conn)
        # Existing audit history predates the Audit Channel dispatcher.
        # Mark that old history as archived exactly once; future unsent rows
        # remain durable and will be retried if Telegram delivery fails.
        try:
            boot = conn.execute("SELECT value FROM settings WHERE key='audit_channel_bootstrap_v1'").fetchone()
            if boot is None:
                conn.execute("UPDATE audit_log SET channel_sent=1 WHERE channel_sent=0")
                conn.execute("INSERT INTO settings(key,value,updated_at,updated_by) VALUES('audit_channel_bootstrap_v1','1',?, 'SYSTEM')", (now_iso(),))
                conn.commit()
        except Exception:
            logger.exception("Could not initialize audit channel backlog marker")
        _seed_default_menu_options(conn)
        logger.info("Database ready at %s", DB_PATH)
    finally:
        conn.close()


# ---------------------------------------------------------------
# DEFAULT MENU OPTIONS — only inserted the very first time (each row
# has a fixed, stable option_id, so re-running this on every startup
# is a safe no-op via INSERT OR IGNORE; anything the admin later
# edits/renames/disables/deletes stays exactly as the admin left it).
# ---------------------------------------------------------------

def _seed_default_menu_options(conn):
    if conn.execute("SELECT 1 FROM menu_options LIMIT 1").fetchone():
        return  # Already seeded (or admin already cleared it) — never overwrite.
    now = now_iso()
    rows = [
        ("WCAT-FB", "work_category", None, "🔵 Facebook Work", 1),
        ("WCAT-IG", "work_category", None, "🟠 Instagram Work", 2),
        ("WSUB-FB1", "work_subtype", "WCAT-FB", "🆔 Webmail 00frnd 2FA", 1),
        ("WSUB-FB2", "work_subtype", "WCAT-FB", "🆔 Hotmail 30frnd 2FA", 2),
        ("WSUB-FB3", "work_subtype", "WCAT-FB", "🆔 Any Mail 00frnd 2FA", 3),
        ("WSUB-FB4", "work_subtype", "WCAT-FB", "🆔 Facebook Cookies", 4),
        ("WSUB-FB5", "work_subtype", "WCAT-FB", "🆔 Swich account old", 5),
        ("WSUB-IG1", "work_subtype", "WCAT-IG", "🆔 Instagram 2FA", 1),
        ("WSUB-IG2", "work_subtype", "WCAT-IG", "🆔 Instagram Cookies", 2),
        ("MAIL-SELLG", "mail_option", None, "SELL GMAIL", 1),
        ("MAIL-BUYG", "mail_option", None, "BUY GMAIL", 2),
        ("MAIL-OUTLOOK", "mail_option", None, "OUTLOOK MAIL", 3),
        ("MAIL-BUYH", "mail_option", None, "BUY HOTMAIL", 4),
    ]
    for option_id, section, parent_key, label, position in rows:
        conn.execute(
            "INSERT OR IGNORE INTO menu_options (option_id, section, parent_key, label, "
            "position, active, created_by, created_at) VALUES (?,?,?,?,?,1,?,?)",
            (option_id, section, parent_key, label, position, "system", now),
        )
    conn.commit()


# ---------------------------------------------------------------
# FSM (conversation state) — persisted to SQLite so a bot restart
# never loses a user's place mid-flow (mid-withdrawal,
# mid-admin-task, etc). This replaces the old plain Python dicts.
# ---------------------------------------------------------------

def get_state(chat_id) -> dict:
    row = fetchone("SELECT data FROM fsm_state WHERE chat_id=?", (str(chat_id),))
    if not row:
        return {}
    try:
        return json.loads(row["data"])
    except (json.JSONDecodeError, TypeError):
        logger.warning("Corrupt fsm_state for chat %s, resetting", chat_id)
        return {}


def set_state(chat_id, data: dict):
    with db_tx() as conn:
        conn.execute(
            "INSERT INTO fsm_state (chat_id, data, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (str(chat_id), json.dumps(data), now_iso()),
        )


def update_state(chat_id, **kwargs) -> dict:
    s = get_state(chat_id)
    s.update(kwargs)
    set_state(chat_id, s)
    return s


def clear_state(chat_id):
    with db_tx() as conn:
        conn.execute("DELETE FROM fsm_state WHERE chat_id=?", (str(chat_id),))


# ---------------------------------------------------------------
# USERS & WALLETS
# ---------------------------------------------------------------

def ensure_wallet(conn, user_id):
    user_id = str(user_id)
    row = conn.execute("SELECT user_id FROM wallets WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO wallets (user_id, usdt, approved, pending, ref_count, ref_usdt) "
            "VALUES (?,0,0,0,0,0)",
            (user_id,),
        )


def ensure_user(conn, user_id, name, username=None) -> bool:
    """Returns True if this is a newly created user."""
    user_id = str(user_id)
    row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
    is_new = row is None
    if is_new:
        conn.execute(
            "INSERT INTO users (user_id, name, username, created_at) VALUES (?,?,?,?)",
            (user_id, name, username, now_iso()),
        )
        ensure_wallet(conn, user_id)
    else:
        # Keep name/username fresh (Telegram users can change either).
        conn.execute(
            "UPDATE users SET name=?, username=? WHERE user_id=?",
            (name, username, user_id),
        )
    return is_new


def get_user(user_id):
    return fetchone("SELECT * FROM users WHERE user_id=?", (str(user_id),))


def user_exists(user_id) -> bool:
    return get_user(user_id) is not None


def display_username(user_row) -> str:
    if user_row and user_row["username"]:
        return f"@{user_row['username']}"
    return "Not set"


# ---------------------------------------------------------------
# BAN / UNBAN
# ---------------------------------------------------------------

def is_banned(user_id) -> bool:
    row = fetchone("SELECT banned FROM users WHERE user_id=?", (str(user_id),))
    return bool(row and row["banned"])


def get_ban_info(user_id):
    return fetchone(
        "SELECT banned, ban_reason, banned_by, banned_by_username, banned_at "
        "FROM users WHERE user_id=?", (str(user_id),)
    )


def ban_user(user_id, admin_id, admin_username, reason):
    user_id = str(user_id)
    with db_tx() as conn:
        row = conn.execute("SELECT user_id, banned FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            raise ValueError("That User ID does not exist in the system.")
        if row["banned"]:
            raise ActionStateError("This user is already banned.")
        conn.execute(
            "UPDATE users SET banned=1, ban_reason=?, banned_by=?, banned_by_username=?, "
            "banned_at=? WHERE user_id=?",
            (reason, str(admin_id), admin_username, now_iso(), user_id),
        )
        conn.execute(
            "INSERT INTO audit_log (admin_id, action, target_user, amount, txn_id, "
            "reason, created_at) VALUES (?,?,?,?,?,?,?)",
            (str(admin_id), "USER_BANNED", user_id, None, None, reason, now_iso()),
        )


def unban_user(user_id, admin_id):
    user_id = str(user_id)
    with db_tx() as conn:
        row = conn.execute("SELECT user_id, banned FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            raise ValueError("That User ID does not exist in the system.")
        if not row["banned"]:
            raise ActionStateError("This user is not currently banned.")
        conn.execute(
            "UPDATE users SET banned=0, ban_reason=NULL, banned_by=NULL, "
            "banned_by_username=NULL, banned_at=NULL WHERE user_id=?",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO audit_log (admin_id, action, target_user, amount, txn_id, "
            "reason, created_at) VALUES (?,?,?,?,?,?,?)",
            (str(admin_id), "USER_UNBANNED", user_id, None, None, None, now_iso()),
        )


def get_banned_users(offset=0, limit=8):
    return fetchall(
        "SELECT * FROM users WHERE banned=1 ORDER BY banned_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )


def count_banned_users():
    row = fetchone("SELECT COUNT(*) AS c FROM users WHERE banned=1")
    return row["c"] if row else 0


def get_users_page(offset=0, limit=8):
    return fetchall(
        "SELECT u.user_id, u.name, u.username, u.banned, w.usdt "
        "FROM users u LEFT JOIN wallets w ON w.user_id = u.user_id "
        "ORDER BY u.created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )


def count_all_users():
    row = fetchone("SELECT COUNT(*) AS c FROM users")
    return row["c"] if row else 0


def search_users_by_name(query, limit=8):
    like = f"%{query}%"
    return fetchall(
        "SELECT u.user_id, u.name, u.username, u.banned, w.usdt "
        "FROM users u LEFT JOIN wallets w ON w.user_id = u.user_id "
        "WHERE u.name LIKE ? OR u.username LIKE ? OR u.user_id = ? "
        "ORDER BY u.created_at DESC LIMIT ?",
        (like, like, query, limit),
    )


def get_wallet(user_id):
    row = fetchone("SELECT * FROM wallets WHERE user_id=?", (str(user_id),))
    if row is None:
        # Self-heal: create the user/wallet rows if somehow missing.
        with db_tx() as conn:
            ensure_user(conn, user_id, "Unknown")
        row = fetchone("SELECT * FROM wallets WHERE user_id=?", (str(user_id),))
    return row


# ---------------------------------------------------------------
# CORE FINANCIAL ENGINE
# ---------------------------------------------------------------
# Every function below either fully succeeds (balance updated AND a
# ledger row written) or fully fails (nothing changes) — enforced by
# running inside db_tx(). This is what prevents "sender charged but
# receiver not credited" type bugs.

def adjust_balance(conn, user_id, currency, delta, txn_type, *, reason=None,
                    related_user=None, related_txn=None, processed_by=None,
                    status="COMPLETED", allow_negative=False):
    """
    Atomically changes a wallet balance and writes a ledger entry.
    MUST be called with a connection obtained from db_tx().
    """
    if currency not in VALID_CURRENCIES:
        raise ValueError(f"Invalid currency: {currency}")

    user_id = str(user_id)
    row = conn.execute(f"SELECT {currency} FROM wallets WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        raise ValueError(f"No wallet found for user {user_id}")

    balance_before = row[0]
    balance_after = balance_before + delta

    if delta < 0 and balance_after < -1e-9 and not allow_negative:
        raise InsufficientFundsError(user_id, currency, balance_before, -delta)

    conn.execute(f"UPDATE wallets SET {currency}=? WHERE user_id=?", (balance_after, user_id))

    txn_id = gen_id("TXN")
    conn.execute(
        "INSERT INTO ledger (txn_id, user_id, type, amount, currency, status, "
        "balance_before, balance_after, related_user, related_txn, reason, "
        "processed_by, created_at, processed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (txn_id, user_id, txn_type, delta, currency, status, balance_before, balance_after,
         str(related_user) if related_user is not None else None,
         related_txn, reason, str(processed_by) if processed_by is not None else None,
         now_iso(), now_iso()),
    )
    return {
        "txn_id": txn_id, "user_id": user_id, "type": txn_type, "amount": delta,
        "currency": currency, "status": status,
        "balance_before": balance_before, "balance_after": balance_after,
    }


def _change_wallet_locked(conn, user_id, currency, delta, txn_type, reason, related_txn=None, processed_by=None):
    return adjust_balance(conn, user_id, currency, delta, txn_type, reason=reason, related_txn=related_txn, processed_by=processed_by)


def create_withdrawal(user_id, currency, amount, method):
    """
    Creates a PENDING withdrawal and reserves the funds immediately
    (deducts from available balance) so the user can't spend the same
    money twice while the request is awaiting admin review.
    """
    user_id = str(user_id)
    if amount is None or amount <= 0:
        raise ValueError("Amount must be a positive number.")
    if currency not in VALID_CURRENCIES:
        raise ValueError("Invalid currency.")

    with db_tx() as conn:
        reserved = adjust_balance(
            conn, user_id, currency, -amount, "WITHDRAWAL_REQUEST",
            reason="Withdrawal requested — funds reserved pending admin review",
            processed_by=user_id,
        )
        wd_id = gen_id("WD")
        conn.execute(
            "INSERT INTO withdrawals (withdrawal_id, user_id, amount, currency, "
            "method, status, created_at, txn_id) VALUES (?,?,?,?,?,?,?,?)",
            (wd_id, user_id, amount, currency, method, "PENDING", now_iso(), reserved["txn_id"]),
        )
    return wd_id


def approve_withdrawal(withdrawal_id, admin_id):
    """
    Finalizes a PENDING withdrawal. Funds were already reserved at
    request time, so this just flips the status — but only if it is
    still PENDING (double-processing protection).
    """
    with db_tx() as conn:
        row = conn.execute(
            "SELECT * FROM withdrawals WHERE withdrawal_id=?", (withdrawal_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Withdrawal not found.")
        if row["status"] != "PENDING":
            raise WithdrawalStateError(f"This withdrawal is already {row['status']}.")

        cur = conn.execute(
            "UPDATE withdrawals SET status='APPROVED', processed_at=?, processed_by=? "
            "WHERE withdrawal_id=? AND status='PENDING'",
            (now_iso(), str(admin_id), withdrawal_id),
        )
        if cur.rowcount == 0:
            raise WithdrawalStateError("This withdrawal was just processed by another admin.")

        conn.execute(
            "INSERT INTO ledger (txn_id, user_id, type, amount, currency, status, "
            "related_txn, processed_by, created_at, processed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (gen_id("TXN"), row["user_id"], "WITHDRAWAL_APPROVED", row["amount"],
             row["currency"], "COMPLETED", row["txn_id"], str(admin_id), now_iso(), now_iso()),
        )
        conn.execute(
            "INSERT INTO audit_log (admin_id, action, target_user, amount, txn_id, "
            "reason, created_at) VALUES (?,?,?,?,?,?,?)",
            (str(admin_id), "WITHDRAWAL_APPROVED", row["user_id"], row["amount"],
             row["txn_id"], None, now_iso()),
        )
    return row


def decline_withdrawal(withdrawal_id, admin_id, reason):
    """
    Declines a PENDING withdrawal and returns the reserved funds to
    the user's available balance. Only works if still PENDING.
    """
    with db_tx() as conn:
        row = conn.execute(
            "SELECT * FROM withdrawals WHERE withdrawal_id=?", (withdrawal_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Withdrawal not found.")
        if row["status"] != "PENDING":
            raise WithdrawalStateError(f"This withdrawal is already {row['status']}.")

        cur = conn.execute(
            "UPDATE withdrawals SET status='DECLINED', processed_at=?, processed_by=?, "
            "reason=? WHERE withdrawal_id=? AND status='PENDING'",
            (now_iso(), str(admin_id), reason, withdrawal_id),
        )
        if cur.rowcount == 0:
            raise WithdrawalStateError("This withdrawal was just processed by another admin.")

        refund = adjust_balance(
            conn, row["user_id"], row["currency"], row["amount"], "WITHDRAWAL_DECLINED",
            reason=reason, related_txn=row["txn_id"], processed_by=str(admin_id),
        )
        conn.execute(
            "INSERT INTO audit_log (admin_id, action, target_user, amount, txn_id, "
            "reason, created_at) VALUES (?,?,?,?,?,?,?)",
            (str(admin_id), "WITHDRAWAL_DECLINED", row["user_id"], row["amount"],
             refund["txn_id"], reason, now_iso()),
        )
    return row


def admin_adjust_funds(target_user_id, currency, delta, admin_id, reason):
    """Admin manually adds or removes funds. Always ledgered and audited."""
    with db_tx() as conn:
        target = conn.execute(
            "SELECT user_id FROM users WHERE user_id=?", (str(target_user_id),)
        ).fetchone()
        if target is None:
            raise ValueError("That User ID does not exist in the system.")

        txn_type = "ADMIN_ADD_FUNDS" if delta > 0 else "ADMIN_MINUS_FUNDS"
        entry = adjust_balance(
            conn, target_user_id, currency, delta, txn_type,
            reason=reason, processed_by=admin_id,
        )
        conn.execute(
            "INSERT INTO audit_log (admin_id, action, target_user, amount, txn_id, "
            "reason, created_at) VALUES (?,?,?,?,?,?,?)",
            (str(admin_id), txn_type, str(target_user_id), delta, entry["txn_id"],
             reason, now_iso()),
        )
    return entry


def claim_referral_reward(referrer_id, referred_id, currency):
    """
    Pays a referral reward exactly once per (referrer, referred) pair.
    Raises ValueError if already claimed.
    """
    if not is_referral_enabled():
        raise ValueError("Referral rewards are currently switched off by the admin.")

    referrer_id, referred_id = str(referrer_id), str(referred_id)
    claim_id = f"{referrer_id}_{referred_id}"
    amount = get_referral_amount(currency)

    with db_tx() as conn:
        existing = conn.execute(
            "SELECT claim_id FROM paid_referrals WHERE claim_id=?", (claim_id,)
        ).fetchone()
        if existing:
            raise ValueError("This referral reward was already claimed.")

        ensure_wallet(conn, referrer_id)
        entry = adjust_balance(
            conn, referrer_id, currency, amount, "REFERRAL_REWARD",
            related_user=referred_id, processed_by=referrer_id, allow_negative=True,
        )
        conn.execute(
            "UPDATE wallets SET ref_count = ref_count + 1, "
            f"ref_{currency} = ref_{currency} + ? WHERE user_id=?",
            (amount, referrer_id),
        )
        conn.execute(
            "INSERT INTO paid_referrals (claim_id, referrer_id, referred_id, currency, "
            "amount, created_at) VALUES (?,?,?,?,?,?)",
            (claim_id, referrer_id, referred_id, currency, amount, now_iso()),
        )
    return entry, amount


# ---------------------------------------------------------------
# WORK SUBMISSIONS
# ---------------------------------------------------------------

def create_submission(sub_id, user_id, work_type, sub_type, file_id, file_type):
    with db_tx() as conn:
        conn.execute(
            "INSERT INTO submissions (sub_id, user_id, work_type, sub_type, file_id, "
            "file_type, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (sub_id, str(user_id), work_type, sub_type, file_id, file_type,
             "PENDING", now_iso()),
        )
        conn.execute(
            "INSERT INTO reject_counts (user_id, count) VALUES (?, 0) "
            "ON CONFLICT(user_id) DO NOTHING",
            (str(user_id),),
        )
        conn.execute(
            "UPDATE wallets SET pending = pending + 1 WHERE user_id=?", (str(user_id),)
        )


def approve_submission(sub_id, admin_id):
    with db_tx() as conn:
        row = conn.execute("SELECT * FROM submissions WHERE sub_id=?", (sub_id,)).fetchone()
        if row is None:
            raise ValueError("Submission not found.")
        if row["status"] != "PENDING":
            raise SubmissionStateError(f"This submission is already {row['status']}.")

        cur = conn.execute(
            "UPDATE submissions SET status='APPROVED', processed_at=?, processed_by=? "
            "WHERE sub_id=? AND status='PENDING'",
            (now_iso(), str(admin_id), sub_id),
        )
        if cur.rowcount == 0:
            raise SubmissionStateError("This submission was just processed by another admin.")

        conn.execute("UPDATE wallets SET approved = approved + 1, pending = MAX(pending - 1, 0) "
                      "WHERE user_id=?", (row["user_id"],))
        conn.execute(
            "UPDATE reject_counts SET count = 0 WHERE user_id=?", (row["user_id"],)
        )
        conn.execute(
            "INSERT INTO audit_log (admin_id, action, target_user, amount, txn_id, "
            "reason, created_at) VALUES (?,?,?,?,?,?,?)",
            (str(admin_id), "WORK_APPROVED", row["user_id"], None, None, None, now_iso()),
        )
    return row


def reject_submission(sub_id, admin_id, reason):
    with db_tx() as conn:
        row = conn.execute("SELECT * FROM submissions WHERE sub_id=?", (sub_id,)).fetchone()
        if row is None:
            raise ValueError("Submission not found.")
        if row["status"] != "PENDING":
            raise SubmissionStateError(f"This submission is already {row['status']}.")

        cur = conn.execute(
            "UPDATE submissions SET status='REJECTED', processed_at=?, processed_by=?, "
            "reject_reason=? WHERE sub_id=? AND status='PENDING'",
            (now_iso(), str(admin_id), reason, sub_id),
        )
        if cur.rowcount == 0:
            raise SubmissionStateError("This submission was just processed by another admin.")

        conn.execute("UPDATE wallets SET pending = MAX(pending - 1, 0) WHERE user_id=?",
                      (row["user_id"],))
        conn.execute(
            "INSERT INTO reject_counts (user_id, count) VALUES (?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET count = count + 1",
            (row["user_id"],),
        )
        conn.execute(
            "INSERT INTO audit_log (admin_id, action, target_user, amount, txn_id, "
            "reason, created_at) VALUES (?,?,?,?,?,?,?)",
            (str(admin_id), "WORK_REJECTED", row["user_id"], None, None, reason, now_iso()),
        )
    return row


def get_reject_count(user_id) -> int:
    row = fetchone("SELECT count FROM reject_counts WHERE user_id=?", (str(user_id),))
    return row["count"] if row else 0


# ---------------------------------------------------------------
# BANK DETAILS
# ---------------------------------------------------------------

def save_bank_details(user_id, method, details, category=None):
    with db_tx() as conn:
        conn.execute(
            "INSERT INTO bank_details (user_id, category, method, details, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET category=excluded.category, method=excluded.method, "
            "details=excluded.details, updated_at=excluded.updated_at",
            (str(user_id), category, method, details, now_iso()),
        )


def get_bank_details(user_id):
    return fetchone("SELECT * FROM bank_details WHERE user_id=?", (str(user_id),))


# ---------------------------------------------------------------
# BANK / WALLET / CRYPTO SUBMISSIONS (pending admin approval)
# ---------------------------------------------------------------

def create_bank_submission(bank_id, user_id, category, method, details):
    with db_tx() as conn:
        conn.execute(
            "INSERT INTO bank_submissions (bank_id, user_id, category, method, details, "
            "status, created_at) VALUES (?,?,?,?,?,?,?)",
            (bank_id, str(user_id), category, method, details, "PENDING", now_iso()),
        )


def get_bank_submission(bank_id):
    return fetchone("SELECT * FROM bank_submissions WHERE bank_id=?", (bank_id,))


def get_submission(sub_id):
    return fetchone("SELECT * FROM submissions WHERE sub_id=?", (sub_id,))


def approve_bank_submission(bank_id, admin_id):
    with db_tx() as conn:
        row = conn.execute("SELECT * FROM bank_submissions WHERE bank_id=?", (bank_id,)).fetchone()
        if row is None:
            raise ValueError("Submission not found.")
        if row["status"] != "PENDING":
            raise SubmissionStateError(f"This submission is already {row['status']}.")

        cur = conn.execute(
            "UPDATE bank_submissions SET status='APPROVED', processed_at=?, processed_by=? "
            "WHERE bank_id=? AND status='PENDING'",
            (now_iso(), str(admin_id), bank_id),
        )
        if cur.rowcount == 0:
            raise SubmissionStateError("This submission was just processed by another admin.")

        # This is the "gather bank info in one place" record — only
        # ever updated here, once an admin has approved it.
        conn.execute(
            "INSERT INTO bank_details (user_id, category, method, details, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET category=excluded.category, method=excluded.method, "
            "details=excluded.details, updated_at=excluded.updated_at",
            (row["user_id"], row["category"], row["method"], row["details"], now_iso()),
        )
        conn.execute(
            "INSERT INTO audit_log (admin_id, action, target_user, amount, txn_id, "
            "reason, created_at) VALUES (?,?,?,?,?,?,?)",
            (str(admin_id), "BANK_DETAILS_APPROVED", row["user_id"], None, None, None, now_iso()),
        )
    return row


def decline_bank_submission(bank_id, admin_id, reason):
    with db_tx() as conn:
        row = conn.execute("SELECT * FROM bank_submissions WHERE bank_id=?", (bank_id,)).fetchone()
        if row is None:
            raise ValueError("Submission not found.")
        if row["status"] != "PENDING":
            raise SubmissionStateError(f"This submission is already {row['status']}.")

        cur = conn.execute(
            "UPDATE bank_submissions SET status='DECLINED', processed_at=?, processed_by=?, "
            "decline_reason=? WHERE bank_id=? AND status='PENDING'",
            (now_iso(), str(admin_id), reason, bank_id),
        )
        if cur.rowcount == 0:
            raise SubmissionStateError("This submission was just processed by another admin.")

        conn.execute(
            "INSERT INTO audit_log (admin_id, action, target_user, amount, txn_id, "
            "reason, created_at) VALUES (?,?,?,?,?,?,?)",
            (str(admin_id), "BANK_DETAILS_DECLINED", row["user_id"], None, None, reason, now_iso()),
        )
    return row


# ---------------------------------------------------------------
# SUPPORT TICKETS
# ---------------------------------------------------------------

def create_support_ticket(ticket_id, user_id, complaint=None, media_type=None, media_file_id=None):
    with db_tx() as conn:
        conn.execute(
            "INSERT INTO support_tickets (ticket_id, user_id, complaint, media_type, "
            "media_file_id, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (ticket_id, str(user_id), complaint, media_type, media_file_id, "OPEN", now_iso()),
        )


def get_support_ticket(ticket_id):
    return fetchone("SELECT * FROM support_tickets WHERE ticket_id=?", (ticket_id,))


# ---------------------------------------------------------------
# ADMIN-ADJUSTABLE SETTINGS (minimum withdrawal, USDT-only wallet settings…)
# ---------------------------------------------------------------

def get_setting(key, default=None):
    row = fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key, value, admin_id=None):
    with db_tx() as conn:
        old = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        conn.execute(
            "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (key, str(value), now_iso(), str(admin_id) if admin_id is not None else None),
        )
        if admin_id is not None and (old is None or str(old["value"]) != str(value)):
            conn.execute(
                "INSERT INTO audit_log(admin_id,action,target_user,amount,txn_id,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                (str(admin_id), "SETTING_CHANGED", None, None, key, f"{old['value'] if old else '<unset>'} -> {value}", now_iso()),
            )


def delete_setting(key):
    with db_tx() as conn:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))


# ---------------------------------------------------------------
# FUND WALLET — admin-configured payment methods, local-currency input,
# USDT-only credit, photo proof, admin approval/decline.
# ---------------------------------------------------------------
def list_fund_methods(active_only=False):
    q = "SELECT * FROM fund_methods"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY name"
    return fetchall(q)


def get_fund_method(method_id):
    return fetchone("SELECT * FROM fund_methods WHERE method_id=?", (method_id,))


def create_fund_method(method_id, name, currency_code, rate_usdt, destination, admin_id):
    with db_tx() as conn:
        conn.execute(
            "INSERT INTO fund_methods(method_id,name,currency_code,rate_usdt,destination,active,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?)",
            (method_id, name, currency_code.upper(), float(rate_usdt), destination, now_iso(), now_iso()),
        )
        conn.execute(
            "INSERT INTO audit_log(admin_id,action,target_user,amount,txn_id,reason,created_at) VALUES(?,?,?,?,?,?,?)",
            (str(admin_id), "FUND_METHOD_CREATED", None, None, method_id, f"{name} / {currency_code}", now_iso()),
        )


def toggle_fund_method(method_id, admin_id):
    with db_tx() as conn:
        row = conn.execute("SELECT active FROM fund_methods WHERE method_id=?", (method_id,)).fetchone()
        if not row:
            raise ValueError("Funding method not found.")
        new = 0 if int(row["active"]) else 1
        conn.execute("UPDATE fund_methods SET active=?,updated_at=? WHERE method_id=?", (new, now_iso(), method_id))
    return new


def delete_fund_method(method_id, admin_id):
    with db_tx() as conn:
        conn.execute("DELETE FROM fund_methods WHERE method_id=?", (method_id,))
        conn.execute(
            "INSERT INTO audit_log(admin_id,action,target_user,amount,txn_id,reason,created_at) VALUES(?,?,?,?,?,?,?)",
            (str(admin_id), "FUND_METHOD_DELETED", None, None, method_id, "Admin deleted funding method", now_iso()),
        )


def create_fund_request(request_id, user_id, method_id, currency_code, amount_currency, usdt_amount, proof_file_id, proof_type="photo"):
    with db_tx() as conn:
        conn.execute(
            "INSERT INTO fund_requests(request_id,user_id,method_id,currency_code,amount_currency,usdt_amount,proof_file_id,proof_type,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (request_id, str(user_id), method_id, currency_code.upper(), float(amount_currency), float(usdt_amount), proof_file_id, proof_type, "PENDING", now_iso()),
        )


def get_fund_request(request_id):
    return fetchone("SELECT * FROM fund_requests WHERE request_id=?", (request_id,))


def approve_fund_request(request_id, admin_id):
    with db_tx() as conn:
        row = conn.execute("SELECT * FROM fund_requests WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            raise ValueError("Funding request not found.")
        if row["status"] != "PENDING":
            raise ValueError(f"This request is already {row['status']}.")
        ensure_wallet(conn, row["user_id"])
        entry = _change_wallet_locked(
            conn, row["user_id"], "usdt", float(row["usdt_amount"]),
            "FUND_WALLET", f"Funding approved {request_id}", request_id, str(admin_id)
        )
        conn.execute(
            "UPDATE fund_requests SET status='APPROVED',processed_at=?,processed_by=?,credited_txn_id=? WHERE request_id=? AND status='PENDING'",
            (now_iso(), str(admin_id), entry["txn_id"], request_id),
        )
        return row, entry


def decline_fund_request(request_id, admin_id, reason):
    with db_tx() as conn:
        row = conn.execute("SELECT * FROM fund_requests WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            raise ValueError("Funding request not found.")
        if row["status"] != "PENDING":
            raise ValueError(f"This request is already {row['status']}.")
        conn.execute(
            "UPDATE fund_requests SET status='DECLINED',processed_at=?,processed_by=?,decline_reason=? WHERE request_id=? AND status='PENDING'",
            (now_iso(), str(admin_id), reason, request_id),
        )
        return row


def _fund_method_text(row):
    return (
        f"💳 <b>Fund With {html.escape(row['name'])}</b>\n\n"
        f"💱 Currency: <b>{html.escape(row['currency_code'])}</b>\n"
        f"💱 Rate: <b>1 {html.escape(row['currency_code'])} = {float(row['rate_usdt']):.8f} USDT</b>\n\n"
        f"📥 Send to:\n<code>{html.escape(row['destination'])}</code>\n\n"
        "After sending the money, enter the exact amount you sent."
    )


# ---------------------------------------------------------------
# FUND WALLET — admin-configured payment methods, local-currency input,
# USDT-only credit, photo proof, admin approval/decline.
# ---------------------------------------------------------------
def list_fund_methods(active_only=False):
    q = "SELECT * FROM fund_methods"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY name"
    return fetchall(q)


def get_fund_method(method_id):
    return fetchone("SELECT * FROM fund_methods WHERE method_id=?", (method_id,))


def create_fund_method(method_id, name, currency_code, rate_usdt, destination, admin_id):
    with db_tx() as conn:
        conn.execute(
            "INSERT INTO fund_methods(method_id,name,currency_code,rate_usdt,destination,active,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?)",
            (method_id, name, currency_code.upper(), float(rate_usdt), destination, now_iso(), now_iso()),
        )
        conn.execute(
            "INSERT INTO audit_log(admin_id,action,target_user,amount,txn_id,reason,created_at) VALUES(?,?,?,?,?,?,?)",
            (str(admin_id), "FUND_METHOD_CREATED", None, None, method_id, f"{name} / {currency_code}", now_iso()),
        )


def toggle_fund_method(method_id, admin_id):
    with db_tx() as conn:
        row = conn.execute("SELECT active FROM fund_methods WHERE method_id=?", (method_id,)).fetchone()
        if not row:
            raise ValueError("Funding method not found.")
        new = 0 if int(row["active"]) else 1
        conn.execute("UPDATE fund_methods SET active=?,updated_at=? WHERE method_id=?", (new, now_iso(), method_id))
    return new


def delete_fund_method(method_id, admin_id):
    with db_tx() as conn:
        conn.execute("DELETE FROM fund_methods WHERE method_id=?", (method_id,))
        conn.execute(
            "INSERT INTO audit_log(admin_id,action,target_user,amount,txn_id,reason,created_at) VALUES(?,?,?,?,?,?,?)",
            (str(admin_id), "FUND_METHOD_DELETED", None, None, method_id, "Admin deleted funding method", now_iso()),
        )


def create_fund_request(request_id, user_id, method_id, currency_code, amount_currency, usdt_amount, proof_file_id, proof_type="photo"):
    with db_tx() as conn:
        conn.execute(
            "INSERT INTO fund_requests(request_id,user_id,method_id,currency_code,amount_currency,usdt_amount,proof_file_id,proof_type,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (request_id, str(user_id), method_id, currency_code.upper(), float(amount_currency), float(usdt_amount), proof_file_id, proof_type, "PENDING", now_iso()),
        )


def get_fund_request(request_id):
    return fetchone("SELECT * FROM fund_requests WHERE request_id=?", (request_id,))


def approve_fund_request(request_id, admin_id):
    with db_tx() as conn:
        row = conn.execute("SELECT * FROM fund_requests WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            raise ValueError("Funding request not found.")
        if row["status"] != "PENDING":
            raise ValueError(f"This request is already {row['status']}.")
        ensure_wallet(conn, row["user_id"])
        entry = _change_wallet_locked(
            conn, row["user_id"], "usdt", float(row["usdt_amount"]),
            "FUND_WALLET", f"Funding approved {request_id}", request_id, str(admin_id)
        )
        conn.execute(
            "UPDATE fund_requests SET status='APPROVED',processed_at=?,processed_by=?,credited_txn_id=? WHERE request_id=? AND status='PENDING'",
            (now_iso(), str(admin_id), entry["txn_id"], request_id),
        )
        if conn.total_changes < 1:
            raise ValueError("This request was just processed by another admin.")
        return row, entry


def decline_fund_request(request_id, admin_id, reason):
    with db_tx() as conn:
        row = conn.execute("SELECT * FROM fund_requests WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            raise ValueError("Funding request not found.")
        if row["status"] != "PENDING":
            raise ValueError(f"This request is already {row['status']}.")
        conn.execute(
            "UPDATE fund_requests SET status='DECLINED',processed_at=?,processed_by=?,decline_reason=? WHERE request_id=? AND status='PENDING'",
            (now_iso(), str(admin_id), reason, request_id),
        )
        return row


def _fund_method_text(row):
    return (
        f"💳 <b>Fund With {html.escape(row['name'])}</b>\n\n"
        f"💱 Currency: <b>{html.escape(row['currency_code'])}</b>\n"
        f"💱 Rate: <b>1 {html.escape(row['currency_code'])} = {float(row['rate_usdt']):.8f} USDT</b>\n\n"
        f"📥 Send to:\n<code>{html.escape(row['destination'])}</code>\n\n"
        "After sending the money, enter the exact amount you sent."
    )


# ---------------------------------------------------------------
# EDITABLE BOT TEXT (admin can rewrite any of these from inside the
# bot, without touching code or redeploying — see "📝 Edit Bot Text"
# in the admin menu). Every editable message has a TEXT_TEMPLATES
# entry below: (label, default_template). The template is stored
# under settings key f"text:{key}" the same way every other setting
# is, and is filled in with str.format() placeholders at send time.
# ---------------------------------------------------------------

def get_text(key, default):
    val = get_setting(f"text:{key}")
    return val if val is not None else default


def render_text(key, default, **kwargs):
    """Fetch the (possibly admin-edited) template for `key` and fill
    in the given placeholders with str.format(). If an admin saved a
    broken template (typo'd placeholder, stray `{`), we fall back to
    the built-in default rather than ever crashing a handler."""
    template = get_text(key, default)
    try:
        return template.format(**kwargs)
    except Exception:
        logger.exception("Bad saved text template for %r — using built-in default instead", key)
        return default.format(**kwargs)


# Every entry here shows up in "📝 Edit Bot Text". `default` is exactly
# the wording that used to be hardcoded — editing text does not change
# behavior, only wording. Available {placeholders} are noted in the
# label so the admin knows what they can use in a replacement.
TEXT_TEMPLATES = {
    "welcome": (
        "👋 Welcome message ({full_name},{brand},{referral_line},{referral_link})",
        "✨ <b>WELCOME TO {brand}</b> ✨\n\n"
        "👋 Hello <b>{full_name}</b>!\n\n"
        "🚀 Your all-in-one digital service hub is ready.\n\n"
        "📱 Quick OTP\n"
        "💳 Fund Wallet\n"
        "💸 Withdraw\n"
        "📤 Submit Work\n"
        "🎁 Referral Rewards\n"
        "🏦 Bank / Wallet Details\n"
        "🎧 Support\n\n"
        "{referral_line}"
        "🔗 <b>Your Referral Link</b>\n<code>{referral_link}</code>\n\n"
        "👇 <b>Choose a service below to get started.</b>",
    ),
    "withdrawal_approved": (
        "🎉 Withdrawal approved ({brand},{amount},{wd_id})",
        "🎉 WITHDRAWAL APPROVED\n\n{brand}\n\n"
        "💰 Amount: {amount}\n"
        "🧾 Withdrawal ID: <code>{wd_id}</code>\n\n📊 Status: ✅ APPROVED\n\n"
        "Your withdrawal has been approved by the administration.\n\n"
        "💵 This means the funds have already been sent to your bank/wallet — "
        "please check your account now.",
    ),
    "withdrawal_declined": (
        "❌ Withdrawal declined ({brand},{amount},{wd_id},{reason})",
        "❌ WITHDRAWAL DECLINED\n\n{brand}\n\n"
        "💰 Amount: {amount}\n"
        "🧾 Withdrawal ID: <code>{wd_id}</code>\n\n📝 Reason: {reason}\n\n"
        "💰 The amount has been returned to your available balance.\n"
        "📞 If you believe this was a mistake, please contact Support.",
    ),
    "work_approved": (
        "🎉 Work approved ({sub_id},{work_type},{sub_type},{date},{day},{time},{tz},{brand})",
        "🎉 WORK APPROVED\n\n"
        "Congratulations! 🎊\n\n"
        "Your submitted work has been reviewed and approved by our agents.\n\n"
        "🧾 Submission ID: <code>{sub_id}</code>\n"
        "💼 Work: {work_type}\n"
        "📌 Type: {sub_type}\n"
        "📅 Date: {date}\n"
        "📅 Day: {day}\n"
        "⏰ Time: {time}\n"
        "🇳🇬 {tz}\n\n"
        "✅ STATUS: APPROVED\n\n"
        "Your submission has been accepted successfully.\n\n"
        "⏳ Please wait a few hours for the report to be uploaded/processed.\n\n"
        "Thank you for using {brand}. 💙",
    ),
    "work_rejected": (
        "❌ Work rejected ({sub_id},{work_type},{sub_type},{date},{day},{time},{tz},{count})",
        "❌ WORK REJECTED\n\n"
        "Your submitted work has been reviewed by our agents and unfortunately it was rejected.\n\n"
        "🧾 Submission ID: <code>{sub_id}</code>\n"
        "💼 Work: {work_type}\n"
        "📌 Type: {sub_type}\n"
        "📅 Date: {date}\n"
        "📅 Day: {day}\n"
        "⏰ Time: {time}\n"
        "🇳🇬 {tz}\n\n"
        "❌ STATUS: REJECTED\n\n"
        "If you believe this rejection is incorrect, or your submissions are being rejected "
        "repeatedly (Attempt {count}), please contact:\n\n📞 Support\n\n"
        "Our Support Team can review the situation.\n\nThank you.",
    ),
    "support_intro": (
        "📞 Support intro (no placeholders)",
        "📞 𝑴𝒐𝒃𝒊𝒍𝒆 𝑫𝒊𝒈𝒊𝒕𝒂𝒍 𝑯𝒖𝒃 SUPPORT\n\n"
        "Hello 👋\n\n"
        "We're here to help you. 💙\n\n"
        "Please write your complaint or explain the problem you're experiencing in detail.\n\n"
        "📝 Send your complaint below:\n\n"
        "You can report:\n\n"
        "🏦 Withdrawal\n"
        "💰 Balance\n"
        "📤 Work Submission\n"
        "👥 Referral\n"
        "⚙️ Account\n"
        "🛠️ Other Problems\n\n"
        "Please provide as much information as possible so our Support Team can "
        "investigate your issue quickly.\n\n"
        "📎 You can also attach a photo, video, document, or voice note.",
    ),
    "bank_approved": (
        "🎉 Bank/wallet/crypto details approved ({category},{method},{bank_id},{brand})",
        "🎉 DETAILS APPROVED\n\n"
        "Your {category} details have been reviewed and approved. ✅\n\n"
        "🏦 Method: {method}\n"
        "🧾 Reference: <code>{bank_id}</code>\n\n"
        "Thank you for using {brand}. 💙",
    ),
    "bank_declined": (
        "❌ Bank/wallet/crypto details declined ({category},{method},{reason})",
        "❌ DETAILS DECLINED\n\n"
        "Your {category} details submission could not be approved.\n\n"
        "🏦 Method: {method}\n📝 Reason: {reason}\n\n"
        "Please go to 🏦 Bank Details and submit again with correct information.\n"
        "📞 If you believe this was a mistake, please contact Support.",
    ),
}


# ---------------------------------------------------------------
# FEATURE CONTROL (admin "god-mode" switchboard)
# ---------------------------------------------------------------
# Every user-facing handle can be switched OFF globally, or OFF for
# one specific user, straight from the admin panel — no code changes,
# no redeploy. Stored in the existing `settings` table so nothing new
# is needed on Railway.

FEATURES = {
    "submit_work":   ("📤 Submit Work", "📤 Submit Work"),
    "balance":       ("💰 My Balance", "💰 My Balance"),
    "withdraw":      ("💸 Withdraw", "💸 Withdraw"),
    "referrals":     ("👥 My Referrals", "👥 My Referrals"),
    "history":       ("📜 Transaction History", "📜 Transaction History"),
    "bank_details":  ("🏦 Bank Details", "🏦 Bank Details"),
    "support":       ("📞 Support", "📞 Support"),
    "buy_sell_mail": ("🛒 BUY OR SELL MAIL", "🛒 BUY OR SELL MAIL"),
    "profile":       ("👤 My Profile", "👤 My Profile"),
}


# ---------------------------------------------------------------
# EDITABLE BUTTON LABELS (main-menu buttons only — same idea as
# "📝 Edit Bot Text" above, but for the button captions themselves
# instead of the messages they send). Overrides are stored in the
# existing `settings` table as f"btnlabel:{key}" so nothing new is
# needed on Railway. Every handler that matches one of these buttons
# checks btn_label(key) instead of a hardcoded string, so renaming a
# button here instantly updates both the keyboard AND which text the
# bot recognises as a tap on it.
# ---------------------------------------------------------------

BUTTON_LABELS = {
    "profile":       "👤 My Profile",
    "submit_work":   "📤 Submit Work",
    "balance":       "💰 My Balance",
    "withdraw":      "💸 Withdraw",
    "referrals":     "👥 My Referrals",
    "history":       "📜 Transaction History",
    "bank_details":  "🏦 Bank Details",
    "support":       "📞 Support",
    "buy_sell_mail": "🛒 BUY OR SELL MAIL",
}


def btn_label(key: str) -> str:
    return get_setting(f"btnlabel:{key}") or BUTTON_LABELS.get(key, key)


def set_btn_label(key: str, new_label: str, admin_id=None):
    set_setting(f"btnlabel:{key}", new_label, admin_id)


def reset_btn_label(key: str):
    delete_setting(f"btnlabel:{key}")


def reserved_labels_now() -> set:
    """Every label currently in use by a fixed (non-custom) button —
    both the editable main-menu ones (their CURRENT wording, not just
    the original) and the admin-only buttons that aren't editable.
    A custom handle can never reuse one of these, or it would be
    silently unreachable (the fixed handler is registered first and
    always wins)."""
    current = {btn_label(k) for k in BUTTON_LABELS}
    current |= {
        "⏰ Auto Messages", "⚙️ Settings", "✅ Unban User",
        "✉️ Message User", "➕ Add Custom Handle", "➕ Add User", "➕ Add/Minus Funds",
        "🆔 User ID Search", "🎫 Support ID Search", "🏦 Bank ID Search",
        "🏦 Banks", "👥 Users", "💱 Crypto", "💳 Pending Withdrawals",
        "💵 Withdrawal ID Search", "📊 Total Users Balance",
        "📋 Banned Users", "📋 Manage Custom Handles", "📋 Pending Approvals",
        "📝 Edit Bot Text", "📝 Submission ID Search", "📢 Broadcast",
        "🔍 Search", "🔎 Track User",
        "🔙 Back", "🚫 Ban User", "🛠 Feature Control", "🛠 Maintenance Mode", "🧩 Menu Editor", "📱 Quick OTP", "📱 Quick OTP Settings", "⚙️ Community Settings", "/start",
    }
    try:
        current |= {r["label"] for r in fetchall("SELECT label FROM menu_options WHERE active=1")}
    except Exception:
        logger.exception("Failed to load dynamic menu option labels for reserved-label check")
    return current


def is_feature_enabled(feature_key, user_id=None) -> bool:
    if get_setting(f"feature_off:{feature_key}") == "1":
        return False
    if user_id is not None and get_setting(f"userfeature_off:{feature_key}:{user_id}") == "1":
        return False
    return True


def set_feature_global(feature_key, enabled: bool, admin_id=None):
    if enabled:
        delete_setting(f"feature_off:{feature_key}")
    else:
        set_setting(f"feature_off:{feature_key}", "1", admin_id)


def set_feature_for_user(feature_key, user_id, enabled: bool, admin_id=None):
    key = f"userfeature_off:{feature_key}:{user_id}"
    if enabled:
        delete_setting(key)
    else:
        set_setting(key, "1", admin_id)


def feature_blocked_message(m, feature_key) -> bool:
    """Call at the top of a user-facing handler. Returns True (and
    replies to the user) if this feature is currently switched off for
    them, either globally or specifically for their account."""
    if not is_feature_enabled(feature_key, m.chat.id):
        bot.send_message(
            m.chat.id,
            "🚫 This feature is currently unavailable. Please contact Support if you "
            "believe this is a mistake.",
        )
        return True
    return False


def get_min_withdrawal(currency):
    val = get_setting(f"min_withdrawal_{currency}")
    return float(val) if val is not None else MIN_WITHDRAWAL[currency]


def get_referral_amount(currency):
    val = get_setting(f"referral_amount_{currency}")
    if val is not None:
        return float(val)
    return 0.00706


def is_referral_enabled():
    return get_setting("referral_enabled", "1") != "0"


# ---------------------------------------------------------------
# AUTO MESSAGES (admin-scheduled recurring broadcasts)
# ---------------------------------------------------------------

def create_auto_message(auto_id, title, body, hour, minute, admin_id, interval_minutes=None, target_type='bot_users', target_value=None):
    with db_tx() as conn:
        conn.execute(
            "INSERT INTO auto_messages (auto_id, title, body, hour, minute, active, "
            "created_by, created_at, interval_minutes, last_sent_at, target_type, target_value) VALUES (?,?,?,?,?,1,?,?,?,?,?,?)",
            (auto_id, title, body, hour, minute, str(admin_id), now_iso(), interval_minutes, None, target_type, target_value),
        )


def mark_auto_message_sent_at(auto_id, iso_str):
    with db_tx() as conn:
        conn.execute("UPDATE auto_messages SET last_sent_at=? WHERE auto_id=?", (iso_str, auto_id))


def get_auto_message(auto_id):
    return fetchone("SELECT * FROM auto_messages WHERE auto_id=?", (auto_id,))


def list_auto_messages():
    return fetchall("SELECT * FROM auto_messages ORDER BY hour, minute")


def delete_auto_message(auto_id):
    with db_tx() as conn:
        conn.execute("DELETE FROM auto_messages WHERE auto_id=?", (auto_id,))


def update_auto_message(auto_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with db_tx() as conn:
        conn.execute(f"UPDATE auto_messages SET {cols} WHERE auto_id=?", (*fields.values(), auto_id))


def mark_auto_message_sent(auto_id, date_str):
    with db_tx() as conn:
        conn.execute("UPDATE auto_messages SET last_sent_date=? WHERE auto_id=?", (date_str, auto_id))


def all_user_ids():
    return [row["user_id"] for row in fetchall("SELECT user_id FROM users")]


# ---------------------------------------------------------------
# CUSTOM HANDLES (admin-defined extra reply-keyboard buttons)
# ---------------------------------------------------------------

def create_custom_handle(handle_id, label, action_type, config: dict, admin_id):
    with db_tx() as conn:
        conn.execute(
            "INSERT INTO custom_handles (handle_id, label, action_type, config_json, "
            "active, created_by, created_at) VALUES (?,?,?,?,1,?,?)",
            (handle_id, label, action_type, json.dumps(config), str(admin_id), now_iso()),
        )


def get_custom_handle(handle_id):
    return fetchone("SELECT * FROM custom_handles WHERE handle_id=?", (handle_id,))


def get_custom_handle_by_label(label):
    """Active handles only — this is what the message router checks
    against every incoming text message, so an inactive/deleted handle
    must never match."""
    return fetchone("SELECT * FROM custom_handles WHERE label=? AND active=1", (label,))


def list_custom_handles(active_only=False):
    if active_only:
        return fetchall("SELECT * FROM custom_handles WHERE active=1 ORDER BY created_at")
    return fetchall("SELECT * FROM custom_handles ORDER BY created_at")


def list_menu_options(section, parent_key=None, active_only=True):
    """section: 'work_category' | 'work_subtype' | 'mail_option'.
    For 'work_subtype', parent_key selects which category's children
    to return (its option_id)."""
    if section == "work_subtype":
        if active_only:
            return fetchall(
                "SELECT * FROM menu_options WHERE section=? AND parent_key=? AND active=1 ORDER BY position, created_at",
                (section, parent_key),
            )
        return fetchall(
            "SELECT * FROM menu_options WHERE section=? AND parent_key=? ORDER BY position, created_at",
            (section, parent_key),
        )
    if active_only:
        return fetchall("SELECT * FROM menu_options WHERE section=? AND active=1 ORDER BY position, created_at", (section,))
    return fetchall("SELECT * FROM menu_options WHERE section=? ORDER BY position, created_at", (section,))


def get_menu_option(option_id):
    return fetchone("SELECT * FROM menu_options WHERE option_id=?", (option_id,))


def get_menu_option_by_label(section, label, parent_key=None):
    if section == "work_subtype":
        return fetchone(
            "SELECT * FROM menu_options WHERE section=? AND label=? AND parent_key=? AND active=1",
            (section, label, parent_key),
        )
    return fetchone("SELECT * FROM menu_options WHERE section=? AND label=? AND active=1", (section, label))


def create_menu_option(section, label, admin_id, parent_key=None):
    option_id = gen_id("OPT")
    existing = list_menu_options(section, parent_key=parent_key, active_only=False)
    position = (max((r["position"] for r in existing), default=0) + 1)
    with db_tx() as conn:
        conn.execute(
            "INSERT INTO menu_options (option_id, section, parent_key, label, position, "
            "active, created_by, created_at) VALUES (?,?,?,?,?,1,?,?)",
            (option_id, section, parent_key, label, position, str(admin_id), now_iso()),
        )
    return option_id


def update_menu_option_label(option_id, new_label):
    with db_tx() as conn:
        conn.execute("UPDATE menu_options SET label=? WHERE option_id=?", (new_label, option_id))


def toggle_menu_option(option_id, active: bool):
    with db_tx() as conn:
        conn.execute("UPDATE menu_options SET active=? WHERE option_id=?", (1 if active else 0, option_id))


def delete_menu_option(option_id):
    """Deleting a work_category also removes its work_subtype children,
    so nothing is left dangling/orphaned in the menu."""
    with db_tx() as conn:
        conn.execute("DELETE FROM menu_options WHERE option_id=?", (option_id,))
        conn.execute("DELETE FROM menu_options WHERE section='work_subtype' AND parent_key=?", (option_id,))


def list_custom_handles_for_user(chat_id):
    """Active handles visible to this chat_id: 'all' audience handles,
    plus 'specific' ones that name this chat_id. Admins see every
    active handle regardless of audience, so they can verify what a
    restricted handle looks like."""
    handles = list_custom_handles(active_only=True)
    if is_admin(chat_id):
        return handles
    visible = []
    for h in handles:
        try:
            cfg = json.loads(h["config_json"])
        except Exception:
            cfg = {}
        if cfg.get("audience", "all") == "all":
            visible.append(h)
        elif str(chat_id) in {str(u) for u in cfg.get("user_ids", [])}:
            visible.append(h)
    return visible


def set_custom_handle_active(handle_id, active: bool):
    with db_tx() as conn:
        conn.execute("UPDATE custom_handles SET active=? WHERE handle_id=?", (1 if active else 0, handle_id))


def delete_custom_handle(handle_id):
    with db_tx() as conn:
        conn.execute("DELETE FROM custom_handles WHERE handle_id=?", (handle_id,))


def update_custom_handle_config(handle_id, config: dict):
    with db_tx() as conn:
        conn.execute("UPDATE custom_handles SET config_json=? WHERE handle_id=?", (json.dumps(config), handle_id))


# ---------------------------------------------------------------
# LEDGER / HISTORY QUERIES
# ---------------------------------------------------------------

def get_ledger_for_user(user_id, limit=15):
    return fetchall(
        "SELECT * FROM ledger WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (str(user_id), limit),
    )


def get_pending_withdrawals(limit=25):
    return fetchall(
        "SELECT * FROM withdrawals WHERE status='PENDING' ORDER BY created_at ASC LIMIT ?",
        (limit,),
    )


def get_withdrawal(withdrawal_id):
    return fetchone("SELECT * FROM withdrawals WHERE withdrawal_id=?", (withdrawal_id,))


def system_totals():
    row = fetchone("SELECT COUNT(*) AS n_users, COALESCE(SUM(usdt),0) AS total_usdt FROM wallets")
    pending = fetchone(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount) FILTER (WHERE currency='usdt'),0) AS usdt "
        "FROM withdrawals WHERE status='PENDING'"
    )
    approved = fetchone("SELECT COUNT(*) AS n FROM withdrawals WHERE status='APPROVED'")
    declined = fetchone("SELECT COUNT(*) AS n FROM withdrawals WHERE status='DECLINED'")
    return {
        "n_users": row["n_users"],
        "total_usdt": row["total_usdt"],
        "pending_withdrawals": pending["n"],
        "pending_usdt": pending["usdt"],
        "approved_withdrawals": approved["n"],
        "declined_withdrawals": declined["n"],
    }


# ---------------------------------------------------------------
# PROFILE / ADMIN LOOKUP HELPERS
# ---------------------------------------------------------------

def get_user_by_username(username: str):
    """Look up a user by their @username (case-insensitive, no '@')."""
    uname = username.lstrip("@").strip()
    if not uname:
        return None
    return fetchone(
        "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (uname,)
    )


def resolve_user_ref(ref: str):
    """Accepts either a numeric Telegram ID or an @username and returns
    the matching users row, or None if nothing matches."""
    ref = ref.strip()
    if ref.startswith("@"):
        return get_user_by_username(ref)
    if ref.isdigit():
        row = get_user(ref)
        if row:
            return row
        return None
    # No leading @ but not numeric — try it as a bare username too.
    return get_user_by_username(ref)


def get_submissions_for_user(user_id, limit=8):
    return fetchall(
        "SELECT * FROM submissions WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (str(user_id), limit),
    )


def count_submissions_by_status(user_id):
    rows = fetchall(
        "SELECT status, COUNT(*) AS n FROM submissions WHERE user_id=? GROUP BY status",
        (str(user_id),),
    )
    out = {"PENDING": 0, "APPROVED": 0, "REJECTED": 0}
    for r in rows:
        out[r["status"]] = r["n"]
    return out


def get_withdrawals_for_user(user_id, limit=8):
    return fetchall(
        "SELECT * FROM withdrawals WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (str(user_id), limit),
    )


def count_withdrawals_by_status(user_id):
    rows = fetchall(
        "SELECT status, COUNT(*) AS n FROM withdrawals WHERE user_id=? GROUP BY status",
        (str(user_id),),
    )
    out = {"PENDING": 0, "APPROVED": 0, "DECLINED": 0}
    for r in rows:
        out[r["status"]] = r["n"]
    return out


def count_all_pending():
    """Totals shown on the admin '📋 Pending Approvals' dashboard."""
    subs = fetchone("SELECT COUNT(*) AS n FROM submissions WHERE status='PENDING'")
    banks = fetchone("SELECT COUNT(*) AS n FROM bank_submissions WHERE status='PENDING'")
    wds = fetchone("SELECT COUNT(*) AS n FROM withdrawals WHERE status='PENDING'")
    return {
        "submissions": subs["n"] if subs else 0,
        "bank_submissions": banks["n"] if banks else 0,
        "withdrawals": wds["n"] if wds else 0,
    }


# ================================================================
# TELEGRAM BOT HANDLERS
# (originally mobilebot.py — merged below)
# ================================================================

import functools

import telebot
from telebot import types

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)
BRAND = "✦ Mobile Business Hub 🤖"


# ================================================================
# ERROR-SAFE HANDLER WRAPPER
# ================================================================
# Wrap every handler with this. Known financial errors get a clear,
# specific message. Anything unexpected is logged with a full
# traceback and the user gets a generic, non-technical message —
# never a stack trace or internal detail.

def _chat_id_of(update):
    if hasattr(update, "chat") and update.chat:
        return update.chat.id
    if hasattr(update, "message") and update.message:
        return update.message.chat.id
    return PRIMARY_ADMIN


# Entry points a banned user must always be able to reach: navigating
# back, /start, opening/using Support (including finishing an
# in-progress complaint), and Support. Every other
# action is blocked for a banned user with a clear restriction notice.
BAN_EXEMPT_HANDLERS = {"universal_back", "start", "support"}


def _send_ban_notice(chat_id):
    info = get_ban_info(chat_id)
    reason = (info["ban_reason"] if info and info["ban_reason"] else "Not specified") 
    bot.send_message(
        chat_id,
        "🚫 YOUR ACCOUNT HAS BEEN RESTRICTED\n\n"
        "Hello 👋\n\n"
        f"Your {BRAND} account has been temporarily restricted.\n\n"
        f"🆔 User ID: {chat_id}\n"
        f"📌 Reason:\n\n{reason}\n\n"
        "⚠️ You are currently unable to use normal bot services.\n\n"
        "If you believe this restriction was made in error or want to resolve the issue:\n\n"
        "📞 Support\n\n"
        "Our Support Team will review your case.",
        reply_markup=back_kb(),
    )


def _actor_may_proceed(chat_id, func_name) -> bool:
    """Ban gate. Admins always pass. Banned users may only reach the
    exempt entry points, or continue an already-open Support flow
    (so they can still finish filing a complaint, including media)."""
    if is_admin(chat_id):
        return True
    if not is_banned(chat_id):
        return True
    if func_name in BAN_EXEMPT_HANDLERS:
        return True
    if get_state(chat_id).get("flow") == "support":
        return True
    return False


def maintenance_enabled() -> bool:
    return get_setting("maintenance_mode", "0") == "1"


def maintenance_message(chat_id):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🔄 Try Again Later", callback_data="maintenance_try"),
        types.InlineKeyboardButton("📞 Contact Support", callback_data="maintenance_support"),
    )
    bot.send_message(
        chat_id,
        "🛠 <b>Maintenance in Progress</b>\n\n"
        "We are currently performing maintenance and system updates. "
        "Most services are temporarily unavailable.\n\n"
        "Please try again later. If you need assistance, contact Support.\n\n"
        "Thank you for your patience.",
        parse_mode="HTML",
        reply_markup=kb,
    )


def _maintenance_allowed(update, chat_id) -> bool:
    if not maintenance_enabled() or is_admin(chat_id):
        return True
    # Keep support available so users can contact the team during maintenance.
    func_name = getattr(update, "__name__", "")
    if func_name in {"support", "_handle_support_text_complaint"}:
        return True
    # Allow only the maintenance recovery callbacks while the system is paused.
    if hasattr(update, "data") and getattr(update, "data", "") in {"maintenance_try", "maintenance_support"}:
        return True
    # Once a user has entered the support flow, allow them to finish it.
    if get_state(chat_id).get("flow") == "support":
        return True
    maintenance_message(chat_id)
    return False


def safe_handler(func):
    @functools.wraps(func)
    def wrapper(update, *args, **kwargs):
        chat_id = _chat_id_of(update)
        if not _actor_may_proceed(chat_id, func.__name__):
            try:
                _send_ban_notice(chat_id)
            except Exception:
                logger.exception("Failed to send ban notice to %s", chat_id)
            return
        # System-wide maintenance gate. Admins remain fully operational;
        # users can still open and complete Support requests.
        if maintenance_enabled() and not is_admin(chat_id):
            if func.__name__ not in {"support", "_handle_support_text_complaint"}:
                data = getattr(update, "data", "")
                if data not in {"maintenance_try", "maintenance_support"} and get_state(chat_id).get("flow") != "support":
                    maintenance_message(chat_id)
                    return
        try:
            return func(update, *args, **kwargs)
        except InsufficientFundsError as e:
            bot.send_message(
                chat_id,
                f"❌ Insufficient balance.\n💰 Available: {e.available:.4f} "
                f"{e.currency.upper()}\n📝 Needed: {e.requested:.4f} {e.currency.upper()}",
            )
        except (WithdrawalStateError, SubmissionStateError, ValueError) as e:
            bot.send_message(chat_id, f"⚠️ {e}")
        except Exception:
            logger.exception("Unhandled error in handler '%s' (chat %s)", func.__name__, chat_id)
            try:
                bot.send_message(chat_id, "⚠️ Something went wrong. Please try again or contact Support.")
            except Exception:
                logger.exception("Failed to notify user %s of error", chat_id)
            try:
                bot.send_message(
                    PRIMARY_ADMIN,
                    f"🐛 Error in handler `{func.__name__}` for chat {chat_id}.\n"
                    f"Check mobile_bot.log for the full traceback.",
                )
            except Exception:
                logger.exception("Failed to notify admin of error")
    return wrapper


def notify_admins(text, **kwargs):
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, text, **kwargs)
        except Exception:
            logger.exception("Failed to notify admin %s", admin_id)


def _audit_channel_id():
    return _community_id("audit_channel")

def _audit_text(row):
    action=str(row["action"] or "AUDIT")
    amount=row["amount"]
    amount_text=f"\n💰 Amount: <b>{float(amount):.6f} USDT</b>" if amount is not None else ""
    reason=f"\n📝 Details: {html.escape(str(row['reason']))}" if row["reason"] else ""
    target=f"\n👤 User: <code>{html.escape(str(row['target_user']))}</code>" if row["target_user"] else ""
    txn=f"\n🧾 ID: <code>{html.escape(str(row['txn_id']))}</code>" if row["txn_id"] else ""
    return (f"🔐 <b>MOBILE BUSINESS HUB • AUDIT</b>\n\n"
            f"⚡ Action: <b>{html.escape(action)}</b>"
            f"{target}{txn}{amount_text}{reason}\n"
            f"👮 Actor: <code>{html.escape(str(row['admin_id']))}</code>\n"
            f"🕒 {html.escape(str(row['created_at']))}")

def audit_channel_dispatcher():
    """Deliver durable SQLite audit history to the configured channel.
    The database remains the source of truth; failed Telegram delivery is retried.
    """
    while True:
        try:
            cid=_audit_channel_id()
            if cid:
                rows=fetchall("SELECT * FROM audit_log WHERE channel_sent=0 ORDER BY id LIMIT 25")
                for row in rows:
                    try:
                        sent=bot.send_message(cid,_audit_text(row),parse_mode="HTML")
                        with db_tx() as conn:
                            conn.execute("UPDATE audit_log SET channel_sent=1,channel_message_id=? WHERE id=?",(sent.message_id,row["id"]))
                    except Exception:
                        logger.exception("Audit channel delivery failed for audit id %s",row["id"])
                        break
        except Exception:
            logger.exception("Audit channel dispatcher error")
        time.sleep(5)



# ================================================================
# COMMUNITY / INTERNAL CHANNEL SETTINGS
# ================================================================
COMMUNITY_KEYS = {
    "user_group": "community:user_group",
    "user_channel": "community:user_channel",
    "submission_channel": "community:submission_channel",
    "bank_store_channel": "community:bank_store_channel",
    "support_channel": "community:support_channel",
    "audit_channel": "community:audit_channel",
}
COMMUNITY_LABELS = {
    "user_group": "👥 User Group",
    "user_channel": "📢 User Channel",
    "submission_channel": "🔐 Submission Channel",
    "bank_store_channel": "🏦 Bank Store Channel",
    "support_channel": "🎧 Support Channel",
    "audit_channel": "🔐 Audit Channel",
}

def _community_get(kind):
    raw = get_setting(COMMUNITY_KEYS[kind], "") or ""
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        # Backward-compatible: allow a plain numeric chat id.
        return {"id": raw} if str(raw).strip() else {}

def _community_set(kind, chat_id, link="", admin_id=None):
    set_setting(COMMUNITY_KEYS[kind], json.dumps({"id": str(chat_id), "link": (link or "").strip()}), admin_id)

def _community_id(kind):
    v = _community_get(kind).get("id")
    try:
        return int(str(v)) if str(v).lstrip("-").isdigit() else None
    except Exception:
        return None

def _community_link(kind):
    return str(_community_get(kind).get("link") or "").strip()

def _community_settings_text():
    lines=["⚙️ <b>COMMUNITY SETTINGS</b>", "", "Configure where users must join and where internal business records are delivered.", ""]
    for k in COMMUNITY_KEYS:
        d=_community_get(k); cid=d.get("id") or "Not set"; link=d.get("link") or "—"
        lines.append(f"{COMMUNITY_LABELS[k]}\n🆔 <code>{html.escape(str(cid))}</code>\n🔗 {html.escape(str(link))}")
        lines.append("")
    lines.append("ℹ️ User Group + User Channel are the required join points. Internal channels are admin/business destinations and are never shown as join requirements.")
    lines.append("🔐 Audit Channel: create a private Telegram channel, add this bot as an administrator, then save its -100… chat ID above. SQLite remains the source of truth; the bot automatically delivers every new audit event to this channel and retries failed deliveries.")
    return "\n".join(lines)

@bot.message_handler(func=lambda m: m.text == "⚙️ Community Settings" and is_super_admin(m.chat.id))
@safe_handler
def admin_community_settings(m):
    if not is_super_admin(m.chat.id):
        return
    clear_state(m.chat.id)
    kb=types.InlineKeyboardMarkup()
    for k in COMMUNITY_KEYS:
        kb.add(types.InlineKeyboardButton(f"✏️ {COMMUNITY_LABELS[k]}", callback_data=f"comm_set:{k}"))
    kb.add(types.InlineKeyboardButton("🧪 Check Configuration", callback_data="comm_check_config"))
    kb.add(types.InlineKeyboardButton("🔄 Refresh", callback_data="comm_refresh"))
    bot.send_message(m.chat.id, _community_settings_text(), parse_mode="HTML", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data in ("comm_refresh","comm_check_config"))
@safe_handler
def community_refresh_cb(c):
    if not is_super_admin(c.message.chat.id): return
    if c.data == "comm_check_config":
        results=[]
        for k in COMMUNITY_KEYS:
            cid=_community_id(k)
            if not cid:
                results.append(f"❌ {COMMUNITY_LABELS[k]}: ID not set")
                continue
            try:
                ch=bot.get_chat(cid)
                results.append(f"✅ {COMMUNITY_LABELS[k]}: {ch.title or cid}")
            except Exception as e:
                results.append(f"⚠️ {COMMUNITY_LABELS[k]}: bot cannot access this chat ({cid})")
        bot.answer_callback_query(c.id, "Configuration checked.")
        bot.send_message(c.message.chat.id, "🔎 <b>COMMUNITY CHECK</b>\n\n"+"\n".join(results), parse_mode="HTML")
    else:
        bot.answer_callback_query(c.id)
    admin_community_settings(c.message)

@bot.callback_query_handler(func=lambda c: c.data.startswith("comm_set:"))
@safe_handler
def community_set_cb(c):
    if not is_super_admin(c.message.chat.id): return
    kind=c.data.split(":",1)[1]
    if kind not in COMMUNITY_KEYS: return
    clear_state(c.message.chat.id)
    update_state(c.message.chat.id, flow="community_set", step="value", kind=kind)
    bot.answer_callback_query(c.id)
    current=_community_get(kind)
    bot.send_message(c.message.chat.id,
        f"✏️ <b>{COMMUNITY_LABELS[kind]}</b>\n\n"
        "Send the Telegram numeric chat ID and optional invite/public link in one line:\n\n"
        "<code>-1001234567890 | https://t.me/example</code>\n\n"
        "For internal admin-only channels, the link is optional.\n"
        "The bot must already be a member/admin of the target chat.", parse_mode="HTML", reply_markup=back_kb())

def _handle_community_set(m,state):
    if not is_super_admin(m.chat.id): clear_state(m.chat.id); return
    kind=state.get("kind")
    raw=(m.text or "").strip()
    parts=[x.strip() for x in raw.split("|",1)]
    cid=parts[0] if parts else ""
    link=parts[1] if len(parts)>1 else ""
    if not cid.lstrip("-").isdigit() or not cid.startswith("-100"):
        bot.send_message(m.chat.id,"❌ Invalid Telegram supergroup/channel ID. It should look like <code>-1001234567890</code>.",parse_mode="HTML")
        return
    try:
        chat=bot.get_chat(int(cid))
    except Exception:
        bot.send_message(m.chat.id,"❌ Bot cannot access that chat. Add the bot to the group/channel first, then try again.")
        return
    _community_set(kind,cid,link,m.chat.id)
    clear_state(m.chat.id)
    bot.send_message(m.chat.id,f"✅ {COMMUNITY_LABELS[kind]} saved.\n\nTitle: {html.escape(chat.title or str(cid))}\nID: <code>{cid}</code>",parse_mode="HTML",reply_markup=main_menu(m.chat.id))

def _user_join_requirements():
    return [("user_group","👥 Group"),("user_channel","📢 Channel")]

def _user_is_joined(chat_id):
    missing=[]
    for kind,label in _user_join_requirements():
        cid=_community_id(kind)
        if not cid: continue
        try:
            member=bot.get_chat_member(cid, chat_id)
            status=str(member.status)
            if status in ("left","kicked","restricted") and not (status=="restricted" and getattr(member,"is_member",False)):
                missing.append((kind,label,_community_link(kind)))
        except Exception:
            missing.append((kind,label,_community_link(kind)))
    return missing

def _join_gate(chat_id):
    if is_admin(chat_id): return True
    missing=_user_is_joined(chat_id)
    if not missing: return True
    kb=types.InlineKeyboardMarkup()
    for kind,label,link in missing:
        if link:
            kb.add(types.InlineKeyboardButton(f"➡️ Join {label}", url=link))
    kb.add(types.InlineKeyboardButton("✅ I Joined — Check Again", callback_data="community_join_check"))
    bot.send_message(chat_id,"🔐 <b>Join Required</b>\n\nPlease join the required Group and Channel, then tap <b>✅ I Joined — Check Again</b>.",parse_mode="HTML",reply_markup=kb)
    return False

@bot.callback_query_handler(func=lambda c: c.data == "community_join_check")
@safe_handler
def community_join_check_cb(c):
    if _join_gate(c.message.chat.id):
        bot.answer_callback_query(c.id,"✅ Membership confirmed.")
        bot.send_message(c.message.chat.id,"✅ You have joined the required communities.",reply_markup=main_menu(c.message.chat.id))
    else:
        bot.answer_callback_query(c.id,"⚠️ You still need to join both.",show_alert=True)

# ================================================================
# MENUS
# ================================================================

def main_menu(chat_id=None):
    """Professional role-aware keyboard. Super admins control settings;
    operational admins see day-to-day admin actions only.
    """
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if is_admin(chat_id):
        kb.row("📋 Pending Approvals", "🔎 Track User")
        kb.row("🛠 Feature Control")
        if is_super_admin(chat_id):
            kb.row("🛠 Maintenance Mode")
        kb.row("📢 Broadcast", "✉️ Message User")
        kb.row("➕ Add/Minus Funds", "📊 Total Users Balance")
        kb.row("💳 Pending Withdrawals")
        kb.row("🚫 Ban User", "✅ Unban User")
        kb.row("👥 Users", "📋 Banned Users")
        if is_super_admin(chat_id):
            kb.row("➕ Add User", "➕ Add Admin")
            kb.row("🔍 Search")
            kb.row("⚙️ Settings", "💳 Fund Wallet Settings")
            kb.row("⚙️ Community Settings")
            kb.row("📱 Quick OTP Settings")
            kb.row("⏰ Auto Messages")
            kb.row("📝 Edit Bot Text", "🧩 Menu Editor")
            kb.row("➕ Add Custom Handle", "📋 Manage Custom Handles")
        else:
            kb.row("🔍 Search")
    else:
        # Quick OTP gets the first and most visible row as requested.
        kb.row("📱 Quick OTP")
        kb.row(btn_label("balance"), btn_label("withdraw"))
        kb.row(btn_label("bank_details"), btn_label("profile"))
        kb.row(btn_label("submit_work"), btn_label("history"))
        kb.row("💳 Fund Wallet", btn_label("referrals"))
        kb.row(btn_label("support"), btn_label("buy_sell_mail"))
    try:
        for pair in _rows_of_two([h["label"] for h in list_custom_handles_for_user(chat_id)]):
            kb.row(*pair)
    except Exception:
        logger.exception("Failed to load custom handles for menu (chat_id=%s)", chat_id)
    kb.row("🔄 Refresh", "/start")
    return kb


def back_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔄 Refresh", "🔙 Back")
    return kb


def fmt_amount(amount, currency="usdt"):
    return f"{float(amount):.6f} USDT"


# ================================================================
# UNIVERSAL BACK BUTTON — registered FIRST so it always wins,
# regardless of which flow a user is currently in. This is what
# guarantees nobody ever gets trapped in a multi-step process.
# ================================================================

@bot.message_handler(func=lambda m: m.text == "🔄 Refresh")
@safe_handler
def universal_refresh(m):
    # Reset any active conversation flow and rebuild the current menu from
    # fresh database/configuration state. This is a safe UI recovery action:
    # it never creates an order, charges the wallet, or changes account data.
    clear_state(m.chat.id)
    bot.send_message(
        m.chat.id,
        "🔄 <b>Refreshed</b>\n\nThe current session has been reset. Please choose an option from the menu.",
        parse_mode="HTML",
        reply_markup=main_menu(m.chat.id),
    )


@bot.message_handler(func=lambda m: m.text == "🔙 Back")
@safe_handler
def universal_back(m):
    clear_state(m.chat.id)
    bot.send_message(m.chat.id, "🏠 Main Menu", reply_markup=main_menu(m.chat.id))


# ================================================================
# START / ONBOARDING
# ================================================================

@bot.message_handler(commands=["start"])
@safe_handler
def start(msg):
    user_id = str(msg.chat.id)
    full_name = msg.from_user.first_name or "Friend"
    if msg.from_user.last_name:
        full_name += f" {msg.from_user.last_name}"
    username = msg.from_user.username or None

    command_parts = msg.text.split()
    ref_id = command_parts[1] if len(command_parts) > 1 else None

    with db_tx() as conn:
        is_new = ensure_user(conn, user_id, full_name, username)

    clear_state(msg.chat.id)

    if not _join_gate(msg.chat.id):
        return

    if is_new:
        notify_admins(f"🆕 NEW USER\n\n👤 Name: {full_name}\n🆔 ID: {user_id}")
        with db_tx() as conn:
            conn.execute("INSERT INTO audit_log(admin_id,action,target_user,amount,txn_id,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                         ("SYSTEM","NEW_USER",user_id,None,None,full_name,now_iso()))

        if ref_id and str(ref_id) != user_id and user_exists(ref_id) and is_referral_enabled():
            ref_usdt = get_referral_amount("usdt")
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(f"🪙 USDT ({fmt_amount(ref_usdt, 'usdt')})", callback_data=f"refpay_usdt_{user_id}"))
            try:
                bot.send_message(
                    ref_id,
                    f"🎊 New Referral!\n\n"
                    f"👤 {full_name} has joined {BRAND} using your link.\n"
                    "👉 Choose your reward currency below:",
                    reply_markup=kb,
                )
            except Exception:
                logger.exception("Could not notify referrer %s", ref_id)

    referral_link = f"{BOT_LINK}?start={user_id}"
    referral_line = (
        f"🎁 Referral Reward: Earn {fmt_amount(get_referral_amount('usdt'), 'usdt')} for every friend you invite!\n\n"
        if is_referral_enabled() else ""
    )
    _, _welcome_default = TEXT_TEMPLATES["welcome"]
    bot.send_message(
        msg.chat.id,
        render_text(
            "welcome", _welcome_default,
            full_name=full_name, brand=BRAND,
            referral_line=referral_line, referral_link=referral_link,
        ),
        reply_markup=main_menu(msg.chat.id),
    )


# ================================================================
# REFERRALS
# ================================================================

@bot.message_handler(func=lambda m: m.text == btn_label("referrals"))
@safe_handler
def show_referrals(m):
    if feature_blocked_message(m, "referrals"):
        return
    w = get_wallet(m.chat.id)
    referral_link = f"{BOT_LINK}?start={m.chat.id}"
    text = (
        "👥 YOUR REFERRAL STATISTICS\n\n"
        f"🔗 Your Referral Link:\n{referral_link}\n\n"
        f"👤 Total Invited: {w['ref_count']} users\n"
        f"🪙 Total Earned (USDT): {w['ref_usdt']:.6f} USDT\n\n"
        "🚀 Share your link with friends to earn more!"
    )
    bot.send_message(m.chat.id, text)


@bot.callback_query_handler(func=lambda c: c.data.startswith("refpay_"))
@safe_handler
def process_ref_payment(c):
    _, currency, referred_user_id = c.data.split("_", 2)
    if currency != "usdt":
        return bot.answer_callback_query(c.id, "Only USDT referral rewards are supported.", show_alert=True)
    referrer_id = str(c.message.chat.id)

    try:
        entry, amount = claim_referral_reward(referrer_id, referred_user_id, currency)
    except ValueError as e:
        bot.answer_callback_query(c.id, f"❌ {e}")
        bot.edit_message_text("⚠️ This referral reward was already processed.",
                               referrer_id, c.message.message_id)
        return

    reward_text = fmt_amount(amount, currency)
    bot.answer_callback_query(c.id, f"✅ Success! {reward_text} added.")
    bot.edit_message_text(
        f"✅ Reward Added!\n\n🎁 You received {reward_text} for your referral.\n"
        f"🧾 Transaction ID: {entry['txn_id']}\n\n💰 Check your balance!",
        referrer_id, c.message.message_id,
    )


# ================================================================
# BALANCE + WITHDRAWAL
# ================================================================

def withdraw_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💸 Withdraw USDT", callback_data="withdraw_usdt"))
    return kb


@bot.message_handler(func=lambda m: m.text == btn_label("balance"))
@safe_handler
def show_balance(m):
    if feature_blocked_message(m, "balance"):
        return
    w = get_wallet(m.chat.id)
    text = (
        "💳 YOUR ACCOUNT SUMMARY\n\n"
        f"✅ Approved Work: {w['approved']}\n"
        f"⏳ Pending Work: {w['pending']}\n\n"
        f"🪙 Balance (USDT): {w['usdt']:.6f} USDT\n\n"
        "👉 Select an action below:"
    )
    bot.send_message(m.chat.id, text, reply_markup=withdraw_kb())


@bot.message_handler(func=lambda m: m.text == btn_label("withdraw"))
@safe_handler
def show_withdraw_menu(m):
    if feature_blocked_message(m, "withdraw"):
        return
    w = get_wallet(m.chat.id)
    text = (
        "💸 WITHDRAW FUNDS\n\n"
        f"🪙 USDT available: {w['usdt']:.6f} USDT\n\n"
        "👉 Choose your withdrawal amount:"
    )
    bot.send_message(m.chat.id, text, reply_markup=withdraw_kb())


@bot.callback_query_handler(func=lambda c: c.data == "withdraw_usdt")
@safe_handler
def handle_withdraw_click(c):
    currency = "usdt"
    chat_id = c.message.chat.id

    bank = get_bank_details(chat_id)
    if bank is None:
        bot.answer_callback_query(c.id)
        bot.send_message(
            chat_id,
            "🏦 You need to save your payout details before requesting a withdrawal.\n\n"
            "👉 Please go to 🏦 Bank Details first, then try again.",
            reply_markup=main_menu(chat_id),
        )
        return

    if currency == "usdt" and bank["category"] != "crypto":
        bot.answer_callback_query(c.id)
        bot.send_message(
            chat_id,
            "🪙 USDT withdrawals can only be paid out to a Crypto wallet.\n\n"
            "👉 Please go to 🏦 Bank Details, select 💱 Crypto, and set/update your crypto "
            "wallet, then try again.",
            reply_markup=main_menu(chat_id),
        )
        return


    update_state(chat_id, flow="withdraw", currency=currency)
    min_amt = fmt_amount(get_min_withdrawal(currency), currency)
    bot.answer_callback_query(c.id)
    bot.send_message(
        chat_id,
        f"📝 Enter the amount of {CURRENCY_LABELS[currency]} you want to withdraw:\n"
        f"(Minimum: {min_amt})",
        reply_markup=back_kb(),
    )


def _handle_withdraw_amount(m, state):
    currency = state["currency"]
    try:
        amount = float(m.text)
    except (TypeError, ValueError):
        bot.send_message(m.chat.id, "❌ Invalid amount. Please enter a number.")
        return

    if amount <= 0:
        bot.send_message(m.chat.id, "❌ Invalid amount. Enter a positive number.")
        return

    min_val = get_min_withdrawal(currency)
    if amount < min_val:
        bot.send_message(m.chat.id, f"❌ Minimum withdrawal is {fmt_amount(min_val, currency)}.")
        return

    w = get_wallet(m.chat.id)
    if amount > w[currency]:
        bot.send_message(
            m.chat.id,
            f"❌ Insufficient balance! You only have {fmt_amount(w[currency], currency)}.",
            reply_markup=back_kb(),
        )
        return

    bank = get_bank_details(m.chat.id)
    method = f"{bank['method']}: {bank['details']}" if bank else "Not set"

    wd_id = create_withdrawal(m.chat.id, currency, amount, method)
    clear_state(m.chat.id)

    bot.send_message(
        m.chat.id,
        f"⏳ WITHDRAWAL REQUESTED\n\n{BRAND}\n\n"
        f"💰 Amount: {fmt_amount(amount, currency)}\n"
        f"🧾 Withdrawal ID: {wd_id}\n\n"
        "📊 Status: PENDING\n👑 Our team will review it shortly.",
        reply_markup=main_menu(m.chat.id),
    )

    user = get_user(m.chat.id)
    name = user["name"] if user else "Unknown"
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ APPROVE", callback_data=f"wd_approve_{wd_id}"),
        types.InlineKeyboardButton("❌ DECLINE", callback_data=f"wd_decline_{wd_id}"),
    )
    notify_admins(
        f"🔔 NEW WITHDRAWAL REQUEST\n\n{BRAND}\n\n"
        f"👤 User: {name}\n🆔 User ID: <code>{m.chat.id}</code>\n"
        f"💰 Amount: {fmt_amount(amount, currency)}\n"
        f"🏦 Payment Method: {method}\n"
        f"🧾 Withdrawal ID: <code>{wd_id}</code>\n\n⏳ Status: PENDING\n\nPlease review this request.",
        reply_markup=kb,
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("wd_approve_"))
@safe_handler
def wd_approve_cb(c):
    wd_id = c.data[len("wd_approve_"):]
    try:
        row = approve_withdrawal(wd_id, c.from_user.id)
    except WithdrawalStateError as e:
        bot.answer_callback_query(c.id, f"❌ {e}")
        return
    bot.answer_callback_query(c.id, "✅ Approved")
    bot.edit_message_text(
        f"✅ APPROVED by {c.from_user.first_name}\n\n🧾 Withdrawal ID: <code>{wd_id}</code>",
        c.message.chat.id, c.message.message_id,
        parse_mode="HTML",
    )
    _, _wda_default = TEXT_TEMPLATES["withdrawal_approved"]
    bot.send_message(
        row["user_id"],
        render_text(
            "withdrawal_approved", _wda_default,
            brand=BRAND, amount=fmt_amount(row["amount"], row["currency"]), wd_id=wd_id,
        ),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("wd_decline_"))
@safe_handler
def wd_decline_cb(c):
    wd_id = c.data[len("wd_decline_"):]
    row = get_withdrawal(wd_id)
    if row is None:
        bot.answer_callback_query(c.id, "❌ Withdrawal not found.")
        return
    if row["status"] != "PENDING":
        bot.answer_callback_query(c.id, f"❌ Already {row['status']}.")
        return
    update_state(c.from_user.id, flow="wd_decline_reason", withdrawal_id=wd_id)
    bot.answer_callback_query(c.id)
    bot.send_message(
        c.from_user.id,
        f"📝 Please enter the reason for declining withdrawal {wd_id}:",
        reply_markup=back_kb(),
    )


def _handle_wd_decline_reason(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    wd_id = state["withdrawal_id"]
    reason = m.text.strip()
    try:
        row = decline_withdrawal(wd_id, m.chat.id, reason)
    except WithdrawalStateError as e:
        bot.send_message(m.chat.id, f"❌ {e}")
        clear_state(m.chat.id)
        return

    clear_state(m.chat.id)
    bot.send_message(m.chat.id, f"❌ Marked as DECLINED.\n🧾 Withdrawal ID: <code>{wd_id}</code>",
                      reply_markup=main_menu(m.chat.id), parse_mode="HTML")
    _, _wdd_default = TEXT_TEMPLATES["withdrawal_declined"]
    bot.send_message(
        row["user_id"],
        render_text(
            "withdrawal_declined", _wdd_default,
            brand=BRAND, amount=fmt_amount(row["amount"], row["currency"]), wd_id=wd_id,
            reason=html.escape(reason),
        ),
        parse_mode="HTML",
    )


@bot.message_handler(func=lambda m: m.text == "💳 Pending Withdrawals" and is_admin(m.chat.id))
@safe_handler
def list_pending_withdrawals(m):
    rows = get_pending_withdrawals()
    if not rows:
        bot.send_message(m.chat.id, "✅ No pending withdrawals right now.")
        return
    for row in rows:
        user = get_user(row["user_id"])
        name = user["name"] if user else "Unknown"
        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("✅ APPROVE", callback_data=f"wd_approve_{row['withdrawal_id']}"),
            types.InlineKeyboardButton("❌ DECLINE", callback_data=f"wd_decline_{row['withdrawal_id']}"),
        )
        bot.send_message(
            m.chat.id,
            f"👤 {name} (<code>{row['user_id']}</code>)\n"
            f"💰 {fmt_amount(row['amount'], row['currency'])}\n"
            f"🏦 {row['method']}\n🧾 <code>{row['withdrawal_id']}</code>\n🕐 {row['created_at']}",
            reply_markup=kb,
            parse_mode="HTML",
        )


# ================================================================
# PROFILE (user) & TRACK USER (admin) — same underlying view
# ================================================================

def build_profile_text(user_id) -> str:
    user = get_user(user_id)
    if user is None:
        return "❌ User not found."
    w = get_wallet(user_id)
    sub_counts = count_submissions_by_status(user_id)
    wd_counts = count_withdrawals_by_status(user_id)

    lines = [
        "👤 PROFILE\n",
        f"🆔 User ID: <code>{user['user_id']}</code>  (tap to copy)",
        f"📛 Name: {html.escape(user['name'] or '—')}",
        f"🔗 Username: {display_username(user)}",
        f"🚫 Status: {'BANNED' if user['banned'] else 'Active'}\n",
        "💰 BALANCE",
        f"🪙 USDT: {w['usdt']:.6f}\n",
        "📤 WORK SUBMISSIONS",
        f"⏳ Pending: {sub_counts['PENDING']}   ✅ Approved: {sub_counts['APPROVED']}   ❌ Rejected: {sub_counts['REJECTED']}",
    ]

    subs = get_submissions_for_user(user_id, limit=6)
    if subs:
        lines.append("\n🧾 Recent submissions:")
        for s in subs:
            tag = {"PENDING": "⏳", "APPROVED": "✅", "REJECTED": "❌"}.get(s["status"], "•")
            line = f"{tag} <code>{s['sub_id']}</code> — {s['status']}"
            if s["status"] == "REJECTED" and s["reject_reason"]:
                line += f"\n    ↳ Reason: {html.escape(s['reject_reason'])}"
            lines.append(line)

    lines.append(
        f"\n💸 WITHDRAWALS\n"
        f"⏳ Pending: {wd_counts['PENDING']}   ✅ Approved: {wd_counts['APPROVED']}   ❌ Declined: {wd_counts['DECLINED']}"
    )
    wds = get_withdrawals_for_user(user_id, limit=6)
    if wds:
        lines.append("\n🧾 Recent withdrawals:")
        for wd in wds:
            tag = {"PENDING": "⏳", "APPROVED": "✅", "DECLINED": "❌"}.get(wd["status"], "•")
            line = (
                f"{tag} <code>{wd['withdrawal_id']}</code> — "
                f"{fmt_amount(wd['amount'], wd['currency'])} — {wd['status']}"
            )
            if wd["status"] == "DECLINED" and wd["reason"]:
                line += f"\n    ↳ Reason: {html.escape(wd['reason'])}"
            lines.append(line)

    return "\n".join(lines)


@bot.message_handler(func=lambda m: m.text == btn_label("profile"))
@safe_handler
def show_my_profile(m):
    if feature_blocked_message(m, "profile"):
        return
    bot.send_message(m.chat.id, build_profile_text(m.chat.id), parse_mode="HTML")


@bot.message_handler(func=lambda m: m.text == "🔎 Track User" and is_admin(m.chat.id))
@safe_handler
def admin_track_user_start(m):
    update_state(m.chat.id, flow="admin_track_user", step=None)
    bot.send_message(
        m.chat.id,
        "🔎 Enter the User ID or @username you want to look up:",
        reply_markup=back_kb(),
    )


def _handle_admin_track_user(m, state):
    ref = m.text.strip()
    user = resolve_user_ref(ref)
    clear_state(m.chat.id)
    if user is None:
        bot.send_message(m.chat.id, "❌ No user found with that ID/username.", reply_markup=main_menu(m.chat.id))
        return
    bot.send_message(
        m.chat.id,
        build_profile_text(user["user_id"]),
        parse_mode="HTML",
        reply_markup=main_menu(m.chat.id),
    )


# ================================================================
# ADMIN: PENDING APPROVALS DASHBOARD
# ================================================================

@bot.message_handler(func=lambda m: m.text == "📋 Pending Approvals" and is_admin(m.chat.id))
@safe_handler
def admin_pending_dashboard(m):
    counts = count_all_pending()
    text = (
        "📋 PENDING APPROVALS DASHBOARD\n\n"
        f"📤 Work Submissions: {counts['submissions']} pending\n"
        f"🏦 Bank/Wallet/Crypto Details: {counts['bank_submissions']} pending\n"
        f"💳 Withdrawals: {counts['withdrawals']} pending\n\n"
        "👉 Use the shortcuts below to jump straight to a queue."
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        f"📤 Work Submissions ({counts['submissions']})", callback_data="dash_open_submissions"))
    kb.add(types.InlineKeyboardButton(
        f"🏦 Bank Details ({counts['bank_submissions']})", callback_data="dash_open_bank"))
    kb.add(types.InlineKeyboardButton(
        f"💳 Withdrawals ({counts['withdrawals']})", callback_data="dash_open_withdrawals"))
    bot.send_message(m.chat.id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data == "dash_open_withdrawals")
@safe_handler
def dash_open_withdrawals(c):
    bot.answer_callback_query(c.id)
    list_pending_withdrawals(c.message)


@bot.callback_query_handler(func=lambda c: c.data == "dash_open_submissions")
@safe_handler
def dash_open_submissions(c):
    bot.answer_callback_query(c.id)
    rows = fetchall(
        "SELECT * FROM submissions WHERE status='PENDING' ORDER BY created_at ASC LIMIT 15"
    )
    if not rows:
        bot.send_message(c.message.chat.id, "✅ No pending work submissions right now.")
        return
    lines = ["📤 PENDING WORK SUBMISSIONS\n"]
    for r in rows:
        lines.append(f"⏳ <code>{r['sub_id']}</code> — user <code>{r['user_id']}</code> — {r['created_at']}")
    bot.send_message(c.message.chat.id, "\n".join(lines), parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data == "dash_open_bank")
@safe_handler
def dash_open_bank(c):
    bot.answer_callback_query(c.id)
    rows = fetchall(
        "SELECT * FROM bank_submissions WHERE status='PENDING' ORDER BY created_at ASC LIMIT 15"
    )
    if not rows:
        bot.send_message(c.message.chat.id, "✅ No pending bank/wallet/crypto submissions right now.")
        return
    lines = ["🏦 PENDING BANK/WALLET/CRYPTO DETAILS\n"]
    for r in rows:
        lines.append(
            f"⏳ <code>{r['bank_id']}</code> — user <code>{r['user_id']}</code> — "
            f"{r['category']}/{r['method']} — {r['created_at']}"
        )
    bot.send_message(c.message.chat.id, "\n".join(lines), parse_mode="HTML")


# ================================================================
# ADMIN: MAINTENANCE MODE
# ================================================================


def _maintenance_admin_kb():
    active = maintenance_enabled()
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton(
        "🔴 Turn OFF Maintenance" if active else "🟢 Turn ON Maintenance",
        callback_data="maintenance_toggle",
    ))
    kb.row(types.InlineKeyboardButton("🔄 Refresh Status", callback_data="maintenance_refresh"))
    return kb


def _maintenance_admin_text():
    status = "🔴 ACTIVE — users are blocked" if maintenance_enabled() else "🟢 OFF — users can access the bot"
    return (
        "🛠 <b>MAINTENANCE MODE</b>\n\n"
        f"Status: <b>{status}</b>\n\n"
        "When Maintenance Mode is ON, users cannot start transactions or use normal bot features. "
        "They will receive a maintenance notice with options to try again later or contact Support. "
        "Admins remain fully operational.\n\n"
        "Use this mode before deployments, database changes, provider updates, or other maintenance work."
    )


@bot.message_handler(func=lambda m: m.text == "🛠 Maintenance Mode" and is_super_admin(m.chat.id))
@safe_handler
def admin_maintenance_mode(m):
    clear_state(m.chat.id)
    bot.send_message(m.chat.id, _maintenance_admin_text(), parse_mode="HTML", reply_markup=_maintenance_admin_kb())


@bot.callback_query_handler(func=lambda c: c.data in ("maintenance_toggle", "maintenance_refresh"))
@safe_handler
def admin_maintenance_cb(c):
    if not is_super_admin(c.from_user.id):
        return bot.answer_callback_query(c.id, "Admin only", show_alert=True)
    if c.data == "maintenance_toggle":
        new_value = "0" if maintenance_enabled() else "1"
        set_setting("maintenance_mode", new_value, c.from_user.id)
        bot.answer_callback_query(c.id, "Maintenance mode updated.")
    else:
        bot.answer_callback_query(c.id, "Status refreshed.")
    try:
        bot.edit_message_text(_maintenance_admin_text(), c.message.chat.id, c.message.message_id, parse_mode="HTML", reply_markup=_maintenance_admin_kb())
    except Exception:
        bot.send_message(c.message.chat.id, _maintenance_admin_text(), parse_mode="HTML", reply_markup=_maintenance_admin_kb())


@bot.callback_query_handler(func=lambda c: c.data == "maintenance_try")
@safe_handler
def maintenance_try_cb(c):
    if maintenance_enabled() and not is_admin(c.from_user.id):
        bot.answer_callback_query(c.id, "Maintenance is still in progress.", show_alert=True)
        maintenance_message(c.message.chat.id)
        return
    bot.answer_callback_query(c.id, "The system is available again.")
    bot.send_message(c.message.chat.id, "✅ The system is available again. Please choose an option from the menu.", reply_markup=main_menu(c.message.chat.id))


@bot.callback_query_handler(func=lambda c: c.data == "maintenance_support")
@safe_handler
def maintenance_support_cb(c):
    if is_admin(c.from_user.id):
        return bot.answer_callback_query(c.id, "Admins can continue normally.")
    clear_state(c.message.chat.id)
    update_state(c.message.chat.id, flow="support")
    bot.answer_callback_query(c.id)
    bot.send_message(
        c.message.chat.id,
        "📞 <b>Support</b>\n\n"
        "Please describe the issue you need help with. Our support team will review your request.\n\n"
        "Send your message below:",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )


# ================================================================
# ADMIN: FEATURE CONTROL ("god-mode" switchboard)
# ================================================================

def _feature_control_kb():
    kb = types.InlineKeyboardMarkup()
    for key, (label, _) in FEATURES.items():
        on = is_feature_enabled(key)
        icon = "🟢" if on else "🔴"
        kb.row(
            types.InlineKeyboardButton(f"{icon} {label}", callback_data=f"noop"),
            types.InlineKeyboardButton(
                "🔴 Turn OFF (all users)" if on else "🟢 Turn ON (all users)",
                callback_data=f"featoggle_{key}",
            ),
        )
        kb.row(types.InlineKeyboardButton(f"🔒 Restrict one user — {label}", callback_data=f"restrictuser_{key}"))
    return kb


@bot.message_handler(func=lambda m: m.text == "🛠 Feature Control" and is_super_admin(m.chat.id))
@safe_handler
def admin_feature_control(m):
    clear_state(m.chat.id)
    bot.send_message(
        m.chat.id,
        "🛠 FEATURE CONTROL\n\n"
        "Turn any handle ON/OFF for everyone, or restrict it for one specific user only.\n"
        "🟢 = currently on for everyone   🔴 = currently off for everyone",
        reply_markup=_feature_control_kb(),
    )


@bot.callback_query_handler(func=lambda c: c.data == "noop")
@safe_handler
def noop_cb(c):
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("featoggle_"))
@safe_handler
def admin_feature_toggle_cb(c):
    if not is_super_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    key = c.data[len("featoggle_"):]
    if key not in FEATURES:
        bot.answer_callback_query(c.id, "❌ Unknown feature.")
        return
    currently_on = is_feature_enabled(key)
    set_feature_global(key, not currently_on, c.from_user.id)
    bot.answer_callback_query(c.id, "✅ Updated")
    try:
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=_feature_control_kb())
    except Exception:
        bot.send_message(c.message.chat.id, "🛠 FEATURE CONTROL", reply_markup=_feature_control_kb())


@bot.callback_query_handler(func=lambda c: c.data.startswith("restrictuser_"))
@safe_handler
def admin_restrict_user_start(c):
    if not is_super_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    key = c.data[len("restrictuser_"):]
    if key not in FEATURES:
        bot.answer_callback_query(c.id, "❌ Unknown feature.")
        return
    update_state(c.message.chat.id, flow="admin_feature_restrict", step="user_id", feature=key)
    bot.answer_callback_query(c.id)
    bot.send_message(
        c.message.chat.id,
        f"🔒 Enter the User ID or @username to restrict/unrestrict for \"{FEATURES[key][0]}\":",
        reply_markup=back_kb(),
    )


def _handle_admin_feature_restrict_user_id(m, state):
    user = resolve_user_ref(m.text.strip())
    if user is None:
        bot.send_message(m.chat.id, "❌ No user found with that ID/username.")
        return
    key = state["feature"]
    uid = user["user_id"]
    clear_state(m.chat.id)
    currently_on = is_feature_enabled(key, uid)
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🔴 Turn OFF for this user", callback_data=f"userfeat_off_{key}_{uid}"),
        types.InlineKeyboardButton("🟢 Turn ON for this user", callback_data=f"userfeat_on_{key}_{uid}"),
    )
    bot.send_message(
        m.chat.id,
        f"👤 {user['name']} (<code>{uid}</code>)\n"
        f"🛠 Feature: {FEATURES[key][0]}\n"
        f"📊 Currently: {'🟢 ON' if currently_on else '🔴 OFF'} for this user",
        parse_mode="HTML",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("userfeat_on_") or c.data.startswith("userfeat_off_"))
@safe_handler
def admin_userfeat_toggle_cb(c):
    if not is_super_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    enable = c.data.startswith("userfeat_on_")
    rest = c.data[len("userfeat_on_"):] if enable else c.data[len("userfeat_off_"):]
    key, uid = rest.rsplit("_", 1)
    if key not in FEATURES:
        bot.answer_callback_query(c.id, "❌ Unknown feature.")
        return
    set_feature_for_user(key, uid, enable, c.from_user.id)
    bot.answer_callback_query(c.id, "✅ Updated")
    bot.send_message(
        c.message.chat.id,
        f"✅ {FEATURES[key][0]} is now {'🟢 ON' if enable else '🔴 OFF'} for user <code>{uid}</code>.",
        parse_mode="HTML",
        reply_markup=main_menu(c.message.chat.id),
    )


# ================================================================
# TRANSACTION HISTORY
# ================================================================

@bot.message_handler(func=lambda m: m.text == btn_label("history"))
@safe_handler
def transaction_history(m):
    if feature_blocked_message(m, "history"):
        return
    rows = get_ledger_for_user(m.chat.id, limit=15)
    if not rows:
        bot.send_message(m.chat.id, "📜 You have no transactions yet.")
        return

    lines = ["📜 TRANSACTION HISTORY (most recent 15)\n"]
    for r in rows:
        sign = "+" if r["amount"] >= 0 else ""
        lines.append(
            f"🧾 {r['txn_id']}\n"
            f"📌 {r['type']} | {sign}{fmt_amount(r['amount'], r['currency'])}\n"
            f"📊 {r['status']} | 🕐 {r['created_at']}\n"
        )
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        bot.send_message(m.chat.id, text[i:i + 3500])


# ================================================================
# ADMIN: FUND ADJUSTMENT
# ================================================================

@bot.message_handler(func=lambda m: m.text == "➕ Add/Minus Funds" and is_admin(m.chat.id))
@safe_handler
def admin_fund_menu(m):
    update_state(m.chat.id, flow="admin_fund", step="user_id")
    bot.send_message(m.chat.id, "🔢 Please enter the User ID you want to adjust funds for:",
                      reply_markup=back_kb())


def _handle_admin_fund_user_id(m, state):
    target_id = m.text.strip()
    if not user_exists(target_id):
        bot.send_message(m.chat.id, "❌ This ID is not in the system. Please try again.")
        return
    update_state(m.chat.id, step="action", target_id=target_id)
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Add USDT", "➖ Minus USDT")
    kb.row("🔙 Back")
    bot.send_message(m.chat.id, f"Choose action for User ID: {target_id}", reply_markup=kb)


def _handle_admin_fund_action(m, state):
    valid = {"➕ Add USDT", "➖ Minus USDT"}
    if m.text not in valid:
        bot.send_message(m.chat.id, "❌ Please choose one of the options shown.")
        return
    update_state(m.chat.id, step="amount", action_text=m.text)
    bot.send_message(m.chat.id, "💰 Enter amount:", reply_markup=back_kb())


def _handle_admin_fund_amount(m, state):
    try:
        amount = float(m.text)
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        bot.send_message(m.chat.id, "❌ Enter a valid positive number.")
        return
    update_state(m.chat.id, step="reason", amount=amount)
    bot.send_message(m.chat.id, "📝 Enter reason for this transaction:", reply_markup=back_kb())


def _handle_admin_fund_reason(m, state):
    action_text = state["action_text"]
    amount = state["amount"]
    target_id = state["target_id"]
    reason = m.text.strip()

    currency = "usdt"
    delta = amount if "Add" in action_text else -amount

    entry = admin_adjust_funds(target_id, currency, delta, m.chat.id, reason)
    clear_state(m.chat.id)

    bot.send_message(
        m.chat.id,
        f"✅ Successfully updated user {target_id}.\n🧾 Transaction ID: {entry['txn_id']}",
        reply_markup=main_menu(m.chat.id),
    )
    if delta > 0:
        bot.send_message(
            target_id,
            f"🎊 CONGRATULATIONS!\n\n{BRAND}\n\n"
            f"➕ {fmt_amount(amount, currency)} has been added to your balance.\n"
            f"📝 Reason: {reason}\n🧾 Transaction ID: {entry['txn_id']}",
        )
    else:
        bot.send_message(
            target_id,
            f"⚠️ NOTICE\n\n{BRAND}\n\n"
            f"➖ {fmt_amount(amount, currency)} has been deducted from your balance.\n"
            f"📝 Reason: {reason}\n🧾 Transaction ID: {entry['txn_id']}",
        )


# ================================================================
# ADMIN: TOTAL BALANCES / DASHBOARD
# ================================================================

@bot.message_handler(func=lambda m: m.text == "📊 Total Users Balance" and is_admin(m.chat.id))
@safe_handler
def view_all_balances(m):
    t = system_totals()
    text = (
        "📊 ADMIN FINANCIAL DASHBOARD\n\n"
        f"👥 Total Users: {t['n_users']}\n"
        f"🪙 Total USDT in wallets: {t['total_usdt']:.6f} USDT\n\n"
        f"⏳ Pending Withdrawals: {t['pending_withdrawals']}\n"
        f"   └ {t['pending_usdt']:.6f} USDT\n"
        f"✅ Approved Withdrawals: {t['approved_withdrawals']}\n"
        f"❌ Declined Withdrawals: {t['declined_withdrawals']}"
    )
    bot.send_message(m.chat.id, text)


# ================================================================
# ADMIN: BAN / UNBAN USER
# ================================================================

@bot.message_handler(func=lambda m: m.text == "🚫 Ban User" and is_admin(m.chat.id))
@safe_handler
def admin_ban_start(m):
    if not is_admin(m.chat.id):
        return
    update_state(m.chat.id, flow="admin_ban", step="user_id")
    bot.send_message(m.chat.id, "🆔 Enter the Telegram User ID to ban:", reply_markup=back_kb())


def _handle_admin_ban_user_id(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    target_id = m.text.strip()
    if not user_exists(target_id):
        bot.send_message(m.chat.id, "❌ User not found.")
        return
    if is_banned(target_id):
        bot.send_message(m.chat.id, "⚠️ This user is already banned.", reply_markup=main_menu(m.chat.id))
        clear_state(m.chat.id)
        return
    update_state(m.chat.id, step="reason", target_id=target_id)
    bot.send_message(m.chat.id, "📝 Ban reason:", reply_markup=back_kb())


def _handle_admin_ban_reason(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    target_id = state["target_id"]
    reason = m.text.strip()
    admin_username = f"@{m.from_user.username}" if m.from_user.username else None
    try:
        ban_user(target_id, m.chat.id, admin_username, reason)
    except (ValueError, ActionStateError) as e:
        bot.send_message(m.chat.id, f"❌ {e}")
        clear_state(m.chat.id)
        return

    clear_state(m.chat.id)
    bot.send_message(m.chat.id, f"🚫 User {target_id} has been banned.\n📝 Reason: {reason}",
                      reply_markup=main_menu(m.chat.id))
    try:
        bot.send_message(
            target_id,
            "🚫 YOUR ACCOUNT HAS BEEN RESTRICTED\n\n"
            "Hello 👋\n\n"
            f"Your {BRAND} account has been temporarily restricted.\n\n"
            f"🆔 User ID: {target_id}\n\n"
            f"📌 Reason:\n\n{reason}\n\n"
            "⚠️ You are currently unable to use normal bot services.\n\n"
            "If you believe this restriction was made in error or want to resolve the issue:\n\n"
            "📞 Support\n\n"
            "Our Support Team will review your case.",
            reply_markup=back_kb(),
        )
    except Exception:
        logger.exception("Could not notify banned user %s", target_id)


@bot.message_handler(func=lambda m: m.text == "➕ Add User" and is_admin(m.chat.id))
@safe_handler
def admin_add_user_start(m):
    if not is_admin(m.chat.id):
        return
    clear_state(m.chat.id)
    update_state(m.chat.id, flow="admin_add_user", step="user_id")
    bot.send_message(m.chat.id, "🆔 Enter the Telegram User ID to add:", reply_markup=back_kb())


def _handle_admin_add_user_id(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    target_id = m.text.strip()
    clear_state(m.chat.id)

    if not target_id.isdigit():
        bot.send_message(
            m.chat.id,
            "❌ Invalid User ID. Please enter numbers only.",
            reply_markup=main_menu(m.chat.id),
        )
        return

    if user_exists(target_id):
        user = get_user(target_id)
        bot.send_message(
            m.chat.id,
            f"⚠️ This user already exists.\n\n"
            f"👤 Name: {user['name']}\n"
            f"🆔 User ID: {target_id}",
            reply_markup=main_menu(m.chat.id),
        )
        return

    with db_tx() as conn:
        ensure_user(conn, target_id, "Manually Added")

    total = count_all_users()

    dm_sent = True
    try:
        bot.send_message(
            target_id,
            "🎉 WELCOME!\n\n"
            "You have been added to our system.\n\n"
            "Tap /start to begin using the bot.",
        )
    except Exception:
        dm_sent = False

    confirm_text = (
        f"✅ USER ADDED\n\n"
        f"🆔 User ID: {target_id}\n"
        f"👥 Total Users: {total}"
    )
    if not dm_sent:
        confirm_text += (
            "\n\n⚠️ We could not send the welcome message. "
            "The user may not have started the bot yet."
        )

    bot.send_message(
        m.chat.id,
        confirm_text,
        reply_markup=main_menu(m.chat.id),
    )


@bot.message_handler(func=lambda m: m.text == "✅ Unban User" and is_admin(m.chat.id))
@safe_handler
def admin_unban_start(m):
    if not is_admin(m.chat.id):
        return
    update_state(m.chat.id, flow="admin_unban", step="user_id")
    bot.send_message(m.chat.id, "🆔 Enter the Telegram User ID to unban:", reply_markup=back_kb())


def _handle_admin_unban_user_id(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    target_id = m.text.strip()
    if not user_exists(target_id):
        bot.send_message(m.chat.id, "❌ User not found.")
        return
    info = get_ban_info(target_id)
    if not info or not info["banned"]:
        bot.send_message(m.chat.id, "⚠️ This user is not currently banned.", reply_markup=main_menu(m.chat.id))
        clear_state(m.chat.id)
        return

    clear_state(m.chat.id)
    user = get_user(target_id)
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ UNBAN USER", callback_data=f"unbanconfirm_{target_id}"),
        types.InlineKeyboardButton("❌ CANCEL", callback_data="txncancel"),
    )
    bot.send_message(
        m.chat.id,
        f"👤 User: {user['name'] if user else 'Unknown'}\n\n"
        f"🔗 Username: {display_username(user)}\n\n"
        f"🆔 ID: {target_id}\n\n"
        "🚫 Current Status: BANNED\n\n"
        f"📌 Ban Reason: {info['ban_reason'] or 'Not specified'}\n\n"
        f"📅 Banned At: {info['banned_at'] or 'Unknown'}",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("unbanconfirm_"))
@safe_handler
def admin_unban_confirm_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    target_id = c.data[len("unbanconfirm_"):]
    try:
        unban_user(target_id, c.message.chat.id)
    except (ValueError, ActionStateError) as e:
        bot.answer_callback_query(c.id, f"❌ {e}")
        return

    bot.answer_callback_query(c.id, "✅ Unbanned")
    bot.edit_message_text(f"✅ User {target_id} has been unbanned.", c.message.chat.id, c.message.message_id)
    try:
        bot.send_message(
            target_id,
            "✅ YOUR ACCOUNT HAS BEEN RESTORED\n\n"
            f"Your {BRAND} account restriction has been removed.\n\n"
            "You can now use the bot normally again. 🎉\n\n"
            "Thank you for your patience.",
            reply_markup=main_menu(target_id),
        )
    except Exception:
        logger.exception("Could not notify unbanned user %s", target_id)


# ================================================================
# BROADCAST
# ================================================================

@bot.message_handler(func=lambda m: m.text == "📢 Broadcast" and is_super_admin(m.chat.id))
@safe_handler
def ask_broadcast(m):
    clear_state(m.chat.id)
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🤖 Bot Users")
    kb.row("👥 User Group", "📢 User Channel")
    kb.row("🔙 Back")
    bot.send_message(m.chat.id,"📢 BROADCAST\n\nChoose the destination:",reply_markup=kb)

@bot.message_handler(func=lambda m: m.text in ("🤖 Bot Users","👥 All Users","👥 User Group","📢 User Channel") and is_admin(m.chat.id))
@safe_handler
def broadcast_target(m):
    target={"🤖 Bot Users":"all","👥 All Users":"all","👥 User Group":"user_group","📢 User Channel":"user_channel"}[m.text]
    update_state(m.chat.id,flow="admin_broadcast",step="text",target=target)
    bot.send_message(m.chat.id,"📝 Type the message to broadcast:",reply_markup=back_kb())

def _handle_admin_broadcast_text(m,state):
    update_state(m.chat.id, step="confirm", text=m.text, target=state.get("target","all"))
    labels={"all":"all users","user_group":"the User Group","user_channel":"the User Channel"}
    kb=types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ YES, SEND NOW",callback_data="confirm_broadcast"))
    kb.add(types.InlineKeyboardButton("❌ CANCEL",callback_data="cancel_admin"))
    bot.send_message(m.chat.id,f"Destination: <b>{labels.get(state.get('target','all'))}</b>\n\nMessage:\n{html.escape(m.text)}\n\nSend it?",parse_mode="HTML",reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data in ("confirm_broadcast", "cancel_admin"))
@safe_handler
def broadcast_decision_cb(c):
    chat_id=c.message.chat.id
    if not is_admin(chat_id): return bot.answer_callback_query(c.id)
    state=get_state(chat_id)
    if c.data=="cancel_admin":
        clear_state(chat_id); bot.answer_callback_query(c.id); bot.edit_message_text("❌ Cancelled.",chat_id,c.message.message_id); return
    text=state.get("text",""); target=state.get("target","all"); clear_state(chat_id)
    bot.answer_callback_query(c.id); bot.edit_message_text("⌛ Sending message...",chat_id,c.message.message_id)
    sent=0
    if target=="all":
        for row in fetchall("SELECT user_id FROM users"):
            try: bot.send_message(row["user_id"],f"📢 {BRAND}:\n\n{text}"); sent+=1
            except Exception: logger.exception("Broadcast failed to reach %s",row["user_id"])
    else:
        cid=_community_id(target)
        if not cid:
            return bot.send_message(chat_id,"❌ That community destination is not configured.",reply_markup=main_menu(chat_id))
        try: bot.send_message(cid,f"📢 {BRAND}:\n\n{text}"); sent=1
        except Exception as e: bot.send_message(chat_id,f"❌ Could not send to destination: {e}",reply_markup=main_menu(chat_id)); return
    bot.send_message(chat_id,f"✅ Broadcast sent successfully.\n📨 Delivered: {sent}",reply_markup=main_menu(chat_id))
    with db_tx() as conn:
        conn.execute("INSERT INTO audit_log(admin_id,action,target_user,amount,txn_id,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                     (str(chat_id),"BROADCAST_SENT",None,None,gen_id("MSG"),f"target={target}; delivered={sent}; {text[:300]}",now_iso()))


# ================================================================
# MESSAGE INDIVIDUAL USER
# ================================================================

@bot.message_handler(func=lambda m: m.text == "✉️ Message User" and is_admin(m.chat.id))
@safe_handler
def list_users_for_msg(m):
    users = fetchall("SELECT * FROM users ORDER BY created_at DESC LIMIT 50")
    if not users:
        bot.send_message(m.chat.id, "There are no users in the bot yet.")
        return
    lines = ["👥 RECENT USERS (latest 50):\n"]
    for u in users:
        lines.append(f"👤 {u['name']} -> ID: {u['user_id']}")
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        bot.send_message(m.chat.id, text[i:i + 3500])
    update_state(m.chat.id, flow="admin_msg", step="user_id")
    bot.send_message(m.chat.id, "🆔 Now enter the User ID you want to message:", reply_markup=back_kb())


def _handle_admin_msg_user_id(m, state):
    target_id = m.text.strip()
    if not user_exists(target_id):
        bot.send_message(m.chat.id, "❌ This ID is not in the bot.")
        return
    target = get_user(target_id)
    update_state(m.chat.id, step="body", target_id=target_id)
    bot.send_message(m.chat.id, f"Now type the message you want to send to {target['name']}:",
                      reply_markup=back_kb())


def _handle_admin_msg_body(m, state):
    target_id = state["target_id"]
    clear_state(m.chat.id)
    try:
        bot.send_message(target_id, f"📩 Message From Admin\n\n{m.text}")
        bot.send_message(m.chat.id, "✅ Message sent successfully!", reply_markup=main_menu(m.chat.id))
    except Exception:
        logger.exception("Failed to deliver admin message to %s", target_id)
        bot.send_message(m.chat.id, "❌ Error sending message. The user may have blocked the bot.",
                          reply_markup=main_menu(m.chat.id))


# ================================================================
# ADMIN: ADD ANOTHER ADMIN BY NUMERIC TELEGRAM ID
# ================================================================

@bot.message_handler(func=lambda m: m.text == "➕ Add Admin" and is_super_admin(m.chat.id))
@safe_handler
def admin_add_admin_start(m):
    clear_state(m.chat.id)
    update_state(m.chat.id, flow="admin_add_admin", step="user_id")
    bot.send_message(m.chat.id, "🛡️ Send the numeric Telegram User ID of the new admin:", reply_markup=back_kb())

def _handle_admin_add_admin(m, state):
    sid=(m.text or "").strip()
    if not sid.isdigit():
        bot.send_message(m.chat.id, "❌ Invalid ID. Send numbers only, e.g. 7517279474.", reply_markup=back_kb())
        return
    try:
        added=add_extra_admin(sid,m.chat.id)
    except Exception as e:
        clear_state(m.chat.id)
        bot.send_message(m.chat.id,f"❌ Could not add admin: {e}",reply_markup=main_menu(m.chat.id))
        return
    clear_state(m.chat.id)
    if added:
        bot.send_message(m.chat.id,f"✅ Admin added successfully.\n\n🆔 <code>{sid}</code>",parse_mode="HTML",reply_markup=main_menu(m.chat.id))
        try: bot.send_message(int(sid),f"🛡️ You have been added as an admin of {BRAND}.",reply_markup=main_menu(int(sid)))
        except Exception: pass
    else:
        bot.send_message(m.chat.id,f"ℹ️ <code>{sid}</code> is already an admin.",parse_mode="HTML",reply_markup=main_menu(m.chat.id))


# ================================================================
# ADMIN: USERS LIST (view all users, balances, copyable IDs)
# ================================================================

USERS_PAGE_SIZE = 6


def _esc(text):
    return html.escape(str(text)) if text is not None else ""


def _format_users_page(offset):
    rows = get_users_page(offset, USERS_PAGE_SIZE)
    total = count_all_users()
    if not rows:
        return "No users found.", None
    shown_to = min(offset + len(rows), total)
    lines = [f"👥 USERS ({offset + 1}-{shown_to} of {total})\n"]
    for u in rows:
        status = " 🚫 BANNED" if u["banned"] else ""
        usdt = u["usdt"] if u["usdt"] is not None else 0.0
        lines.append(
            f"👤 {_esc(u['name'])}{status}\n"
            f"🔗 {_esc(display_username(u))}\n"
            f"🆔 <code>{u['user_id']}</code>  (tap to copy)\n"
            f"🪙 {fmt_amount(usdt, 'usdt')}\n"
            "━━━━━━━━━━━━━━━━━━"
        )
    text = "\n".join(lines)
    kb = types.InlineKeyboardMarkup()
    nav = []
    if offset > 0:
        nav.append(types.InlineKeyboardButton("⬅️ Prev", callback_data=f"userspage_{max(0, offset - USERS_PAGE_SIZE)}"))
    if offset + USERS_PAGE_SIZE < total:
        nav.append(types.InlineKeyboardButton("Next ➡️", callback_data=f"userspage_{offset + USERS_PAGE_SIZE}"))
    if nav:
        kb.row(*nav)
    return text, kb


@bot.message_handler(func=lambda m: m.text == "👥 Users" and is_admin(m.chat.id))
@safe_handler
def admin_users_list(m):
    if not is_admin(m.chat.id):
        return
    clear_state(m.chat.id)
    text, kb = _format_users_page(0)
    bot.send_message(m.chat.id, text, reply_markup=kb, parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data.startswith("userspage_"))
@safe_handler
def admin_users_page_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    offset = int(c.data[len("userspage_"):])
    text, kb = _format_users_page(offset)
    bot.answer_callback_query(c.id)
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")
    except Exception:
        bot.send_message(c.message.chat.id, text, reply_markup=kb, parse_mode="HTML")


# ================================================================
# ADMIN: BANNED USERS LIST (view + unban directly from the list)
# ================================================================

BANNED_PAGE_SIZE = 6


def _format_banned_page(offset):
    rows = get_banned_users(offset, BANNED_PAGE_SIZE)
    total = count_banned_users()
    if not rows:
        return "✅ No banned users.", None
    shown_to = min(offset + len(rows), total)
    lines = [f"🚫 BANNED USERS ({offset + 1}-{shown_to} of {total})"]
    kb = types.InlineKeyboardMarkup()
    for u in rows:
        lines.append(
            f"\n👤 {_esc(u['name'])}\n"
            f"🆔 <code>{u['user_id']}</code>\n"
            f"📝 Reason: {_esc(u['ban_reason'] or 'Not specified')}"
        )
        kb.add(types.InlineKeyboardButton(f"✅ Unban {u['name']}", callback_data=f"unbanconfirm_{u['user_id']}"))
    nav = []
    if offset > 0:
        nav.append(types.InlineKeyboardButton("⬅️ Prev", callback_data=f"bannedpage_{max(0, offset - BANNED_PAGE_SIZE)}"))
    if offset + BANNED_PAGE_SIZE < total:
        nav.append(types.InlineKeyboardButton("Next ➡️", callback_data=f"bannedpage_{offset + BANNED_PAGE_SIZE}"))
    if nav:
        kb.row(*nav)
    return "\n".join(lines), kb


@bot.message_handler(func=lambda m: m.text == "📋 Banned Users" and is_admin(m.chat.id))
@safe_handler
def admin_banned_list(m):
    if not is_admin(m.chat.id):
        return
    clear_state(m.chat.id)
    text, kb = _format_banned_page(0)
    bot.send_message(m.chat.id, text, reply_markup=kb, parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data.startswith("bannedpage_"))
@safe_handler
def admin_banned_page_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    offset = int(c.data[len("bannedpage_"):])
    text, kb = _format_banned_page(offset)
    bot.answer_callback_query(c.id)
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")
    except Exception:
        bot.send_message(c.message.chat.id, text, reply_markup=kb, parse_mode="HTML")


# ================================================================
# ADMIN: UNIFIED SEARCH
# ================================================================

@bot.message_handler(func=lambda m: m.text == "🔍 Search" and is_admin(m.chat.id))
@safe_handler
def admin_search_menu(m):
    if not is_admin(m.chat.id):
        return
    clear_state(m.chat.id)
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🏦 Bank ID Search")
    kb.row("🎫 Support ID Search", "📝 Submission ID Search")
    kb.row("🆔 User ID Search", "💵 Withdrawal ID Search")
    kb.row("🔙 Back")
    bot.send_message(m.chat.id, "🔍 SEARCH\n\nSelect what you want to look up:", reply_markup=kb)


@bot.message_handler(func=lambda m: m.text == "🏦 Bank ID Search" and is_admin(m.chat.id))
@safe_handler
def admin_search_bank_start(m):
    if not is_admin(m.chat.id):
        return
    clear_state(m.chat.id)
    update_state(m.chat.id, flow="admin_bank_search")
    bot.send_message(
        m.chat.id,
        "🏦 BANK ID SEARCH\n\nPlease enter the Bank/Wallet/Crypto reference (e.g. BANKREQ-XXXXXXXX):",
        reply_markup=back_kb(),
    )


def _handle_admin_bank_search(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    bank_id = m.text.strip()
    clear_state(m.chat.id)
    row = get_bank_submission(bank_id)
    if row is None:
        bot.send_message(
            m.chat.id,
            f"❌ No bank/wallet/crypto submission found with:\n\n{bank_id}",
            reply_markup=main_menu(m.chat.id),
        )
        return
    user = get_user(row["user_id"])
    text = (
        "🏦 BANK/WALLET/CRYPTO SUBMISSION\n\n"
        f"🧾 Reference: {row['bank_id']}\n"
        f"📊 Status: {row['status']}\n\n"
        f"👤 User: {user['name'] if user else 'Unknown'}\n"
        f"🔗 Username: {display_username(user)}\n"
        f"🆔 User ID: {row['user_id']}\n\n"
        f"🏷️ Category: {row['category'].capitalize()}\n"
        f"🏦 Method: {row['method']}\n"
        f"📝 Details: {row['details']}\n\n"
        f"📅 Submitted: {row['created_at']}"
    )
    if row["status"] != "PENDING":
        text += f"\n👮 Processed By: {row['processed_by']}\n📅 Processed At: {row['processed_at']}"
    if row["status"] == "DECLINED" and row["decline_reason"]:
        text += f"\n📝 Decline Reason: {row['decline_reason']}"
    bot.send_message(m.chat.id, text, reply_markup=main_menu(m.chat.id))


@bot.message_handler(func=lambda m: m.text == "🎫 Support ID Search" and is_admin(m.chat.id))
@safe_handler
def admin_search_support_start(m):
    if not is_admin(m.chat.id):
        return
    clear_state(m.chat.id)
    update_state(m.chat.id, flow="admin_support_search")
    bot.send_message(
        m.chat.id,
        "🎫 SUPPORT ID SEARCH\n\nPlease enter the Support Reference (e.g. SUP-XXXXXXXX):",
        reply_markup=back_kb(),
    )


def _handle_admin_support_search(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    ticket_id = m.text.strip()
    clear_state(m.chat.id)
    row = get_support_ticket(ticket_id)
    if row is None:
        bot.send_message(
            m.chat.id,
            f"❌ No support ticket found with:\n\n{ticket_id}",
            reply_markup=main_menu(m.chat.id),
        )
        return
    user = get_user(row["user_id"])
    text = (
        "🎫 SUPPORT TICKET\n\n"
        f"🧾 Reference: {row['ticket_id']}\n"
        f"📊 Status: {row['status']}\n\n"
        f"👤 User: {user['name'] if user else 'Unknown'}\n"
        f"🔗 Username: {display_username(user)}\n"
        f"🆔 User ID: {row['user_id']}\n\n"
        f"📝 Complaint: {row['complaint'] or '(media only — see original message)'}\n\n"
        f"📅 Submitted: {row['created_at']}"
    )
    if row["resolved_at"]:
        text += f"\n✅ Resolved At: {row['resolved_at']}"
    bot.send_message(m.chat.id, text, reply_markup=main_menu(m.chat.id))


@bot.message_handler(func=lambda m: m.text == "📝 Submission ID Search" and is_admin(m.chat.id))
@safe_handler
def admin_search_submission_start(m):
    if not is_admin(m.chat.id):
        return
    clear_state(m.chat.id)
    update_state(m.chat.id, flow="admin_submission_search")
    bot.send_message(
        m.chat.id,
        "📝 SUBMISSION ID SEARCH\n\nPlease enter the Submission Reference (e.g. SUB-XXXXXXXX):",
        reply_markup=back_kb(),
    )


def _handle_admin_submission_search(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    sub_id = m.text.strip()
    clear_state(m.chat.id)
    row = get_submission(sub_id)
    if row is None:
        bot.send_message(
            m.chat.id,
            f"❌ No submission found with:\n\n{sub_id}",
            reply_markup=main_menu(m.chat.id),
        )
        return
    user = get_user(row["user_id"])
    text = (
        "📝 WORK SUBMISSION\n\n"
        f"🧾 Reference: {row['sub_id']}\n"
        f"📊 Status: {row['status']}\n\n"
        f"👤 User: {user['name'] if user else 'Unknown'}\n"
        f"🔗 Username: {display_username(user)}\n"
        f"🆔 User ID: {row['user_id']}\n\n"
        f"🏷️ Work Type: {row['work_type']}\n"
        f"📌 Sub Type: {row['sub_type']}\n"
        f"📎 File Type: {row['file_type'] or 'None'}\n\n"
        f"📅 Submitted: {row['created_at']}"
    )
    if row["status"] != "PENDING":
        text += f"\n👮 Processed By: {row['processed_by']}\n📅 Processed At: {row['processed_at']}"
    if row["status"] == "REJECTED" and row["reject_reason"]:
        text += f"\n📝 Reject Reason: {row['reject_reason']}"
    bot.send_message(m.chat.id, text, reply_markup=main_menu(m.chat.id))


@bot.message_handler(func=lambda m: m.text == "🆔 User ID Search" and is_admin(m.chat.id))
@safe_handler
def admin_search_user_start(m):
    if not is_admin(m.chat.id):
        return
    clear_state(m.chat.id)
    update_state(m.chat.id, flow="admin_user_search")
    bot.send_message(
        m.chat.id,
        "🆔 USER ID SEARCH\n\nPlease enter the User ID (Telegram ID):",
        reply_markup=back_kb(),
    )


def _handle_admin_user_search(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    user_id = m.text.strip()
    clear_state(m.chat.id)
    user = get_user(user_id)
    if user is None:
        bot.send_message(
            m.chat.id,
            f"❌ No user found with:\n\n{user_id}",
            reply_markup=main_menu(m.chat.id),
        )
        return
    wallet = get_wallet(user_id)
    usdt = wallet["usdt"] if wallet and wallet["usdt"] is not None else 0.0
    status = " 🚫 BANNED" if user["banned"] else " ✅ ACTIVE"
    text = (
        "🆔 USER PROFILE\n\n"
        f"👤 Name: {user['name']}{status}\n"
        f"🔗 Username: {display_username(user)}\n"
        f"🆔 User ID: {user['user_id']}\n\n"
        f"💵 USDT Balance: {fmt_amount(usdt, 'usdt')}\n\n"
        f"📅 Joined: {user['created_at']}"
    )
    if user["banned"] and user["ban_reason"]:
        text += f"\n📝 Ban Reason: {user['ban_reason']}"
    bot.send_message(m.chat.id, text, reply_markup=main_menu(m.chat.id))


@bot.message_handler(func=lambda m: m.text == "💵 Withdrawal ID Search" and is_admin(m.chat.id))
@safe_handler
def admin_search_withdrawal_start(m):
    if not is_admin(m.chat.id):
        return
    clear_state(m.chat.id)
    update_state(m.chat.id, flow="admin_withdrawal_search")
    bot.send_message(
        m.chat.id,
        "💵 WITHDRAWAL ID SEARCH\n\nPlease enter the Withdrawal Reference:",
        reply_markup=back_kb(),
    )


def _handle_admin_withdrawal_search(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    withdrawal_id = m.text.strip()
    clear_state(m.chat.id)
    row = get_withdrawal(withdrawal_id)
    if row is None:
        bot.send_message(
            m.chat.id,
            f"❌ No withdrawal found with:\n\n{withdrawal_id}",
            reply_markup=main_menu(m.chat.id),
        )
        return
    user = get_user(row["user_id"])
    text = (
        "💵 WITHDRAWAL\n\n"
        f"🧾 Reference: {row['withdrawal_id']}\n"
        f"📊 Status: {row['status']}\n\n"
        f"👤 User: {user['name'] if user else 'Unknown'}\n"
        f"🔗 Username: {display_username(user)}\n"
        f"🆔 User ID: {row['user_id']}\n\n"
        f"💰 Amount: {fmt_amount(row['amount'], row['currency'])}\n"
        f"🏦 Method: {row['method']}\n\n"
        f"📅 Submitted: {row['created_at']}"
    )
    if row["status"] != "PENDING":
        text += f"\n👮 Processed By: {row['processed_by']}\n📅 Processed At: {row['processed_at']}"
    if row["status"] == "DECLINED" and row["reason"]:
        text += f"\n📝 Decline Reason: {row['reason']}"
    if row["txn_id"]:
        text += f"\n🔖 Txn ID: {row['txn_id']}"
    bot.send_message(m.chat.id, text, reply_markup=main_menu(m.chat.id))


# ================================================================
# ADMIN: SETTINGS (USDT minimum withdrawal/referral settings — anytime)
# ================================================================

_SETTING_LABELS = {
    "min_withdrawal_usdt": ("Minimum Withdrawal (USDT)", "usdt"),
    "referral_amount_usdt": ("Referral Amount (USDT)", "usdt"),
}


@bot.message_handler(func=lambda m: m.text == "⚙️ Settings" and is_super_admin(m.chat.id))
@safe_handler
def admin_settings_menu(m):
    if not is_admin(m.chat.id):
        return
    clear_state(m.chat.id)
    ref_on = is_referral_enabled()
    text = (
        "⚙️ SETTINGS\n\n"
        f"💰 Min Withdrawal (USDT): {fmt_amount(get_min_withdrawal('usdt'), 'usdt')}\n"
        f"🎁 Referral Amount (USDT): {fmt_amount(get_referral_amount('usdt'), 'usdt')}\n"
        f"🔘 Referral Status: {'✅ ON' if ref_on else '⛔ OFF'}\n\n"
        "Tap a value below to change it:"
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✏️ Min Withdrawal (USDT)", callback_data="setedit_min_withdrawal_usdt"))
    kb.add(types.InlineKeyboardButton("✏️ Referral Amount (USDT)", callback_data="setedit_referral_amount_usdt"))
    kb.add(types.InlineKeyboardButton(
        "⛔ Turn Referral OFF" if ref_on else "✅ Turn Referral ON",
        callback_data="reftoggle_off" if ref_on else "reftoggle_on",
    ))
    bot.send_message(m.chat.id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data in ("reftoggle_on", "reftoggle_off"))
@safe_handler
def admin_referral_toggle_cb(c):
    if not is_super_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    turn_on = c.data == "reftoggle_on"
    set_setting("referral_enabled", "1" if turn_on else "0", c.message.chat.id)
    bot.answer_callback_query(c.id, "✅ Referral turned ON." if turn_on else "⛔ Referral turned OFF.")
    admin_settings_menu(c.message)


@bot.callback_query_handler(func=lambda c: c.data.startswith("setedit_"))
@safe_handler
def admin_settings_edit_cb(c):
    if not is_super_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    key = c.data[len("setedit_"):]
    label, currency = _SETTING_LABELS.get(key, (key, "usdt"))
    clear_state(c.message.chat.id)
    update_state(c.message.chat.id, flow="admin_setting", key=key)
    bot.answer_callback_query(c.id)
    extra = ""
    bot.send_message(
        c.message.chat.id,
        f"✏️ Enter the new value for {label} ({currency.upper()}):\n\n"
        f"Send a plain number (e.g. 500 or 0.5).{extra}",
        reply_markup=back_kb(),
    )


def _handle_admin_setting(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    key = state["key"]
    label, currency = _SETTING_LABELS.get(key, (key, "usdt"))
    try:
        value = float(m.text.strip())
    except (TypeError, ValueError):
        bot.send_message(m.chat.id, "❌ Invalid number. Please enter a numeric value.")
        return
    if value < 0:
        bot.send_message(m.chat.id, "❌ Value cannot be negative.")
        return
    clear_state(m.chat.id)

    set_setting(key, value, m.chat.id)
    bot.send_message(m.chat.id, f"✅ {label} updated to {value} {currency.upper()}.", reply_markup=main_menu(m.chat.id))


# ================================================================
# ADMIN: EDIT BOT TEXT (rewrite any listed message, no redeploy)
# ================================================================
# Same shape as the "⚙️ Settings" editor above: tap a template in the
# list → send the new wording → saved into `settings` as
# f"text:{key}" → every future send of that message uses it (see
# TEXT_TEMPLATES / get_text / render_text near the top of the file).

@bot.message_handler(func=lambda m: m.text == "📝 Edit Bot Text" and is_super_admin(m.chat.id))
@safe_handler
def admin_text_menu(m):
    if not is_admin(m.chat.id):
        return
    clear_state(m.chat.id)
    kb = types.InlineKeyboardMarkup()
    for key, (label, default) in TEXT_TEMPLATES.items():
        edited = get_setting(f"text:{key}") is not None
        kb.add(types.InlineKeyboardButton(
            f"{'✏️' if edited else '📄'} {label}", callback_data=f"textedit_{key}",
        ))
    bot.send_message(
        m.chat.id,
        "📝 EDIT BOT TEXT\n\n"
        "Tap a message below to rewrite it. ✏️ means it has already been "
        "customized; 📄 means it's still the original wording.\n\n"
        "Placeholders shown in brackets (e.g. {amount}) will be filled in "
        "automatically — keep them exactly as spelled, in your new text, "
        "wherever you want that value to appear.",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("textedit_"))
@safe_handler
def admin_text_edit_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    key = c.data[len("textedit_"):]
    if key not in TEXT_TEMPLATES:
        bot.answer_callback_query(c.id, "❌ Unknown template.")
        return
    label, default = TEXT_TEMPLATES[key]
    current = get_text(key, default)
    clear_state(c.message.chat.id)
    update_state(c.message.chat.id, flow="text_edit", key=key)
    bot.answer_callback_query(c.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("↩️ Reset to default", callback_data=f"textreset_{key}"))
    bot.send_message(
        c.message.chat.id,
        f"✏️ {label}\n\n"
        "Current text (send a new version below to replace it, keeping any "
        "{placeholders} you want to keep):\n\n"
        f"――――――――――――――\n{current}\n――――――――――――――",
        reply_markup=kb,
    )
    bot.send_message(c.message.chat.id, "👉 Send the new text now:", reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data.startswith("textreset_"))
@safe_handler
def admin_text_reset_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    key = c.data[len("textreset_"):]
    if key not in TEXT_TEMPLATES:
        bot.answer_callback_query(c.id, "❌ Unknown template.")
        return
    delete_setting(f"text:{key}")
    clear_state(c.message.chat.id)
    bot.answer_callback_query(c.id, "↩️ Reset to default.")
    bot.send_message(c.message.chat.id, "✅ Reset to the original wording.", reply_markup=main_menu(c.message.chat.id))


def _handle_text_edit(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    key = state["key"]
    if key not in TEXT_TEMPLATES:
        clear_state(m.chat.id)
        return
    label, default = TEXT_TEMPLATES[key]
    new_text = m.text.strip()

    # Guard against a template the admin can't actually fill in: every
    # {placeholder} used in the *default* wording must still be usable
    # in their replacement, or a future send would silently fall back
    # to the default (see render_text). We don't require every
    # placeholder to be present — just that any curly braces used are
    # valid, so we catch typos immediately instead of at send time.
    try:
        import string
        used = {name for _, name, _, _ in string.Formatter().parse(new_text) if name}
        allowed = {name for _, name, _, _ in string.Formatter().parse(default) if name}
        unknown = used - allowed
    except Exception:
        unknown = set()
    if unknown:
        bot.send_message(
            m.chat.id,
            f"❌ Unknown placeholder(s) in your text: {', '.join('{' + u + '}' for u in unknown)}\n\n"
            f"Allowed placeholders for this message: {', '.join('{' + a + '}' for a in allowed) or '(none)'}\n\n"
            "Please send the text again with only allowed placeholders.",
        )
        return

    clear_state(m.chat.id)
    set_setting(f"text:{key}", new_text, m.chat.id)
    bot.send_message(m.chat.id, f"✅ {label} updated.", reply_markup=main_menu(m.chat.id))


# ================================================================
# ADMIN: MENU EDITOR — rename any main-menu button, and add/rename/
# enable/disable/delete the options shown inside "📤 Submit Work"
# (categories + the sub-options inside each) and "🛒 BUY OR SELL MAIL"
# — all from inside the bot, no code changes, no redeploy.
# ================================================================

@bot.message_handler(func=lambda m: m.text == "🧩 Menu Editor" and is_super_admin(m.chat.id))
@safe_handler
def admin_menu_editor(m):
    if not is_admin(m.chat.id):
        return
    clear_state(m.chat.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📋 View Everything (all buttons & options)", callback_data="me_all"))
    kb.add(types.InlineKeyboardButton("🔘 Edit Main Button Labels", callback_data="me_labels"))
    kb.add(types.InlineKeyboardButton("💼 Submit Work Options", callback_data="me_work"))
    kb.add(types.InlineKeyboardButton("📧 Buy/Sell Mail Options", callback_data="me_mail"))
    bot.send_message(
        m.chat.id,
        "🧩 MENU EDITOR\n\n"
        "Rename any button on the main menu, or add/rename/enable/disable/remove "
        "the choices shown inside Submit Work and Buy/Sell Mail. Changes are live "
        "immediately for every user.",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data == "me_all")
@safe_handler
def me_all_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    bot.answer_callback_query(c.id)
    chat_id = c.message.chat.id

    lines = ["📋 EVERYTHING IN THIS BOT'S MENUS\n(🟢 = active/live, 🔴 = disabled — every item, nothing hidden)\n"]

    lines.append("🔘 MAIN BUTTONS")
    for key in BUTTON_LABELS:
        edited = "✏️ " if get_setting(f"btnlabel:{key}") is not None else ""
        if key in FEATURES:
            status = "🟢" if is_feature_enabled(key) else "🔴 (disabled via 🛠 Feature Control)"
        else:
            status = "🟢"
        lines.append(f"  {status} {edited}{btn_label(key)}")

    lines.append("\n💼 SUBMIT WORK — CATEGORIES & OPTIONS")
    cats = list_menu_options("work_category", active_only=False)
    if not cats:
        lines.append("  (none yet)")
    for cat in cats:
        status = "🟢" if cat["active"] else "🔴"
        lines.append(f"  {status} {cat['label']}")
        subs = list_menu_options("work_subtype", parent_key=cat["option_id"], active_only=False)
        if not subs:
            lines.append("      (no options inside)")
        for s in subs:
            sstatus = "🟢" if s["active"] else "🔴"
            lines.append(f"      {sstatus} {s['label']}")

    lines.append("\n📧 BUY/SELL MAIL — OPTIONS")
    mails = list_menu_options("mail_option", active_only=False)
    if not mails:
        lines.append("  (none yet)")
    for o in mails:
        status = "🟢" if o["active"] else "🔴"
        lines.append(f"  {status} {o['label']}")

    handles = list_custom_handles(active_only=False)
    lines.append("\n➕ CUSTOM HANDLES")
    if not handles:
        lines.append("  (none yet)")
    for h in handles:
        status = "🟢" if h["active"] else "🔴"
        lines.append(f"  {status} {h['label']} — {CUSTOM_HANDLE_TYPES.get(h['action_type'], h['action_type'])}")

    lines.append(
        "\nTap 🔘/💼/📧 below to edit any of these, or use 📋 Manage Custom Handles "
        "from the main menu for the custom handles listed above."
    )

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔘 Edit Main Button Labels", callback_data="me_labels"))
    kb.add(types.InlineKeyboardButton("💼 Submit Work Options", callback_data="me_work"))
    kb.add(types.InlineKeyboardButton("📧 Buy/Sell Mail Options", callback_data="me_mail"))
    bot.send_message(chat_id, "\n".join(lines), reply_markup=kb)


# ---- Main button labels ----------------------------------------

@bot.callback_query_handler(func=lambda c: c.data == "me_labels")
@safe_handler
def me_labels_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    bot.answer_callback_query(c.id)
    kb = types.InlineKeyboardMarkup()
    for key in BUTTON_LABELS:
        edited = get_setting(f"btnlabel:{key}") is not None
        kb.add(types.InlineKeyboardButton(
            f"{'✏️' if edited else '📄'} {btn_label(key)}", callback_data=f"me_lbl_{key}",
        ))
    bot.send_message(
        c.message.chat.id,
        "🔘 MAIN BUTTON LABELS\n\n"
        "Tap a button below to rename it. ✏️ = already customized, 📄 = original wording.",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("me_lblreset_"))
@safe_handler
def me_label_reset_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    key = c.data[len("me_lblreset_"):]
    if key not in BUTTON_LABELS:
        bot.answer_callback_query(c.id, "❌ Unknown button.")
        return
    reset_btn_label(key)
    clear_state(c.message.chat.id)
    bot.answer_callback_query(c.id, "↩️ Reset to default.")
    bot.send_message(c.message.chat.id, "✅ Reset to the original wording.", reply_markup=main_menu(c.message.chat.id))


@bot.callback_query_handler(func=lambda c: c.data.startswith("me_lbl_"))
@safe_handler
def me_label_edit_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    key = c.data[len("me_lbl_"):]
    if key not in BUTTON_LABELS:
        bot.answer_callback_query(c.id, "❌ Unknown button.")
        return
    clear_state(c.message.chat.id)
    update_state(c.message.chat.id, flow="btn_label_edit", key=key)
    bot.answer_callback_query(c.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("↩️ Reset to default", callback_data=f"me_lblreset_{key}"))
    bot.send_message(c.message.chat.id, f"✏️ Current label: {btn_label(key)}", reply_markup=kb)
    bot.send_message(c.message.chat.id, "👉 Send the new button text now:", reply_markup=back_kb())


def _handle_btn_label_edit(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    key = state["key"]
    if key not in BUTTON_LABELS:
        clear_state(m.chat.id)
        return
    new_label = m.text.strip()
    if not new_label:
        bot.send_message(m.chat.id, "❌ Label can't be empty. Please send the button text.")
        return
    others = reserved_labels_now() - {btn_label(key)}
    if new_label in others:
        bot.send_message(m.chat.id, "❌ That label is already used by another button. Please choose a different one.")
        return
    clear_state(m.chat.id)
    set_btn_label(key, new_label, m.chat.id)
    bot.send_message(m.chat.id, f"✅ Button updated to: {new_label}", reply_markup=main_menu(m.chat.id))


# ---- Submit Work: categories & the options inside each ----------

@bot.callback_query_handler(func=lambda c: c.data == "me_work")
@safe_handler
def me_work_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    bot.answer_callback_query(c.id)
    _send_work_categories(c.message.chat.id)


def _send_work_categories(chat_id):
    kb = types.InlineKeyboardMarkup()
    for cat in list_menu_options("work_category", active_only=False):
        status = "🟢" if cat["active"] else "🔴"
        kb.add(types.InlineKeyboardButton(f"{status} {cat['label']}", callback_data=f"me_wcat_{cat['option_id']}"))
    kb.add(types.InlineKeyboardButton("➕ Add Work Category", callback_data="me_wcat_add"))
    bot.send_message(
        chat_id,
        "💼 SUBMIT WORK — CATEGORIES\n\n"
        "🟢 = active, 🔴 = disabled. Tap a category to rename/disable/delete it or add "
        "options inside it, or add a whole new category.",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data == "me_wcat_add")
@safe_handler
def me_wcat_add_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    clear_state(c.message.chat.id)
    update_state(c.message.chat.id, flow="menu_opt_add", section="work_category")
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id, "✍️ Send the name for the new work category (e.g. \"🟢 TikTok Work\"):", reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data.startswith("me_wcat_") and not c.data.startswith("me_wcat_add"))
@safe_handler
def me_wcat_manage_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    option_id = c.data[len("me_wcat_"):]
    cat = get_menu_option(option_id)
    if cat is None:
        bot.answer_callback_query(c.id, "❌ Not found — it may already be deleted.")
        return
    bot.answer_callback_query(c.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✏️ Rename category", callback_data=f"me_wcatren_{option_id}"))
    kb.add(types.InlineKeyboardButton("🔴 Disable" if cat["active"] else "🟢 Enable", callback_data=f"me_wcattoggle_{option_id}"))
    kb.add(types.InlineKeyboardButton("🗑️ Delete category (+ its options)", callback_data=f"me_wcatdel_{option_id}"))
    kb.add(types.InlineKeyboardButton("➕ Add Option Inside This Category", callback_data=f"me_wsub_add_{option_id}"))
    for s in list_menu_options("work_subtype", parent_key=option_id, active_only=False):
        status = "🟢" if s["active"] else "🔴"
        kb.add(types.InlineKeyboardButton(f"{status} {s['label']}", callback_data=f"me_wsub_{s['option_id']}"))
    bot.send_message(
        c.message.chat.id,
        f"🔧 {cat['label']}\n\n"
        f"📊 Status: {'🟢 Active' if cat['active'] else '🔴 Disabled'}\n\n"
        "Options shown to users once they pick this category are listed below — tap one to manage it.",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("me_wcatren_"))
@safe_handler
def me_wcat_rename_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    option_id = c.data[len("me_wcatren_"):]
    if get_menu_option(option_id) is None:
        bot.answer_callback_query(c.id, "❌ Not found.")
        return
    clear_state(c.message.chat.id)
    update_state(c.message.chat.id, flow="menu_opt_rename", option_id=option_id)
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id, "✍️ Send the new name for this category:", reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data.startswith("me_wcattoggle_"))
@safe_handler
def me_wcat_toggle_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    option_id = c.data[len("me_wcattoggle_"):]
    cat = get_menu_option(option_id)
    if cat is None:
        bot.answer_callback_query(c.id, "❌ Not found.")
        return
    toggle_menu_option(option_id, not cat["active"])
    bot.answer_callback_query(c.id, "✅ Updated")
    _send_work_categories(c.message.chat.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("me_wcatdel_"))
@safe_handler
def me_wcat_delete_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    option_id = c.data[len("me_wcatdel_"):]
    if get_menu_option(option_id) is None:
        bot.answer_callback_query(c.id, "❌ Not found.")
        return
    delete_menu_option(option_id)
    bot.answer_callback_query(c.id, "🗑️ Deleted")
    _send_work_categories(c.message.chat.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("me_wsub_add_"))
@safe_handler
def me_wsub_add_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    parent_id = c.data[len("me_wsub_add_"):]
    cat = get_menu_option(parent_id)
    if cat is None:
        bot.answer_callback_query(c.id, "❌ Category not found.")
        return
    clear_state(c.message.chat.id)
    update_state(c.message.chat.id, flow="menu_opt_add", section="work_subtype", parent_key=parent_id)
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id, f"✍️ Send the name for the new option inside \"{cat['label']}\" (e.g. \"🆔 New Sub-type\"):", reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data.startswith("me_wsub_") and not c.data.startswith("me_wsub_add_"))
@safe_handler
def me_wsub_manage_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    option_id = c.data[len("me_wsub_"):]
    sub = get_menu_option(option_id)
    if sub is None:
        bot.answer_callback_query(c.id, "❌ Not found — it may already be deleted.")
        return
    bot.answer_callback_query(c.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✏️ Rename", callback_data=f"me_wsubren_{option_id}"))
    kb.add(types.InlineKeyboardButton("🔴 Disable" if sub["active"] else "🟢 Enable", callback_data=f"me_wsubtoggle_{option_id}"))
    kb.add(types.InlineKeyboardButton("🗑️ Delete", callback_data=f"me_wsubdel_{option_id}"))
    bot.send_message(
        c.message.chat.id,
        f"🔧 {sub['label']}\n\n📊 Status: {'🟢 Active' if sub['active'] else '🔴 Disabled'}",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("me_wsubren_"))
@safe_handler
def me_wsub_rename_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    option_id = c.data[len("me_wsubren_"):]
    if get_menu_option(option_id) is None:
        bot.answer_callback_query(c.id, "❌ Not found.")
        return
    clear_state(c.message.chat.id)
    update_state(c.message.chat.id, flow="menu_opt_rename", option_id=option_id)
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id, "✍️ Send the new name for this option:", reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data.startswith("me_wsubtoggle_"))
@safe_handler
def me_wsub_toggle_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    option_id = c.data[len("me_wsubtoggle_"):]
    sub = get_menu_option(option_id)
    if sub is None:
        bot.answer_callback_query(c.id, "❌ Not found.")
        return
    toggle_menu_option(option_id, not sub["active"])
    bot.answer_callback_query(c.id, "✅ Updated")
    bot.send_message(c.message.chat.id, "✅ Updated. Open the category again from 🧩 Menu Editor to see the change.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("me_wsubdel_"))
@safe_handler
def me_wsub_delete_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    option_id = c.data[len("me_wsubdel_"):]
    if get_menu_option(option_id) is None:
        bot.answer_callback_query(c.id, "❌ Not found.")
        return
    delete_menu_option(option_id)
    bot.answer_callback_query(c.id, "🗑️ Deleted")
    bot.send_message(c.message.chat.id, "✅ Deleted.")


# ---- Buy/Sell Mail options ---------------------------------------

@bot.callback_query_handler(func=lambda c: c.data == "me_mail")
@safe_handler
def me_mail_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    bot.answer_callback_query(c.id)
    _send_mail_options(c.message.chat.id)


def _send_mail_options(chat_id):
    kb = types.InlineKeyboardMarkup()
    for o in list_menu_options("mail_option", active_only=False):
        status = "🟢" if o["active"] else "🔴"
        kb.add(types.InlineKeyboardButton(f"{status} {o['label']}", callback_data=f"me_mopt_{o['option_id']}"))
    kb.add(types.InlineKeyboardButton("➕ Add Mail Option", callback_data="me_mopt_add"))
    bot.send_message(
        chat_id,
        "📧 BUY/SELL MAIL — OPTIONS\n\n"
        "🟢 = active, 🔴 = disabled. Tap an option to rename/disable/delete it, or add a new one.",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data == "me_mopt_add")
@safe_handler
def me_mopt_add_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    clear_state(c.message.chat.id)
    update_state(c.message.chat.id, flow="menu_opt_add", section="mail_option")
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id, "✍️ Send the name for the new mail option (e.g. \"SELL OUTLOOK\"):", reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data.startswith("me_mopt_") and not c.data.startswith("me_mopt_add"))
@safe_handler
def me_mopt_manage_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    option_id = c.data[len("me_mopt_"):]
    o = get_menu_option(option_id)
    if o is None:
        bot.answer_callback_query(c.id, "❌ Not found — it may already be deleted.")
        return
    bot.answer_callback_query(c.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✏️ Rename", callback_data=f"me_moptren_{option_id}"))
    kb.add(types.InlineKeyboardButton("🔴 Disable" if o["active"] else "🟢 Enable", callback_data=f"me_mopttoggle_{option_id}"))
    kb.add(types.InlineKeyboardButton("🗑️ Delete", callback_data=f"me_moptdel_{option_id}"))
    bot.send_message(
        c.message.chat.id,
        f"🔧 {o['label']}\n\n📊 Status: {'🟢 Active' if o['active'] else '🔴 Disabled'}",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("me_moptren_"))
@safe_handler
def me_mopt_rename_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    option_id = c.data[len("me_moptren_"):]
    if get_menu_option(option_id) is None:
        bot.answer_callback_query(c.id, "❌ Not found.")
        return
    clear_state(c.message.chat.id)
    update_state(c.message.chat.id, flow="menu_opt_rename", option_id=option_id)
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id, "✍️ Send the new name for this option:", reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data.startswith("me_mopttoggle_"))
@safe_handler
def me_mopt_toggle_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    option_id = c.data[len("me_mopttoggle_"):]
    o = get_menu_option(option_id)
    if o is None:
        bot.answer_callback_query(c.id, "❌ Not found.")
        return
    toggle_menu_option(option_id, not o["active"])
    bot.answer_callback_query(c.id, "✅ Updated")
    _send_mail_options(c.message.chat.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("me_moptdel_"))
@safe_handler
def me_mopt_delete_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    option_id = c.data[len("me_moptdel_"):]
    if get_menu_option(option_id) is None:
        bot.answer_callback_query(c.id, "❌ Not found.")
        return
    delete_menu_option(option_id)
    bot.answer_callback_query(c.id, "🗑️ Deleted")
    _send_mail_options(c.message.chat.id)


# ---- Shared text-input handlers for "add" / "rename" -------------

def _handle_menu_opt_add(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    section = state["section"]
    parent_key = state.get("parent_key")
    label = m.text.strip()
    if not label:
        bot.send_message(m.chat.id, "❌ Name can't be empty. Please send the text.")
        return
    if label in reserved_labels_now():
        bot.send_message(m.chat.id, "❌ That name is already used elsewhere in the bot. Please choose a different one.")
        return
    if get_menu_option_by_label(section, label, parent_key=parent_key) is not None:
        bot.send_message(m.chat.id, "❌ An option with that exact name already exists here. Please choose a different name.")
        return
    clear_state(m.chat.id)
    create_menu_option(section, label, m.chat.id, parent_key=parent_key)
    bot.send_message(m.chat.id, f"✅ Added: {label}", reply_markup=main_menu(m.chat.id))
    if section == "work_category":
        _send_work_categories(m.chat.id)
    elif section == "work_subtype":
        cat = get_menu_option(parent_key)
        if cat:
            bot.send_message(m.chat.id, f"Open \"{cat['label']}\" again from 🧩 Menu Editor to see it.")
    elif section == "mail_option":
        _send_mail_options(m.chat.id)


def _handle_menu_opt_rename(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    option_id = state["option_id"]
    row = get_menu_option(option_id)
    if row is None:
        clear_state(m.chat.id)
        bot.send_message(m.chat.id, "❌ That option no longer exists.", reply_markup=main_menu(m.chat.id))
        return
    new_label = m.text.strip()
    if not new_label:
        bot.send_message(m.chat.id, "❌ Name can't be empty. Please send the text.")
        return
    others = reserved_labels_now() - {row["label"]}
    if new_label in others:
        bot.send_message(m.chat.id, "❌ That name is already used elsewhere in the bot. Please choose a different one.")
        return
    clear_state(m.chat.id)
    update_menu_option_label(option_id, new_label)
    bot.send_message(m.chat.id, f"✅ Renamed to: {new_label}", reply_markup=main_menu(m.chat.id))


# ================================================================
# ADMIN: CUSTOM HANDLES (extra reply-keyboard buttons, built entirely
# from inside the bot — no code changes, no redeploy)
# ================================================================
# Three action_types, chosen per-handle by the admin while creating it
# (nothing about type, placement, or audience is fixed in code —
# every choice below lives in custom_handles.config_json / audience):
#   - static_message  : just shows a fixed message when tapped.
#   - forward_to_admin: asks the user for text/media, then forwards it
#                        to every admin (same shape as 📞 Support).
#   - link_button     : shows an inline button that opens a URL.
# Audience is also chosen per-handle: "all" users, or a specific list
# of user IDs (admins always see every active handle, regardless of
# audience, so they can check what a restricted one looks like).

CUSTOM_HANDLE_TYPES = {
    "static_message": "📄 Static message",
    "forward_to_admin": "📨 Forward to admin",
    "link_button": "🔗 Link button",
    "feature_link": "🔀 Open an existing feature",
}

# feature_link lets the admin wire a brand-new button straight to ANY
# capability already in the bot — Submit Work, Buy/Sell Mail, Balance,
# Withdraw, Referrals, History, Bank Details, Support,
# Profile — under whatever label/wording they want. This
# covers anything the admin might want a button to do that isn't a
# static message, a forward-to-admin form, or a plain link — every
# feature already built into the bot becomes reusable as a button.
FEATURE_LINK_LABELS = {
    "submit_work":   "📤 Submit Work",
    "buy_sell_mail": "🛒 Buy or Sell Mail",
    "balance":       "💰 My Balance",
    "withdraw":      "💸 Withdraw",
    "referrals":     "👥 My Referrals",
    "history":       "📜 Transaction History",
    "bank_details":  "🏦 Bank Details",
    "support":       "📞 Support",
    "profile":       "👤 My Profile",
}

# Reserved labels (built-in buttons a custom handle must never reuse)
# are now computed dynamically by reserved_labels_now() above, since
# main-menu button wording can be changed by the admin at any time —
# see "EDITABLE BUTTON LABELS" earlier in this file.


@bot.message_handler(func=lambda m: m.text == "➕ Add Custom Handle" and is_super_admin(m.chat.id))
@safe_handler
def admin_custom_handle_add_start(m):
    if not is_admin(m.chat.id):
        return
    clear_state(m.chat.id)
    update_state(m.chat.id, flow="custom_handle_add", step="label")
    bot.send_message(
        m.chat.id,
        "➕ ADD CUSTOM HANDLE\n\n"
        "This becomes a new button on the reply keyboard.\n\n"
        "✍️ Step 1/4 — Send the exact button text/label you want "
        "(e.g. \"📢 Announcements\"). Keep it short — this is what users tap.",
        reply_markup=back_kb(),
    )


def _handle_cha_label(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    label = m.text.strip()
    if not label:
        bot.send_message(m.chat.id, "❌ Label can't be empty. Please send the button text.")
        return
    if label in reserved_labels_now():
        bot.send_message(m.chat.id, "❌ That label is already used by a built-in button. Please choose a different one.")
        return
    if get_custom_handle_by_label(label) is not None:
        bot.send_message(m.chat.id, "❌ A handle with that exact label already exists. Please choose a different label.")
        return
    update_state(m.chat.id, flow="custom_handle_add", step="await_type", label=label)
    kb = types.InlineKeyboardMarkup()
    for key, cap in CUSTOM_HANDLE_TYPES.items():
        kb.add(types.InlineKeyboardButton(cap, callback_data=f"chatype_{key}"))
    bot.send_message(m.chat.id, "✍️ Step 2/4 — What should this button do?", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("chatype_"))
@safe_handler
def admin_custom_handle_type_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    state = get_state(c.message.chat.id)
    if state.get("flow") != "custom_handle_add" or state.get("step") != "await_type":
        bot.answer_callback_query(c.id, "⚠️ This setup has expired — please start again.")
        return
    action_type = c.data[len("chatype_"):]
    if action_type not in CUSTOM_HANDLE_TYPES:
        bot.answer_callback_query(c.id, "❌ Unknown type.")
        return
    bot.answer_callback_query(c.id)
    if action_type == "static_message":
        update_state(c.message.chat.id, flow="custom_handle_add", step="static_text", action_type=action_type)
        bot.send_message(c.message.chat.id, "✍️ Step 3/4 — Send the message to show when this button is tapped.", reply_markup=back_kb())
    elif action_type == "forward_to_admin":
        update_state(c.message.chat.id, flow="custom_handle_add", step="forward_prompt", action_type=action_type)
        bot.send_message(
            c.message.chat.id,
            "✍️ Step 3/4 — Send the prompt shown to the user before they type "
            "(e.g. \"✍️ Please enter your phone number:\"). Their reply (text, photo, "
            "video, document, or voice note) will be forwarded to every admin.",
            reply_markup=back_kb(),
        )
    elif action_type == "link_button":
        update_state(c.message.chat.id, flow="custom_handle_add", step="link_url", action_type=action_type)
        bot.send_message(c.message.chat.id, "✍️ Step 3/4 — Send the URL this button should open (must start with http:// or https://).", reply_markup=back_kb())
    elif action_type == "feature_link":
        update_state(c.message.chat.id, flow="custom_handle_add", step="feature_pick", action_type=action_type)
        kb = types.InlineKeyboardMarkup()
        for fkey, fcap in FEATURE_LINK_LABELS.items():
            kb.add(types.InlineKeyboardButton(fcap, callback_data=f"chafeat_{fkey}"))
        bot.send_message(c.message.chat.id, "✍️ Step 3/4 — Which existing feature should this button open?", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("chafeat_"))
@safe_handler
def admin_custom_handle_feature_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    state = get_state(c.message.chat.id)
    if state.get("flow") != "custom_handle_add" or state.get("step") != "feature_pick":
        bot.answer_callback_query(c.id, "⚠️ This setup has expired — please start again.")
        return
    fkey = c.data[len("chafeat_"):]
    if fkey not in FEATURE_LINK_LABELS:
        bot.answer_callback_query(c.id, "❌ Unknown feature.")
        return
    bot.answer_callback_query(c.id)
    update_state(c.message.chat.id, flow="custom_handle_add", step="audience", action_type="feature_link", feature_key=fkey)
    _ask_cha_audience(c.message.chat.id)


def _handle_cha_static_text(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    update_state(m.chat.id, flow="custom_handle_add", step="audience", static_text=m.text.strip())
    _ask_cha_audience(m.chat.id)


def _handle_cha_forward_prompt(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    update_state(m.chat.id, flow="custom_handle_add", step="audience", forward_prompt=m.text.strip())
    _ask_cha_audience(m.chat.id)


def _handle_cha_link_url(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    url = m.text.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        bot.send_message(m.chat.id, "❌ That doesn't look like a URL. Please send a link starting with http:// or https://")
        return
    update_state(m.chat.id, flow="custom_handle_add", step="link_text", link_url=url)
    bot.send_message(
        m.chat.id,
        "✍️ Step 3/4 (continued) — Send the caption for the link button (e.g. \"🔗 Open\"), "
        "or send - to use the default caption.",
        reply_markup=back_kb(),
    )


def _handle_cha_link_text(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    text = m.text.strip()
    link_text = None if text == "-" else text
    update_state(m.chat.id, flow="custom_handle_add", step="audience", link_text=link_text)
    _ask_cha_audience(m.chat.id)


def _ask_cha_audience(chat_id):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("👥 All users", callback_data="chaaud_all"),
        types.InlineKeyboardButton("🔒 Specific users", callback_data="chaaud_specific"),
    )
    bot.send_message(chat_id, "✍️ Step 4/4 — Who should see this button?", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data in ("chaaud_all", "chaaud_specific"))
@safe_handler
def admin_custom_handle_audience_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    state = get_state(c.message.chat.id)
    if state.get("flow") != "custom_handle_add" or state.get("step") != "audience":
        bot.answer_callback_query(c.id, "⚠️ This setup has expired — please start again.")
        return
    bot.answer_callback_query(c.id)
    if c.data == "chaaud_all":
        update_state(c.message.chat.id, audience="all")
        _finish_custom_handle_creation(c.message.chat.id)
    else:
        update_state(c.message.chat.id, step="audience_users")
        bot.send_message(
            c.message.chat.id,
            "✍️ Send the user IDs who should see this button, comma-separated "
            "(e.g. 111111,222222).",
            reply_markup=back_kb(),
        )


def _handle_cha_audience_users(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    ids = [x.strip() for x in m.text.split(",") if x.strip()]
    if not ids:
        bot.send_message(m.chat.id, "❌ Please send at least one user ID, comma-separated.")
        return
    update_state(m.chat.id, audience="specific", user_ids=ids)
    _finish_custom_handle_creation(m.chat.id)


def _finish_custom_handle_creation(chat_id):
    state = get_state(chat_id)
    action_type = state["action_type"]
    config = {"audience": state.get("audience", "all")}
    if state.get("audience") == "specific":
        config["user_ids"] = state.get("user_ids", [])
    if action_type == "static_message":
        config["text"] = state["static_text"]
    elif action_type == "forward_to_admin":
        config["prompt"] = state["forward_prompt"]
    elif action_type == "link_button":
        config["url"] = state["link_url"]
        if state.get("link_text"):
            config["text"] = state["link_text"]
    elif action_type == "feature_link":
        config["feature_key"] = state["feature_key"]

    handle_id = gen_id("HANDLE")
    create_custom_handle(handle_id, state["label"], action_type, config, chat_id)
    clear_state(chat_id)
    bot.send_message(
        chat_id,
        f"✅ Custom handle created: {state['label']}\n\n"
        f"🧾 <code>{handle_id}</code>\n"
        f"🔖 Type: {CUSTOM_HANDLE_TYPES[action_type]}\n"
        f"👥 Audience: {'All users' if config['audience'] == 'all' else 'Specific users'}\n\n"
        "It's live now — check the menu below.",
        reply_markup=main_menu(chat_id),
        parse_mode="HTML",
    )


@bot.message_handler(func=lambda m: m.text == "📋 Manage Custom Handles" and is_super_admin(m.chat.id))
@safe_handler
def admin_custom_handle_manage(m):
    if not is_admin(m.chat.id):
        return
    clear_state(m.chat.id)
    handles = list_custom_handles()
    if not handles:
        bot.send_message(m.chat.id, "📋 No custom handles yet. Tap \"➕ Add Custom Handle\" to create one.")
        return
    kb = types.InlineKeyboardMarkup()
    for h in handles:
        status = "🟢" if h["active"] else "🔴"
        kb.add(types.InlineKeyboardButton(f"{status} {h['label']}", callback_data=f"chamanage_{h['handle_id']}"))
    bot.send_message(m.chat.id, "📋 CUSTOM HANDLES\n\n🟢 = active, 🔴 = disabled. Tap one to manage it.", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("chamanage_"))
@safe_handler
def admin_custom_handle_manage_one_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    handle_id = c.data[len("chamanage_"):]
    h = get_custom_handle(handle_id)
    if h is None:
        bot.answer_callback_query(c.id, "❌ Not found — it may already be deleted.")
        return
    bot.answer_callback_query(c.id)
    try:
        cfg = json.loads(h["config_json"])
    except Exception:
        cfg = {}
    audience = "All users" if cfg.get("audience", "all") == "all" else f"Specific ({len(cfg.get('user_ids', []))} users)"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        "🔴 Disable" if h["active"] else "🟢 Enable",
        callback_data=f"chatoggle_{handle_id}",
    ))
    kb.add(types.InlineKeyboardButton("🗑️ Delete", callback_data=f"chadelete_{handle_id}"))
    bot.send_message(
        c.message.chat.id,
        f"🔧 {h['label']}\n\n"
        f"🧾 <code>{handle_id}</code>\n"
        f"🔖 Type: {CUSTOM_HANDLE_TYPES.get(h['action_type'], h['action_type'])}\n"
        f"👥 Audience: {audience}\n"
        f"📊 Status: {'🟢 Active' if h['active'] else '🔴 Disabled'}",
        reply_markup=kb,
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("chatoggle_"))
@safe_handler
def admin_custom_handle_toggle_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    handle_id = c.data[len("chatoggle_"):]
    h = get_custom_handle(handle_id)
    if h is None:
        bot.answer_callback_query(c.id, "❌ Not found.")
        return
    set_custom_handle_active(handle_id, not h["active"])
    bot.answer_callback_query(c.id, "🔴 Disabled." if h["active"] else "🟢 Enabled.")
    admin_custom_handle_manage(c.message)


@bot.callback_query_handler(func=lambda c: c.data.startswith("chadelete_"))
@safe_handler
def admin_custom_handle_delete_cb(c):
    if not is_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    handle_id = c.data[len("chadelete_"):]
    delete_custom_handle(handle_id)
    bot.answer_callback_query(c.id, "🗑️ Deleted.")
    admin_custom_handle_manage(c.message)


def _handle_custom_handle_input(m, state):
    _finish_custom_handle_forward(m, state["handle_id"], complaint_text=m.text.strip())


def _finish_custom_handle_forward(m, handle_id, complaint_text=None, media_type=None, file_id=None):
    handle = get_custom_handle(handle_id)
    label = handle["label"] if handle else handle_id
    user = get_user(m.chat.id)
    name = user["name"] if user else (m.from_user.first_name or "Unknown")
    username = f"@{m.from_user.username}" if m.from_user.username else "Not set"
    forward_text = (
        f"📨 CUSTOM HANDLE SUBMISSION — {html.escape(label)}\n\n"
        f"👤 Name: {html.escape(name)}\n"
        f"🔗 Username: {username}\n"
        f"🆔 User ID: <code>{m.chat.id}</code>\n\n"
        f"📝 Message:\n{html.escape(complaint_text) if complaint_text else '(see attached media)'}"
    )
    delivered = False
    try:
        if media_type == "photo":
            for admin_id in ADMIN_IDS:
                bot.send_photo(admin_id, file_id, caption=forward_text, parse_mode="HTML")
        elif media_type == "video":
            for admin_id in ADMIN_IDS:
                bot.send_video(admin_id, file_id, caption=forward_text, parse_mode="HTML")
        elif media_type == "voice":
            for admin_id in ADMIN_IDS:
                bot.send_voice(admin_id, file_id, caption=forward_text, parse_mode="HTML")
        elif media_type == "document":
            for admin_id in ADMIN_IDS:
                bot.send_document(admin_id, file_id, caption=forward_text, parse_mode="HTML")
        else:
            notify_admins(forward_text, parse_mode="HTML")
        delivered = True
    except Exception:
        logger.exception("Failed to deliver custom handle submission for %s", handle_id)
        delivered = False

    clear_state(m.chat.id)
    if delivered:
        bot.send_message(m.chat.id, "✅ Received — thank you! Our team will follow up if needed.", reply_markup=main_menu(m.chat.id))
    else:
        bot.send_message(m.chat.id, "⚠️ Sorry, we couldn't deliver this right now. Please try again shortly.", reply_markup=main_menu(m.chat.id))


# ================================================================
# ADMIN: AUTO MESSAGES (create/list/edit/delete scheduled broadcasts)
# ================================================================
# Each auto message has a title (for the admin's own reference), a
# body, and a daily send time (Africa/Lagos). A background thread
# (started in main(), see ENTRYPOINT) checks every ~30s and sends
# any message whose time has come, once per day, to every user.

def _format_auto_list():
    rows = list_auto_messages()
    if not rows:
        return "⏰ AUTO MESSAGES\n\nYou haven't created any yet.", None
    lines = ["⏰ AUTO MESSAGES\n"]
    kb = types.InlineKeyboardMarkup()
    for r in rows:
        state_icon = "🟢" if r["active"] else "🔴"
        if r["interval_minutes"]:
            schedule = f"every {r['interval_minutes']} min"
        else:
            schedule = f"{r['hour']:02d}:{r['minute']:02d} (Lagos, daily)"
        lines.append(f"{state_icon} {r['title']} — {schedule} — 🎯 {r['target_type']}")
        kb.row(
            types.InlineKeyboardButton(f"✏️ {r['title']}", callback_data=f"autoedit_{r['auto_id']}"),
            types.InlineKeyboardButton("🗑 Delete", callback_data=f"autodel_{r['auto_id']}"),
        )
    kb.add(types.InlineKeyboardButton("➕ Add New", callback_data="autoadd"))
    return "\n".join(lines), kb


@bot.message_handler(func=lambda m: m.text == "⏰ Auto Messages" and is_super_admin(m.chat.id))
@safe_handler
def admin_auto_list(m):
    if not is_super_admin(m.chat.id):
        return
    clear_state(m.chat.id)
    text, kb = _format_auto_list()
    if kb is None:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("➕ Add New", callback_data="autoadd"))
    bot.send_message(m.chat.id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data == "autoadd")
@safe_handler
def admin_auto_add_start(c):
    if not is_super_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    clear_state(c.message.chat.id)
    update_state(c.message.chat.id, flow="auto_add", step="title")
    bot.answer_callback_query(c.id)
    bot.send_message(
        c.message.chat.id,
        "➕ NEW AUTO MESSAGE\n\n📝 First, send a short title (for your own reference, e.g. 'Morning Reminder'):",
        reply_markup=back_kb(),
    )


def _handle_auto_add_title(m, state):
    title = m.text.strip()
    update_state(m.chat.id, step="body", title=title)
    bot.send_message(m.chat.id, "✍️ Now send the full message text you want sent out:", reply_markup=back_kb())


def _handle_auto_add_body(m, state):
    update_state(m.chat.id, step="target", body=m.text)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🤖 Bot Users", callback_data="automsgtarget:bot_users"))
    kb.add(types.InlineKeyboardButton("👥 User Group", callback_data="automsgtarget:user_group"))
    kb.add(types.InlineKeyboardButton("📢 User Channel", callback_data="automsgtarget:user_channel"))
    bot.send_message(m.chat.id, "🎯 <b>Choose where this auto message should go:</b>", parse_mode="HTML", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("automsgtarget:"))
@safe_handler
def auto_msg_target_cb(c):
    if not is_super_admin(c.message.chat.id):
        bot.answer_callback_query(c.id, "Super admin only", show_alert=True); return
    state=get_state(c.message.chat.id)
    if state.get("flow") != "auto_add" or state.get("step") != "target":
        bot.answer_callback_query(c.id, "⚠️ Expired.", show_alert=True); return
    target=c.data.split(":",1)[1]
    if target in ("user_group","user_channel") and not _community_id(target):
        bot.answer_callback_query(c.id, "Configure this destination first.", show_alert=True); return
    update_state(c.message.chat.id, step="mode", target_type=target)
    bot.answer_callback_query(c.id)
    kb=types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⏰ Daily at a fixed time", callback_data="automode_daily"))
    kb.add(types.InlineKeyboardButton("🔁 Repeat every N minutes", callback_data="automode_interval"))
    bot.send_message(c.message.chat.id, "🕒 <b>Choose the schedule:</b>", parse_mode="HTML", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data in ("automode_daily", "automode_interval"))
@safe_handler
def auto_add_mode_cb(c):
    if not is_super_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    chat_id = c.message.chat.id
    state = get_state(chat_id)
    if state.get("flow") != "auto_add" or state.get("step") != "mode":
        bot.answer_callback_query(c.id, "⚠️ Expired, please start again.")
        return
    bot.answer_callback_query(c.id)
    if c.data == "automode_daily":
        update_state(chat_id, step="time")
        bot.send_message(
            chat_id,
            "⏰ What time should this be sent every day? (Africa/Lagos time)\n\n"
            "Send in 24-hour HH:MM format, e.g. 08:00 or 21:30.",
            reply_markup=back_kb(),
        )
    else:
        update_state(chat_id, step="interval")
        bot.send_message(
            chat_id,
            "🔁 Every how many minutes should this message be resent?\n"
            "(e.g. 30 = every 30 minutes, 60 = every hour)",
            reply_markup=back_kb(),
        )


def _handle_auto_add_time(m, state):
    raw = m.text.strip()
    try:
        hh, mm = raw.split(":")
        hour, minute = int(hh), int(mm)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        bot.send_message(m.chat.id, "❌ Invalid time. Please send it as HH:MM, e.g. 08:00.")
        return

    auto_id = gen_id("AUTO")
    create_auto_message(auto_id, state["title"], state["body"], hour, minute, m.chat.id, target_type=state.get("target_type","bot_users"))
    clear_state(m.chat.id)
    bot.send_message(
        m.chat.id,
        f"✅ Auto message created!\n\n📝 Title: {state['title']}\n⏰ Time: {hour:02d}:{minute:02d} (Lagos, daily)\n\n"
        "You can edit or delete it anytime from ⏰ Auto Messages.",
        reply_markup=main_menu(m.chat.id),
    )


def _handle_auto_add_interval(m, state):
    raw = m.text.strip()
    if not raw.isdigit() or int(raw) <= 0:
        bot.send_message(m.chat.id, "❌ Please send a positive whole number of minutes, e.g. 30.")
        return
    interval = int(raw)
    auto_id = gen_id("AUTO")
    create_auto_message(auto_id, state["title"], state["body"], 0, 0, m.chat.id, interval_minutes=interval, target_type=state.get("target_type","bot_users"))
    clear_state(m.chat.id)
    bot.send_message(
        m.chat.id,
        f"✅ Auto message created!\n\n📝 Title: {state['title']}\n🔁 Repeats: every {interval} minute(s)\n\n"
        "You can edit the interval, message, or delete it anytime from ⏰ Auto Messages.",
        reply_markup=main_menu(m.chat.id),
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("autodel_"))
@safe_handler
def admin_auto_delete_cb(c):
    if not is_super_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    auto_id = c.data[len("autodel_"):]
    delete_auto_message(auto_id)
    bot.answer_callback_query(c.id, "🗑 Deleted")
    text, kb = _format_auto_list()
    if kb is None:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("➕ Add New", callback_data="autoadd"))
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    except Exception:
        bot.send_message(c.message.chat.id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("autoedit_"))
@safe_handler
def admin_auto_edit_cb(c):
    if not is_super_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    auto_id = c.data[len("autoedit_"):]
    row = get_auto_message(auto_id)
    if row is None:
        bot.answer_callback_query(c.id, "❌ Not found.")
        return
    bot.answer_callback_query(c.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✏️ Edit Title", callback_data=f"autofield_title_{auto_id}"))
    kb.add(types.InlineKeyboardButton("✏️ Edit Message", callback_data=f"autofield_body_{auto_id}"))
    kb.add(types.InlineKeyboardButton("🎯 Edit Destination", callback_data=f"autofield_target_{auto_id}"))
    if row["interval_minutes"]:
        kb.add(types.InlineKeyboardButton("🔁 Edit Interval (minutes)", callback_data=f"autofield_interval_{auto_id}"))
        schedule = f"every {row['interval_minutes']} minute(s)"
    else:
        kb.add(types.InlineKeyboardButton("⏰ Edit Time", callback_data=f"autofield_time_{auto_id}"))
        schedule = f"{row['hour']:02d}:{row['minute']:02d} (Lagos, daily)"
    toggle_label = "🔴 Deactivate" if row["active"] else "🟢 Activate"
    kb.add(types.InlineKeyboardButton(toggle_label, callback_data=f"autotoggle_{auto_id}"))
    bot.send_message(
        c.message.chat.id,
        f"✏️ EDIT: {row['title']}\n\n"
        f"🕒 Schedule: {schedule}\n"
        f"📊 Status: {'🟢 Active' if row['active'] else '🔴 Inactive'}\n\n"
        f"📝 Current message:\n{row['body']}",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("autotoggle_"))
@safe_handler
def admin_auto_toggle_cb(c):
    if not is_super_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    auto_id = c.data[len("autotoggle_"):]
    row = get_auto_message(auto_id)
    if row is None:
        bot.answer_callback_query(c.id, "❌ Not found.")
        return
    update_auto_message(auto_id, active=0 if row["active"] else 1)
    bot.answer_callback_query(c.id, "✅ Updated")
    bot.send_message(c.message.chat.id, "✅ Status updated.", reply_markup=main_menu(c.message.chat.id))


@bot.callback_query_handler(func=lambda c: c.data.startswith("autofield_"))
@safe_handler
def admin_auto_field_cb(c):
    if not is_super_admin(c.message.chat.id):
        bot.answer_callback_query(c.id)
        return
    _, field, auto_id = c.data.split("_", 2)
    row = get_auto_message(auto_id)
    if row is None:
        bot.answer_callback_query(c.id, "❌ Not found.")
        return
    clear_state(c.message.chat.id)
    update_state(c.message.chat.id, flow="auto_edit_field", field=field, auto_id=auto_id)
    bot.answer_callback_query(c.id)
    if field == "target":
        kb=types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🤖 Bot Users", callback_data=f"automsgedit_target:{auto_id}:bot_users"))
        kb.add(types.InlineKeyboardButton("👥 User Group", callback_data=f"automsgedit_target:{auto_id}:user_group"))
        kb.add(types.InlineKeyboardButton("📢 User Channel", callback_data=f"automsgedit_target:{auto_id}:user_channel"))
        bot.send_message(c.message.chat.id, "🎯 Choose the new destination:", reply_markup=kb)
        return
    prompts = {
        "title": "📝 Send the new title:",
        "body": "✍️ Send the new message text:",
        "time": "⏰ Send the new time as HH:MM (24-hour, Africa/Lagos), e.g. 08:00:",
        "interval": "🔁 Send the new interval in minutes (e.g. 30, 60, 120):",
    }
    bot.send_message(c.message.chat.id, prompts[field], reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data.startswith("automsgedit_target:"))
@safe_handler
def auto_msg_edit_target_cb(c):
    if not is_super_admin(c.message.chat.id):
        bot.answer_callback_query(c.id, "Super admin only", show_alert=True); return
    _,auto_id,target=c.data.split(":",2)
    if target in ("user_group","user_channel") and not _community_id(target):
        bot.answer_callback_query(c.id,"Configure this destination first.",show_alert=True); return
    row=get_auto_message(auto_id)
    if not row:
        bot.answer_callback_query(c.id,"Not found",show_alert=True); return
    update_auto_message(auto_id,target_type=target)
    bot.answer_callback_query(c.id,"Destination updated")
    bot.send_message(c.message.chat.id,f"✅ Destination changed to {target}.",reply_markup=main_menu(c.message.chat.id))


def _handle_auto_edit_field(m, state):
    if not is_super_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    field, auto_id = state["field"], state["auto_id"]
    if field == "title":
        update_auto_message(auto_id, title=m.text.strip())
    elif field == "body":
        update_auto_message(auto_id, body=m.text)
    elif field == "time":
        raw = m.text.strip()
        try:
            hh, mm = raw.split(":")
            hour, minute = int(hh), int(mm)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError:
            bot.send_message(m.chat.id, "❌ Invalid time. Please send it as HH:MM, e.g. 08:00.")
            return
        update_auto_message(auto_id, hour=hour, minute=minute)
    elif field == "interval":
        raw = m.text.strip()
        if not raw.isdigit() or int(raw) <= 0:
            bot.send_message(m.chat.id, "❌ Please send a positive whole number of minutes, e.g. 30.")
            return
        update_auto_message(auto_id, interval_minutes=int(raw))
    clear_state(m.chat.id)
    bot.send_message(m.chat.id, "✅ Updated.", reply_markup=main_menu(m.chat.id))


def _send_target_message(target_type, body, target_value=None):
    """Send a scheduled/manual message to the configured audience."""
    sent=0
    if target_type == "bot_users":
        for uid in all_user_ids():
            try:
                bot.send_message(uid, f"📢 {BRAND}:\n\n{body}")
                sent+=1
            except Exception:
                logger.exception("Message delivery failed to bot user %s", uid)
        return sent
    cid=_community_id(target_type)
    if cid:
        bot.send_message(cid, f"📢 {BRAND}:\n\n{body}")
        return 1
    return 0


def auto_message_scheduler():
    """Background loop: every ~30s, checks whether any active auto
    message's daily send-time has arrived (Africa/Lagos) and, if so,
    sends it to every user once, then marks it sent for the day so it
    doesn't repeat until tomorrow."""
    while True:
        try:
            now = datetime.now(LAGOS_TZ)
            today_str = now.strftime("%Y-%m-%d")
            for r in list_auto_messages():
                if not r["active"]:
                    continue

                if r["interval_minutes"]:
                    # Repeat-every-N-minutes mode: ignores the daily
                    # hour/minute fields entirely and instead tracks
                    # the last send timestamp.
                    due = True
                    if r["last_sent_at"]:
                        try:
                            last = datetime.fromisoformat(r["last_sent_at"])
                            due = (datetime.now(timezone.utc) - last) >= timedelta(minutes=r["interval_minutes"])
                        except ValueError:
                            due = True
                    if not due:
                        continue
                    sent = _send_target_message(r["target_type"], r["body"], r["target_value"])
                    mark_auto_message_sent_at(r["auto_id"], now_iso())
                    logger.info(
                        "Auto message %s ('%s') sent to %d destinations (every %d min)",
                        r["auto_id"], r["title"], sent, r["interval_minutes"],
                    )
                    with db_tx() as conn:
                        conn.execute("INSERT INTO audit_log(admin_id,action,target_user,amount,txn_id,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                                     (str(r["created_by"] or "SYSTEM"),"AUTO_MESSAGE_SENT",None,None,r["auto_id"],f"target={r['target_type']}; delivered={sent}",now_iso()))
                    continue

                if r["last_sent_date"] == today_str:
                    continue
                if now.hour == r["hour"] and now.minute == r["minute"]:
                    sent = _send_target_message(r["target_type"], r["body"], r["target_value"])
                    mark_auto_message_sent(r["auto_id"], today_str)
                    logger.info("Auto message %s ('%s') sent to %d destinations", r["auto_id"], r["title"], sent)
                    with db_tx() as conn:
                        conn.execute("INSERT INTO audit_log(admin_id,action,target_user,amount,txn_id,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                                     (str(r["created_by"] or "SYSTEM"),"AUTO_MESSAGE_SENT",None,None,r["auto_id"],f"target={r['target_type']}; delivered={sent}",now_iso()))
        except Exception:
            logger.exception("Auto message scheduler loop error")
        time.sleep(30)



@bot.message_handler(func=lambda m: m.text == btn_label("buy_sell_mail"))
@safe_handler
def buy_mail_menu(m):
    if feature_blocked_message(m, "buy_sell_mail"):
        return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for o in list_menu_options("mail_option"):
        kb.add(o["label"])
    kb.add("🔙 Back")
    bot.send_message(m.chat.id, "Please select what you wish to purchase:", reply_markup=kb)


def _mail_option_labels():
    return {r["label"] for r in list_menu_options("mail_option")}


@bot.message_handler(func=lambda m: m.text in _mail_option_labels())
@safe_handler
def contact_for_purchase(m):
    contact_info = (
        f"To purchase {m.text}, please contact our official agent:\n\n"
        "👤 Telegram Agent: https://t.me/ahmerdeebbr\n"
        "📱 WhatsApp Agent:\n"
        "https://wa.me/2347083324469?text=I%20am%20a%20customer%20from%20mobile%20Digital"
        "%20Hub%20on%20Telegram.%20I%20need%20to%20BUY%20or%20SELL%20something%20right%20now."
    )
    bot.send_message(m.chat.id, contact_info, reply_markup=main_menu(m.chat.id))


# ================================================================
# WORK SUBMISSION
# ================================================================
# Work categories (e.g. "🔵 Facebook Work") and their sub-types
# (e.g. "🆔 Facebook Cookies") now live in the menu_options table
# instead of being hardcoded, so the admin can add/rename/remove them
# from "🧩 Menu Editor" — no code changes, no redeploy.

@bot.message_handler(func=lambda m: m.text == btn_label("submit_work"))
@safe_handler
def submit_work(m):
    if feature_blocked_message(m, "submit_work"):
        return
    clear_state(m.chat.id)
    update_state(m.chat.id, flow="work")
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    cats = [r["label"] for r in list_menu_options("work_category")]
    for pair in _rows_of_two(cats):
        kb.row(*pair)
    kb.row("🔙 Back")
    bot.send_message(m.chat.id, "Choose work type:", reply_markup=kb)


def _work_category_labels():
    return {r["label"] for r in list_menu_options("work_category")}


@bot.message_handler(func=lambda m: m.text in _work_category_labels())
@safe_handler
def work_category_selected(m):
    state = get_state(m.chat.id)
    if state.get("flow") != "work":
        return
    cat = get_menu_option_by_label("work_category", m.text.strip())
    if cat is None:
        return
    update_state(m.chat.id, flow="work", work_type=cat["label"], work_category_id=cat["option_id"])
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for s in list_menu_options("work_subtype", parent_key=cat["option_id"]):
        kb.add(s["label"])
    kb.add("🔙 Back")
    bot.send_message(m.chat.id, f"Choose {cat['label']} type:", reply_markup=kb)


def _work_subtype_labels():
    return {r["label"] for r in fetchall("SELECT label FROM menu_options WHERE section='work_subtype' AND active=1")}


@bot.message_handler(func=lambda m: m.text in _work_subtype_labels())
@safe_handler
def fb_subtype(m):
    state = get_state(m.chat.id)
    if state.get("flow") != "work":
        return
    update_state(m.chat.id, sub_type=m.text)
    bot.send_message(m.chat.id, "📤 Please upload proof (Sheet File / Photo):", reply_markup=back_kb())


def _extract_file_id(m):
    if m.content_type == "photo":
        return m.photo[-1].file_id
    if m.content_type == "video":
        return m.video.file_id
    if m.content_type == "document":
        return m.document.file_id
    if m.content_type == "voice":
        return m.voice.file_id
    return None


@bot.message_handler(content_types=["photo", "video", "document", "voice"])
@safe_handler
def receive_media(m):
    """Dispatches incoming media by the user's current flow: either a
    Work Submission proof, or a Support complaint attachment."""
    state = get_state(m.chat.id)
    flow = state.get("flow")

    if flow == "fund_wallet" and state.get("step") == "proof":
        if m.content_type != "photo":
            bot.send_message(m.chat.id, "📸 Please send the payment receipt as a photo/screenshot only.", reply_markup=back_kb())
        else:
            fund_proof_photo(m)
    elif flow == "work" and state.get("sub_type"):
        _receive_work_proof(m, state)
    elif flow == "support":
        _finish_support_ticket(m, complaint_text=None, media_type=m.content_type, file_id=_extract_file_id(m))
    elif flow == "custom_handle_input":
        _finish_custom_handle_forward(m, state["handle_id"], complaint_text=None, media_type=m.content_type, file_id=_extract_file_id(m))
    # else: not in a flow that accepts media — silently ignore.


def _receive_work_proof(m, state):
    file_id = _extract_file_id(m)

    sub_id = gen_id("SUB")
    create_submission(
        sub_id, m.chat.id, state.get("work_type"), state.get("sub_type"), file_id, m.content_type,
    )
    clear_state(m.chat.id)

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Approve", callback_data=f"sub_approve_{sub_id}"),
        types.InlineKeyboardButton("❌ Reject", callback_data=f"sub_reject_{sub_id}"),
    )
    caption = (
        f"📥 NEW SUBMISSION\n👤 ID: <code>{m.chat.id}</code>\n💼 Work: {state.get('work_type')}\n"
        f"🔖 Type: {state.get('sub_type')}\n🧾 <code>{sub_id}</code>"
    )
    for admin_id in ADMIN_IDS:
        try:
            if m.content_type == "photo":
                sent = bot.send_photo(admin_id, file_id, caption=caption, reply_markup=kb, parse_mode="HTML")
            elif m.content_type == "video":
                sent = bot.send_video(admin_id, file_id, caption=caption, reply_markup=kb, parse_mode="HTML")
            else:
                sent = bot.send_document(admin_id, file_id, caption=caption, reply_markup=kb, parse_mode="HTML")
            # Remember which message this admin got, so that once the
            # submission is approved and posted to the work channel we
            # can auto-delete it here and keep the admin's chat tidy.
            with db_tx() as conn:
                conn.execute(
                    "INSERT INTO submission_admin_msgs (sub_id, admin_id, message_id) VALUES (?, ?, ?)",
                    (sub_id, str(admin_id), sent.message_id),
                )
        except Exception:
            logger.exception("Failed to forward submission to admin %s", admin_id)

    bot.send_message(
        m.chat.id,
        "✅ SUBMISSION RECEIVED\n\n"
        "Your work has been successfully submitted. 📥\n\n"
        "⏳ Please wait a few minutes while our agents review your submission.\n\n"
        "Our agents will either:\n\n✅ APPROVE\n\nor\n\n❌ REJECT\n\nyour submission.\n\n"
        f"🧾 Reference: <code>{sub_id}</code>\n\n"
        "📌 Please do not submit the same work repeatedly while waiting.\n\n"
        "Thank you for your patience. 💙",
        reply_markup=main_menu(m.chat.id),
        parse_mode="HTML",
    )


def _delete_submission_admin_messages(sub_id):
    """Delete the proof (photo/video/document) that was sent to every
    admin's chat for this submission. Called once the submission has
    been posted to the work channel, so admin chats don't fill up with
    old submissions."""
    rows = fetchall("SELECT admin_id, message_id FROM submission_admin_msgs WHERE sub_id=?", (sub_id,))
    for row in rows:
        try:
            bot.delete_message(row["admin_id"], row["message_id"])
        except Exception:
            # Message may already be gone, too old to delete, or the
            # admin may have blocked/left — safe to ignore.
            logger.exception("Failed to delete submission %s message for admin %s", sub_id, row["admin_id"])
    with db_tx() as conn:
        conn.execute("DELETE FROM submission_admin_msgs WHERE sub_id=?", (sub_id,))


@bot.callback_query_handler(func=lambda c: c.data.startswith("sub_approve_"))
@safe_handler
def sub_approve_cb(c):
    sub_id = c.data[len("sub_approve_"):]
    try:
        row = approve_submission(sub_id, c.from_user.id)
    except SubmissionStateError as e:
        bot.answer_callback_query(c.id, f"❌ {e}")
        return
    bot.answer_callback_query(c.id, "Recorded")
    try:
        bot.edit_message_caption(
            f"✅ Processed: APPROVED\n🧾 <code>{sub_id}</code>", c.message.chat.id, c.message.message_id,
            parse_mode="HTML",
        )
    except Exception:
        pass

    lg = lagos_parts()
    _, _wa_default = TEXT_TEMPLATES["work_approved"]
    bot.send_message(
        row["user_id"],
        render_text(
            "work_approved", _wa_default,
            sub_id=sub_id, work_type=row["work_type"], sub_type=row["sub_type"],
            date=lg["date"], day=lg["day"], time=lg["time"], tz=lg["tz"], brand=BRAND,
        ),
        parse_mode="HTML",
    )

    work_channel_id = _community_id("user_channel") or WORK_CHANNEL_ID
    if work_channel_id:
        submitter = get_user(row["user_id"])
        uname = display_username(submitter)
        channel_post = (
            "🎉 NEW APPROVED WORK\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"🧾 Submission ID: <code>{sub_id}</code>\n"
            f"💼 Work: {row['work_type']}\n"
            f"📌 Type: {row['sub_type']}\n"
            f"👤 Submitted By: {html.escape(uname)}\n"
            f"🆔 User ID: <code>{row['user_id']}</code>\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "✅ STATUS: APPROVED\n\n"
            f"📅 Date: {lg['date']}\n"
            f"📅 Day: {lg['day']}\n"
            f"⏰ Time: {lg['time']}\n"
            f"🇳🇬 {lg['tz']}\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"🚀 {BRAND}"
        )
        try:
            if row["file_type"] == "photo" and row["file_id"]:
                bot.send_photo(work_channel_id, row["file_id"], caption=channel_post, parse_mode="HTML")
            elif row["file_type"] == "video" and row["file_id"]:
                bot.send_video(work_channel_id, row["file_id"], caption=channel_post, parse_mode="HTML")
            elif row["file_id"]:
                bot.send_document(work_channel_id, row["file_id"], caption=channel_post, parse_mode="HTML")
            else:
                bot.send_message(work_channel_id, channel_post, parse_mode="HTML")
            # Posted to the channel successfully — clean up every
            # admin's copy of the submission so chats don't pile up.
            _delete_submission_admin_messages(sub_id)
        except Exception:
            logger.exception("Failed to post approved work %s to work channel", sub_id)
            notify_admins(f"⚠️ Could not publish approved work {sub_id} to the work channel. Check WORK_CHANNEL_ID / bot membership.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("sub_reject_"))
@safe_handler
def sub_reject_cb(c):
    sub_id = c.data[len("sub_reject_"):]
    row = fetchone("SELECT * FROM submissions WHERE sub_id=?", (sub_id,))
    if row is None:
        bot.answer_callback_query(c.id, "❌ Submission not found.")
        return
    if row["status"] != "PENDING":
        bot.answer_callback_query(c.id, f"❌ Already {row['status']}.")
        return
    update_state(c.from_user.id, flow="sub_reject_reason", sub_id=sub_id)
    bot.answer_callback_query(c.id)
    bot.send_message(c.from_user.id, f"📝 Enter the rejection reason for submission <code>{sub_id}</code>:",
                      reply_markup=back_kb(), parse_mode="HTML")


def _handle_sub_reject_reason(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    sub_id = state["sub_id"]
    reason = m.text.strip()
    try:
        row = reject_submission(sub_id, m.chat.id, reason)
    except SubmissionStateError as e:
        bot.send_message(m.chat.id, f"❌ {e}")
        clear_state(m.chat.id)
        return
    clear_state(m.chat.id)
    count = get_reject_count(row["user_id"])
    lg = lagos_parts()
    # Rejected work is never posted to the channel, so we just drop the
    # tracking rows here (table hygiene) without touching the messages
    # still sitting in admin chats.
    with db_tx() as conn:
        conn.execute("DELETE FROM submission_admin_msgs WHERE sub_id=?", (sub_id,))
    bot.send_message(m.chat.id, f"❌ Marked as REJECTED.\n🧾 <code>{sub_id}</code>",
                      reply_markup=main_menu(m.chat.id), parse_mode="HTML")
    _, _wr_default = TEXT_TEMPLATES["work_rejected"]
    bot.send_message(
        row["user_id"],
        render_text(
            "work_rejected", _wr_default,
            sub_id=sub_id, work_type=row["work_type"], sub_type=row["sub_type"],
            date=lg["date"], day=lg["day"], time=lg["time"], tz=lg["tz"], count=count,
        ),
        parse_mode="HTML",
    )


# ================================================================
# BANK DETAILS  (Bank / Wallet / Crypto — admin-approval workflow)
# ================================================================
# Flow: user picks a category → picks a specific method from a list
# (reply keyboard, one selection removes the list) → types the
# detail. It is then held PENDING and sent to every admin with
# Approve/Decline buttons. Only once an admin taps Approve is it:
#   1) saved into bank_details (the single "gathered" record used
#      everywhere else, e.g. the withdrawal gate), and
#   2) posted into BANK_GROUP_ID, the one place every approved
#      bank/wallet/crypto detail is collected.
# Every admin's pending copy is then deleted so admin chats don't
# fill up with old requests.

BANK_LIST = [
    "Access Bank", "GTBank", "Zenith Bank", "UBA",
    "First Bank", "Fidelity Bank", "Union Bank", "Sterling Bank",
    "Wema Bank", "Polaris Bank", "Stanbic IBTC", "Ecobank",
    "FCMB", "Keystone Bank", "Providus Bank", "Other Bank",
]

WALLET_LIST = ["OPay", "Moniepoint", "Smartcash", "MoMo PSB", "Kuda", "palmpay"]

CRYPTO_LIST = ["Binance", "Bybit", "Write Full Other Crypto"]


def _rows_of_two(items):
    """Chunk a flat list of button labels into pairs for kb.row()."""
    return [items[i:i + 2] for i in range(0, len(items), 2)]


def _list_kb(items):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for pair in _rows_of_two(items):
        kb.row(*pair)
    kb.row("🔙 Back")
    return kb


@bot.message_handler(func=lambda m: m.text == btn_label("bank_details"))
@safe_handler
def bank_menu(m):
    if feature_blocked_message(m, "bank_details"):
        return
    clear_state(m.chat.id)
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("💱 Crypto")
    kb.row("🔙 Back")
    bot.send_message(m.chat.id, "Select your USDT withdrawal destination:", reply_markup=kb)


@bot.message_handler(func=lambda m: False)
@safe_handler
def bank_category_banks(m):
    update_state(m.chat.id, flow="bank", step="choose_method", category="bank")
    bot.send_message(m.chat.id, "Select your bank:", reply_markup=_list_kb(BANK_LIST))


@bot.message_handler(func=lambda m: False)
@safe_handler
def bank_category_wallet(m):
    update_state(m.chat.id, flow="bank", step="choose_method", category="wallet")
    bot.send_message(m.chat.id, "Select your wallet:", reply_markup=_list_kb(WALLET_LIST))


@bot.message_handler(func=lambda m: m.text == "💱 Crypto")
@safe_handler
def bank_category_crypto(m):
    update_state(m.chat.id, flow="bank", step="choose_method", category="crypto")
    bot.send_message(m.chat.id, "Select your crypto exchange/wallet:", reply_markup=_list_kb(CRYPTO_LIST))


@bot.message_handler(func=lambda m: get_state(m.chat.id).get("flow") == "bank"
                      and get_state(m.chat.id).get("step") == "choose_method")
@safe_handler
def choose_bank_method(m):
    state = get_state(m.chat.id)
    category = state.get("category")
    method = m.text.strip()

    valid = {"bank": BANK_LIST, "wallet": WALLET_LIST, "crypto": CRYPTO_LIST}.get(category, [])
    if method not in valid:
        bot.send_message(
            m.chat.id,
            "❌ That is not a valid option.\n\n"
            "👉 Please tap one of the buttons below to continue — nothing has been "
            "sent or saved.",
            reply_markup=_list_kb(valid),
        )
        return

    update_state(m.chat.id, flow="bank", step="write", category=category, method=method)

    if category == "crypto":
        if method == "Binance":
            prompt = "✍️ Please send your Binance ID only."
        else:
            prompt = f"✍️ Please send your {method} wallet address / ID."
    else:
        prompt = f"✍️ Please send your {method} Account Number & Name (e.g. 0123456789 - John Doe)."

    bot.send_message(m.chat.id, prompt, reply_markup=back_kb())


def _handle_bank_write(m, state):
    category = state["category"]
    method = state["method"]
    details = m.text.strip()
    clear_state(m.chat.id)

    bank_id = gen_id("BANKREQ")
    create_bank_submission(bank_id, m.chat.id, category, method, details)

    user = get_user(m.chat.id)
    name = user["name"] if user else (m.from_user.first_name or "Unknown")

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Approve", callback_data=f"bank_approve_{bank_id}"),
        types.InlineKeyboardButton("❌ Decline", callback_data=f"bank_decline_{bank_id}"),
    )
    admin_text = (
        f"💰 NEW {category.upper()} DETAILS — PENDING APPROVAL\n\n"
        f"👤 User: {html.escape(name)}\n🆔 ID: <code>{m.chat.id}</code>\n"
        f"🏷️ Category: {category.capitalize()}\n"
        f"🏦 Method: {method}\n📝 Details: {html.escape(details)}\n\n"
        f"🧾 <code>{bank_id}</code>"
    )
    for admin_id in ADMIN_IDS:
        try:
            sent = bot.send_message(admin_id, admin_text, reply_markup=kb, parse_mode="HTML")
            with db_tx() as conn:
                conn.execute(
                    "INSERT INTO bank_admin_msgs (bank_id, admin_id, message_id) VALUES (?, ?, ?)",
                    (bank_id, str(admin_id), sent.message_id),
                )
        except Exception:
            logger.exception("Failed to forward bank submission %s to admin %s", bank_id, admin_id)

    bot.send_message(
        m.chat.id,
        "✅ SUBMITTED\n\n"
        "Your details have been received and are awaiting admin approval. 🕒\n\n"
        f"🧾 Reference: <code>{bank_id}</code>\n\n"
        "You'll be notified as soon as it's reviewed.",
        reply_markup=main_menu(m.chat.id),
        parse_mode="HTML",
    )


def _delete_bank_admin_messages(bank_id):
    """Delete the pending-approval message sent to every admin for this
    bank submission, once it has been approved/declined, so admin
    chats don't fill up with old requests."""
    rows = fetchall("SELECT admin_id, message_id FROM bank_admin_msgs WHERE bank_id=?", (bank_id,))
    for row in rows:
        try:
            bot.delete_message(row["admin_id"], row["message_id"])
        except Exception:
            logger.exception("Failed to delete bank submission %s message for admin %s", bank_id, row["admin_id"])
    with db_tx() as conn:
        conn.execute("DELETE FROM bank_admin_msgs WHERE bank_id=?", (bank_id,))


@bot.callback_query_handler(func=lambda c: c.data.startswith("bank_approve_"))
@safe_handler
def bank_approve_cb(c):
    bank_id = c.data[len("bank_approve_"):]
    try:
        row = approve_bank_submission(bank_id, c.from_user.id)
    except SubmissionStateError as e:
        bot.answer_callback_query(c.id, f"❌ {e}")
        return
    except ValueError as e:
        bot.answer_callback_query(c.id, f"❌ {e}")
        return
    bot.answer_callback_query(c.id, "✅ Approved")

    lg = lagos_parts()
    bank_store_channel_id = _community_id("bank_store_channel") or BANK_GROUP_ID
    if bank_store_channel_id:
        user = get_user(row["user_id"])
        uname = display_username(user)
        group_text = (
            "💰 NEW APPROVED PAYMENT DETAILS\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 User: {html.escape(uname)}\n🆔 User ID: <code>{row['user_id']}</code>\n"
            f"🏷️ Category: {row['category'].capitalize()}\n"
            f"🏦 Method: {row['method']}\n📝 Details: {html.escape(row['details'])}\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"📅 Date: {lg['date']}\n⏰ Time: {lg['time']}\n🇳🇬 {lg['tz']}\n\n"
            f"🧾 <code>{bank_id}</code>\n\n"
            f"🏢 {BRAND}"
        )
        try:
            bot.send_message(bank_store_channel_id, group_text, parse_mode="HTML")
        except Exception:
            logger.exception("Failed to post approved bank details %s to BANK_GROUP_ID", bank_id)
            notify_admins(f"⚠️ Could not publish approved bank details {bank_id} to the group. Check BANK_GROUP_ID / bot membership.")

    _delete_bank_admin_messages(bank_id)

    _, _ba_default = TEXT_TEMPLATES["bank_approved"]
    bot.send_message(
        row["user_id"],
        render_text(
            "bank_approved", _ba_default,
            category=row["category"], method=row["method"], bank_id=bank_id, brand=BRAND,
        ),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("bank_decline_"))
@safe_handler
def bank_decline_cb(c):
    bank_id = c.data[len("bank_decline_"):]
    row = get_bank_submission(bank_id)
    if row is None:
        bot.answer_callback_query(c.id, "❌ Submission not found.")
        return
    if row["status"] != "PENDING":
        bot.answer_callback_query(c.id, f"❌ Already {row['status']}.")
        return
    update_state(c.from_user.id, flow="bank_decline_reason", bank_id=bank_id)
    bot.answer_callback_query(c.id)
    bot.send_message(
        c.from_user.id,
        f"📝 Please enter the reason for declining <code>{bank_id}</code>:",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )


def _handle_bank_decline_reason(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id)
        return
    bank_id = state["bank_id"]
    reason = m.text.strip()
    try:
        row = decline_bank_submission(bank_id, m.chat.id, reason)
    except (SubmissionStateError, ValueError) as e:
        bot.send_message(m.chat.id, f"❌ {e}")
        clear_state(m.chat.id)
        return

    clear_state(m.chat.id)
    _delete_bank_admin_messages(bank_id)

    bot.send_message(m.chat.id, f"❌ Marked as DECLINED.\n🧾 <code>{bank_id}</code>",
                      reply_markup=main_menu(m.chat.id), parse_mode="HTML")
    _, _bd_default = TEXT_TEMPLATES["bank_declined"]
    bot.send_message(
        row["user_id"],
        render_text(
            "bank_declined", _bd_default,
            category=row["category"], method=row["method"], reason=html.escape(reason),
        ),
        parse_mode="HTML",
    )


# ================================================================
# FUND WALLET — USER + ADMIN
# ================================================================

@bot.message_handler(func=lambda m: m.text == "💳 Fund Wallet")
@safe_handler
def fund_wallet_menu(m):
    methods = list_fund_methods(active_only=True)
    if not methods:
        bot.send_message(m.chat.id, "💳 Funding is currently unavailable. Please try again later.", reply_markup=main_menu(m.chat.id))
        return
    kb = types.InlineKeyboardMarkup()
    for row in methods:
        kb.add(types.InlineKeyboardButton(f"💳 Fund With {row['name']} ({row['currency_code']})", callback_data=f"fund_method:{row['method_id']}"))
    bot.send_message(m.chat.id, "💳 <b>Fund Your Wallet</b>\n\nChoose how you want to fund your wallet:", parse_mode="HTML", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("fund_method:"))
@safe_handler
def fund_method_cb(c):
    method_id = c.data.split(":", 1)[1]
    row = get_fund_method(method_id)
    if not row or not int(row['active']):
        return bot.answer_callback_query(c.id, "This funding method is unavailable.", show_alert=True)
    clear_state(c.from_user.id)
    update_state(c.from_user.id, flow="fund_wallet", step="amount", fund_method_id=method_id)
    bot.answer_callback_query(c.id)
    bot.send_message(c.from_user.id, _fund_method_text(row) + f"\n\n💰 Enter amount in {row['currency_code']}:" , parse_mode="HTML", reply_markup=back_kb())


def _handle_fund_amount(m, state):
    row = get_fund_method(state.get('fund_method_id'))
    if not row or not int(row['active']):
        clear_state(m.chat.id)
        bot.send_message(m.chat.id, "❌ This funding method is no longer available.", reply_markup=main_menu(m.chat.id))
        return
    try:
        amount = float(m.text.replace(',', '').strip())
    except (TypeError, ValueError):
        bot.send_message(m.chat.id, f"❌ Enter a valid amount in {row['currency_code']}.")
        return
    if amount <= 0:
        bot.send_message(m.chat.id, "❌ Amount must be greater than 0.")
        return
    usdt = round(amount * float(row['rate_usdt']), 6)
    if usdt <= 0:
        bot.send_message(m.chat.id, "❌ The calculated USDT amount is invalid.")
        return
    update_state(m.chat.id, step="proof", fund_method_id=row['method_id'], fund_amount=amount, fund_usdt=usdt)
    bot.send_message(
        m.chat.id,
        f"💰 Amount received: <b>{amount:,.2f} {html.escape(row['currency_code'])}</b>\n"
        f"🪙 You will receive: <b>{usdt:.6f} USDT</b>\n\n"
        "📸 Now send the payment receipt as a <b>photo/screenshot only</b>.\n"
        "After admin approval, the USDT will be added to your balance.",
        parse_mode="HTML", reply_markup=back_kb()
    )


@bot.message_handler(content_types=["photo"], func=lambda m: get_state(m.chat.id).get("flow") == "fund_wallet" and get_state(m.chat.id).get("step") == "proof")
@safe_handler
def fund_proof_photo(m):
    state = get_state(m.chat.id)
    row = get_fund_method(state.get('fund_method_id'))
    if not row:
        clear_state(m.chat.id)
        return bot.send_message(m.chat.id, "❌ Funding method not found.", reply_markup=main_menu(m.chat.id))
    amount = float(state.get('fund_amount', 0))
    usdt = float(state.get('fund_usdt', 0))
    request_id = gen_id("FUND")
    proof_id = m.photo[-1].file_id
    create_fund_request(request_id, m.chat.id, row['method_id'], row['currency_code'], amount, usdt, proof_id, "photo")
    clear_state(m.chat.id)
    bot.send_message(m.chat.id, f"⏳ <b>Funding submitted</b>\n\n🧾 Request: <code>{request_id}</code>\n💰 Sent: {amount:,.2f} {html.escape(row['currency_code'])}\n🪙 Credit: {usdt:.6f} USDT\n\nStatus: PENDING", parse_mode="HTML", reply_markup=main_menu(m.chat.id))
    user = get_user(m.chat.id)
    name = html.escape(user['name'] if user else str(m.chat.id))
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("✅ Approve", callback_data=f"fund_approve:{request_id}"), types.InlineKeyboardButton("❌ Decline", callback_data=f"fund_decline:{request_id}"))
    caption = (f"💳 <b>NEW FUNDING REQUEST</b>\n\n👤 {name}\n🆔 <code>{m.chat.id}</code>\n\n"
               f"🏷 Method: <b>{html.escape(row['name'])}</b>\n💱 Currency: {html.escape(row['currency_code'])}\n"
               f"💰 Sent: <b>{amount:,.2f} {html.escape(row['currency_code'])}</b>\n🪙 Credit: <b>{usdt:.6f} USDT</b>\n"
               f"🧾 Request: <code>{request_id}</code>")
    destinations=[]
    for kind in ("submission_channel","bank_store_channel"):
        cid=_community_id(kind)
        if cid and cid not in destinations: destinations.append(cid)
    if destinations:
        for cid in destinations:
            try:
                bot.send_photo(cid, proof_id, caption=caption, parse_mode="HTML", reply_markup=kb)
            except Exception:
                logger.exception("Failed to send funding proof to %s", cid)
    else:
        for admin_id in ADMIN_IDS:
            try: bot.send_photo(admin_id, proof_id, caption=caption, parse_mode="HTML", reply_markup=kb)
            except Exception: logger.exception("Failed to send funding proof to admin %s", admin_id)


@bot.message_handler(func=lambda m: get_state(m.chat.id).get("flow") == "fund_wallet" and get_state(m.chat.id).get("step") == "proof")
@safe_handler
def fund_proof_text_block(m):
    bot.send_message(m.chat.id, "📸 Please send the payment receipt as a photo/screenshot only.", reply_markup=back_kb())


@bot.callback_query_handler(func=lambda c: c.data.startswith("fund_approve:"))
@safe_handler
def fund_approve_cb(c):
    if not is_admin(c.from_user.id):
        return bot.answer_callback_query(c.id, "Admin only", show_alert=True)
    request_id = c.data.split(":", 1)[1]
    row = get_fund_request(request_id)
    if not row:
        return bot.answer_callback_query(c.id, "Request not found.", show_alert=True)
    try:
        row, entry = approve_fund_request(request_id, c.from_user.id)
    except ValueError as e:
        return bot.answer_callback_query(c.id, str(e), show_alert=True)
    bot.answer_callback_query(c.id, "Approved")
    try:
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
    except Exception:
        pass
    bot.send_message(row['user_id'], f"✅ <b>Funding approved</b>\n\n🧾 Request: <code>{request_id}</code>\n🪙 Added: <b>{float(row['usdt_amount']):.6f} USDT</b>\n💰 New balance: <b>{_otp_balance(row['user_id']):.6f} USDT</b>\n\nTransaction: <code>{entry['txn_id']}</code>", parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data.startswith("fund_decline:"))
@safe_handler
def fund_decline_cb(c):
    if not is_admin(c.from_user.id):
        return bot.answer_callback_query(c.id, "Admin only", show_alert=True)
    request_id = c.data.split(":", 1)[1]
    row = get_fund_request(request_id)
    if not row or row['status'] != 'PENDING':
        return bot.answer_callback_query(c.id, "Request is no longer pending.", show_alert=True)
    update_state(c.from_user.id, flow="fund_decline", step="reason", fund_request_id=request_id)
    bot.answer_callback_query(c.id)
    bot.send_message(c.from_user.id, f"📝 Enter the reason for declining <code>{request_id}</code>:", parse_mode="HTML", reply_markup=back_kb())


def _handle_fund_decline_reason(m, state):
    if not is_admin(m.chat.id):
        clear_state(m.chat.id); return
    request_id = state.get('fund_request_id')
    reason = m.text.strip()
    if not reason:
        bot.send_message(m.chat.id, "❌ Please enter a reason."); return
    try:
        row = decline_fund_request(request_id, m.chat.id, reason)
    except ValueError as e:
        clear_state(m.chat.id); bot.send_message(m.chat.id, f"❌ {e}", reply_markup=main_menu(m.chat.id)); return
    clear_state(m.chat.id)
    bot.send_message(m.chat.id, f"❌ Funding request <code>{request_id}</code> declined.", parse_mode="HTML", reply_markup=main_menu(m.chat.id))
    bot.send_message(row['user_id'], f"❌ <b>Funding declined</b>\n\n🧾 Request: <code>{request_id}</code>\n📝 Reason: {html.escape(reason)}", parse_mode="HTML")


# ---------- ADMIN: FUNDING METHODS ----------
@bot.message_handler(func=lambda m: m.text == "💳 Fund Wallet Settings" and is_super_admin(m.chat.id))
@safe_handler
def fund_admin_menu(m):
    clear_state(m.chat.id)
    rows = list_fund_methods()
    kb = types.InlineKeyboardMarkup()
    for r in rows:
        kb.add(types.InlineKeyboardButton(f"{'🟢' if r['active'] else '🔴'} {r['name']} | 1 {r['currency_code']} = {float(r['rate_usdt']):.8f} USDT", callback_data=f"fund_admin_method:{r['method_id']}"))
    kb.add(types.InlineKeyboardButton("➕ Add Funding Method", callback_data="fund_admin_add"))
    bot.send_message(m.chat.id, "💳 <b>FUND WALLET SETTINGS</b>\n\nAdmin can add any currency/payment method, set its rate and payment account/address. User deposits are credited in USDT only.", parse_mode="HTML", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data == "fund_admin_add")
@safe_handler
def fund_admin_add_cb(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id, "Admin only", show_alert=True)
    clear_state(c.from_user.id); update_state(c.from_user.id, flow="fund_admin_add", step="name")
    bot.answer_callback_query(c.id); bot.send_message(c.from_user.id, "✏️ Enter method name. Example: Binance / Bybit / USD Wallet / Nigerian Bank:", reply_markup=back_kb())


def _handle_fund_admin_add(m, state):
    if not is_super_admin(m.chat.id):
        clear_state(m.chat.id); return
    step=state.get('step')
    value=m.text.strip()
    if not value: return bot.send_message(m.chat.id,"❌ Value cannot be empty.")
    if step=='name':
        update_state(m.chat.id, step='currency', fund_name=value)
        return bot.send_message(m.chat.id,"💱 Enter the currency code. Example: USD, NGN, AED, GBP:",reply_markup=back_kb())
    if step=='currency':
        update_state(m.chat.id, step='rate', fund_currency=value.upper())
        return bot.send_message(m.chat.id,f"💱 Enter rate as: 1 {value.upper()} = how many USDT?\nExample: 1 USD = 1 USDT or 1 NGN = 0.000667 USDT",reply_markup=back_kb())
    if step=='rate':
        try: rate=float(value.replace(',',''))
        except: return bot.send_message(m.chat.id,"❌ Invalid rate. Send a number.")
        if rate<=0: return bot.send_message(m.chat.id,"❌ Rate must be greater than 0.")
        update_state(m.chat.id, step='destination', fund_rate=rate)
        return bot.send_message(m.chat.id,"📥 Enter the exact account number, wallet address, ID, or payment instructions the user should receive:",reply_markup=back_kb())
    if step=='destination':
        method_id=gen_id('FUND')
        create_fund_method(method_id,state['fund_name'],state['fund_currency'],state['fund_rate'],value,m.chat.id)
        clear_state(m.chat.id)
        return bot.send_message(m.chat.id,f"✅ Funding method added.\n\n🧾 ID: <code>{method_id}</code>",parse_mode='HTML',reply_markup=main_menu(m.chat.id))


@bot.callback_query_handler(func=lambda c: c.data.startswith("fund_admin_method:"))
@safe_handler
def fund_admin_method_cb(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,"Admin only",show_alert=True)
    mid=c.data.split(":",1)[1]; row=get_fund_method(mid)
    if not row: return bot.answer_callback_query(c.id,"Not found",show_alert=True)
    kb=types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔴 Disable" if row['active'] else "🟢 Enable",callback_data=f"fund_admin_toggle:{mid}"))
    kb.add(types.InlineKeyboardButton("🗑 Delete",callback_data=f"fund_admin_delete:{mid}"))
    kb.add(types.InlineKeyboardButton("⬅️ Back",callback_data="fund_admin_back"))
    bot.answer_callback_query(c.id)
    bot.send_message(c.from_user.id,_fund_method_text(row)+f"\n\nStatus: {'ACTIVE' if row['active'] else 'DISABLED'}",parse_mode='HTML',reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("fund_admin_toggle:"))
@safe_handler
def fund_admin_toggle_cb(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,"Admin only",show_alert=True)
    mid=c.data.split(":",1)[1]; new=toggle_fund_method(mid,c.from_user.id)
    bot.answer_callback_query(c.id,"Updated"); fund_admin_menu(c.message)

@bot.callback_query_handler(func=lambda c: c.data.startswith("fund_admin_delete:"))
@safe_handler
def fund_admin_delete_cb(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,"Admin only",show_alert=True)
    mid=c.data.split(":",1)[1]; delete_fund_method(mid,c.from_user.id)
    bot.answer_callback_query(c.id,"Deleted"); fund_admin_menu(c.message)

@bot.callback_query_handler(func=lambda c: c.data == "fund_admin_back")
@safe_handler
def fund_admin_back_cb(c):
    fund_admin_menu(c.message)

# ================================================================
# GROUPS / SUPPORT
# ================================================================

@bot.message_handler(func=lambda m: m.text == btn_label("support"))
@safe_handler
def support(m):
    if feature_blocked_message(m, "support"):
        return
    clear_state(m.chat.id)
    update_state(m.chat.id, flow="support")
    _, _si_default = TEXT_TEMPLATES["support_intro"]
    bot.send_message(
        m.chat.id,
        render_text("support_intro", _si_default),
        reply_markup=back_kb(),
    )


def _handle_support_text_complaint(m, state):
    _finish_support_ticket(m, complaint_text=m.text.strip())


def _finish_support_ticket(m, complaint_text=None, media_type=None, file_id=None):
    ticket_id = gen_id("SUP")
    user = get_user(m.chat.id)
    name = user["name"] if user else (m.from_user.first_name or "Unknown")
    username = f"@{m.from_user.username}" if m.from_user.username else "Not set"
    lg = lagos_parts()

    forward_text = (
        "🚨 NEW CUSTOMER SUPPORT REQUEST\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "👤 CUSTOMER INFORMATION\n\n"
        f"Name: {name}\n\n"
        f"🔗 Username: {username}\n\n"
        f"🆔 User ID: {m.chat.id}\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📩 CUSTOMER COMPLAINT\n\n"
        f"{complaint_text if complaint_text else '(see attached media)'}\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🎫 Support Reference: {ticket_id}\n\n"
        "📊 Status: OPEN\n\n"
        f"📅 Date: {lg['date']}\n\n"
        f"📅 Day: {lg['day']}\n\n"
        f"⏰ Time: {lg['time']}\n\n"
        f"🇳🇬 Timezone: {lg['tz']}\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🏢 {BRAND} Support"
    )

    delivered = False
    try:
        support_channel_id = _community_id("support_channel") or SUPPORT_GROUP_ID
        if support_channel_id:
            if media_type == "photo":
                bot.send_photo(support_channel_id, file_id, caption=forward_text)
            elif media_type == "video":
                bot.send_video(support_channel_id, file_id, caption=forward_text)
            elif media_type == "voice":
                bot.send_voice(support_channel_id, file_id, caption=forward_text)
            elif media_type == "document":
                bot.send_document(support_channel_id, file_id, caption=forward_text)
            else:
                bot.send_message(support_channel_id, forward_text)
            delivered = True
        else:
            # No support group configured yet — fall back to admins so
            # nothing is lost, rather than failing silently.
            if media_type == "photo":
                for admin_id in ADMIN_IDS:
                    bot.send_photo(admin_id, file_id, caption=forward_text)
            elif media_type == "video":
                for admin_id in ADMIN_IDS:
                    bot.send_video(admin_id, file_id, caption=forward_text)
            elif media_type == "voice":
                for admin_id in ADMIN_IDS:
                    bot.send_voice(admin_id, file_id, caption=forward_text)
            elif media_type == "document":
                for admin_id in ADMIN_IDS:
                    bot.send_document(admin_id, file_id, caption=forward_text)
            else:
                notify_admins(forward_text)
            delivered = True
    except Exception:
        logger.exception("Failed to deliver support ticket %s", ticket_id)
        delivered = False

    if not delivered:
        bot.send_message(
            m.chat.id,
            "⚠️ SUPPORT REQUEST NOT SENT\n\n"
            "We are sorry, but your complaint could not be delivered to our Support "
            "Team at the moment.\n\nPlease try again shortly.",
            reply_markup=main_menu(m.chat.id),
        )
        try:
            notify_admins(f"🐛 Support ticket delivery failed for user {m.chat.id}. Check SUPPORT_GROUP_ID / bot membership.\n\n{forward_text}")
        except Exception:
            logger.exception("Failed to also alert admins of support delivery failure")
        return

    create_support_ticket(ticket_id, m.chat.id, complaint=complaint_text, media_type=media_type, media_file_id=file_id)
    clear_state(m.chat.id)

    bot.send_message(
        m.chat.id,
        "✅ YOUR COMPLAINT HAS BEEN RECEIVED\n\n"
        f"Thank you for contacting {BRAND} Support. 💙\n\n"
        "📩 We have successfully received your complaint.\n\n"
        f"🎫 Support Reference:\n\n{ticket_id}\n\n"
        "Our Support Team will review your complaint and contact you if further "
        "information is needed.\n\n"
        "⏳ Please be patient while our team investigates the issue.\n\n"
        "🙏 Kindly allow us some time to review and resolve your case.\n\n"
        "We will get back to you or complete the necessary action as soon as possible.\n\n"
        "💙 Thank you for your patience and understanding.",
        reply_markup=main_menu(m.chat.id),
    )


# ================================================================
# CUSTOM HANDLE DISPATCH — registered after every fixed-menu-text
# handler above (same rule as the generic router right below: first
# matching handler wins registration order), so a built-in button
# always wins if an admin ever reuses its exact label by mistake.
# ================================================================

# Maps a feature_key (chosen when the admin creates a feature_link
# custom handle) to the actual function that already implements it —
# so a feature_link button behaves 100% identically to tapping the
# real menu button, just under whatever new label the admin gave it.
FEATURE_LINK_DISPATCH = {
    "submit_work": submit_work,
    "buy_sell_mail": buy_mail_menu,
    "balance": show_balance,
    "withdraw": show_withdraw_menu,
    "referrals": show_referrals,
    "history": transaction_history,
    "bank_details": bank_menu,
    "support": support,
    "profile": show_my_profile,
}


@bot.message_handler(func=lambda m: m.content_type == "text" and get_custom_handle_by_label(m.text.strip()) is not None)
@safe_handler
def custom_handle_dispatch(m):
    handle = get_custom_handle_by_label(m.text.strip())
    if handle is None:
        return
    try:
        cfg = json.loads(handle["config_json"])
    except Exception:
        cfg = {}
    if cfg.get("audience", "all") == "specific" and not is_admin(m.chat.id):
        allowed = {str(u) for u in cfg.get("user_ids", [])}
        if str(m.chat.id) not in allowed:
            return  # Not in this handle's audience — not visible/actionable to them.

    action_type = handle["action_type"]
    if action_type == "static_message":
        bot.send_message(m.chat.id, cfg.get("text", ""), reply_markup=main_menu(m.chat.id))
    elif action_type == "link_button":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(cfg.get("text") or "🔗 Open", url=cfg.get("url", "")))
        bot.send_message(m.chat.id, "👉 Tap below:", reply_markup=kb)
    elif action_type == "forward_to_admin":
        clear_state(m.chat.id)
        update_state(m.chat.id, flow="custom_handle_input", handle_id=handle["handle_id"])
        bot.send_message(m.chat.id, cfg.get("prompt", "✍️ Please send your message:"), reply_markup=back_kb())
    elif action_type == "feature_link":
        fn = FEATURE_LINK_DISPATCH.get(cfg.get("feature_key"))
        if fn:
            fn(m)
        else:
            bot.send_message(m.chat.id, "⚠️ This button's feature is no longer available. Please contact support.", reply_markup=main_menu(m.chat.id))


# ============================================================================================================================== GENERIC STATE-DRIVEN TEXT ROUTER
# ================================================================
# This MUST be registered after every fixed-menu-text handler above
# (pyTelegramBotAPI executes only the first handler whose filter
# matches, in registration order). Anything that isn't an exact menu
# button falls through to here, where we route by the persisted
# conversation state instead.

_FLOW_ROUTES = {
    ("fund_wallet", "amount"): _handle_fund_amount,
    ("fund_decline", "reason"): _handle_fund_decline_reason,
    ("fund_admin_add", "name"): _handle_fund_admin_add,
    ("fund_admin_add", "currency"): _handle_fund_admin_add,
    ("fund_admin_add", "rate"): _handle_fund_admin_add,
    ("fund_admin_add", "destination"): _handle_fund_admin_add,
    ("withdraw", None): _handle_withdraw_amount,
    ("wd_decline_reason", None): _handle_wd_decline_reason,
    ("admin_fund", "user_id"): _handle_admin_fund_user_id,
    ("admin_fund", "action"): _handle_admin_fund_action,
    ("admin_fund", "amount"): _handle_admin_fund_amount,
    ("admin_fund", "reason"): _handle_admin_fund_reason,
    ("admin_broadcast", "text"): _handle_admin_broadcast_text,
    ("admin_msg", "user_id"): _handle_admin_msg_user_id,
    ("admin_msg", "body"): _handle_admin_msg_body,
    ("sub_reject_reason", None): _handle_sub_reject_reason,
    ("bank", "write"): _handle_bank_write,
    ("bank_decline_reason", None): _handle_bank_decline_reason,
    ("support", None): _handle_support_text_complaint,
    ("admin_ban", "user_id"): _handle_admin_ban_user_id,
    ("admin_ban", "reason"): _handle_admin_ban_reason,
    ("admin_unban", "user_id"): _handle_admin_unban_user_id,
    ("admin_add_user", "user_id"): _handle_admin_add_user_id,
    ("admin_add_admin", "user_id"): _handle_admin_add_admin,
    ("admin_bank_search", None): _handle_admin_bank_search,
    ("admin_support_search", None): _handle_admin_support_search,
    ("admin_submission_search", None): _handle_admin_submission_search,
    ("admin_user_search", None): _handle_admin_user_search,
    ("admin_withdrawal_search", None): _handle_admin_withdrawal_search,
    ("admin_setting", None): _handle_admin_setting,
    ("community_set", "value"): _handle_community_set,
    ("text_edit", None): _handle_text_edit,
    ("btn_label_edit", None): _handle_btn_label_edit,
    ("menu_opt_add", None): _handle_menu_opt_add,
    ("menu_opt_rename", None): _handle_menu_opt_rename,
    ("custom_handle_add", "label"): _handle_cha_label,
    ("custom_handle_add", "static_text"): _handle_cha_static_text,
    ("custom_handle_add", "forward_prompt"): _handle_cha_forward_prompt,
    ("custom_handle_add", "link_url"): _handle_cha_link_url,
    ("custom_handle_add", "link_text"): _handle_cha_link_text,
    ("custom_handle_add", "audience_users"): _handle_cha_audience_users,
    ("custom_handle_input", None): _handle_custom_handle_input,
    ("auto_add", "title"): _handle_auto_add_title,
    ("auto_add", "body"): _handle_auto_add_body,
    ("auto_add", "time"): _handle_auto_add_time,
    ("auto_add", "interval"): _handle_auto_add_interval,
    ("auto_edit_field", None): _handle_auto_edit_field,
    ("admin_track_user", None): _handle_admin_track_user,
    ("admin_feature_restrict", "user_id"): _handle_admin_feature_restrict_user_id,
}



# ================================================================
# QUICK OTP MODULE — GRIZZLY + SHARED MOBILE DIGITAL HUB WALLET
# ================================================================
# This module deliberately uses the existing users/wallets/ledger tables.
# There is NO second wallet, deposit system, referral system, or OTP-only
# balance. All Quick OTP charges/refunds go through the main bot wallet.

OTP_SCHEMA = """
CREATE TABLE IF NOT EXISTS otp_services (
    service_code TEXT PRIMARY KEY,
    service_name TEXT NOT NULL,
    emoji TEXT NOT NULL DEFAULT '🧩',
    enabled INTEGER NOT NULL DEFAULT 1,
    global_profit_percent REAL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS otp_service_countries (
    service_code TEXT NOT NULL,
    country_code TEXT NOT NULL,
    name TEXT NOT NULL,
    flag TEXT NOT NULL DEFAULT '🌍',
    grizzly_cost REAL,
    explicit_price REAL,
    markup_percent REAL NOT NULL DEFAULT 0,
    markup_fixed REAL NOT NULL DEFAULT 0,
    available_count INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 0,
    profit_active INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(service_code,country_code)
);
CREATE INDEX IF NOT EXISTS idx_otp_sc_user ON otp_service_countries(service_code,enabled,profit_active);
CREATE TABLE IF NOT EXISTS otp_countries (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    flag TEXT NOT NULL DEFAULT '🌍',
    service_code TEXT NOT NULL DEFAULT 'wa',
    grizzly_cost REAL,
    explicit_price REAL,
    markup_percent REAL NOT NULL DEFAULT 0,
    markup_fixed REAL NOT NULL DEFAULT 0,
    available_count INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 0,
    profit_active INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS otp_price_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_code TEXT NOT NULL,
    country_code TEXT NOT NULL,
    country_name TEXT NOT NULL,
    old_cost REAL,
    new_cost REAL NOT NULL,
    direction TEXT NOT NULL,
    created_at TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_otp_price_alerts_created ON otp_price_alerts(created_at);
CREATE TABLE IF NOT EXISTS otp_orders (
    order_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    country_code TEXT NOT NULL,
    country_name TEXT NOT NULL,
    service_code TEXT NOT NULL DEFAULT 'wa',
    status TEXT NOT NULL DEFAULT 'processing',
    phone_number TEXT,
    activation_id TEXT UNIQUE,
    raw_cost REAL,
    selling_price REAL NOT NULL,
    otp_code TEXT,
    refunded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    chat_id TEXT,
    message_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_otp_orders_user_status ON otp_orders(user_id,status);
CREATE INDEX IF NOT EXISTS idx_otp_orders_status ON otp_orders(status);
"""


_GRIZZLY_COUNTRY_META = {'1': ('Ukraine', 'UA'), '2': ('Kazakhstan', 'KZ'), '3': ('China', 'CN'), '4': ('Philippines', 'PH'), '6': ('Indonesia', 'ID'), '7': ('Malaysia', 'MY'), '8': ('Kenya', 'KE'), '9': ('Tanzania', 'TZ'), '10': ('Vietnam', 'VN'), '11': ('Kyrgyzstan', 'KG'), '12': ('USA (virtual)', 'US'), '13': ('Israel', 'IL'), '14': ('Hong Kong', 'HK'), '15': ('Poland', 'PL'), '16': ('United Kingdom', 'GB'), '17': ('Madagascar', 'MG'), '18': ('DR Congo', 'CD'), '19': ('Nigeria', 'NG'), '20': ('Macao', 'MO'), '21': ('Egypt', 'EG'), '22': ('India', 'IN'), '23': ('Ireland', 'IE'), '24': ('Cambodia', 'KH'), '25': ('Laos', 'LA'), '26': ('Haiti', 'HT'), '27': ('Ivory Coast', 'CI'), '28': ('Gambia', 'GM'), '29': ('Serbia', 'RS'), '30': ('Yemen', 'YE'), '31': ('South Africa', 'ZA'), '32': ('Romania', 'RO'), '33': ('Colombia', 'CO'), '34': ('Estonia', 'EE'), '35': ('Azerbaijan', 'AZ'), '36': ('Canada', 'CA'), '37': ('Morocco', 'MA'), '38': ('Ghana', 'GH'), '39': ('Argentina', 'AR'), '40': ('Uzbekistan', 'UZ'), '41': ('Cameroon', 'CM'), '42': ('Chad', 'TD'), '43': ('Germany', 'DE'), '44': ('Lithuania', 'LT'), '45': ('Croatia', 'HR'), '46': ('Sweden', 'SE'), '48': ('Netherlands', 'NL'), '49': ('Latvia', 'LV'), '50': ('Austria', 'AT'), '52': ('Thailand', 'TH'), '53': ('Saudi Arabia', 'SA'), '55': ('Taiwan', 'TW'), '56': ('Spain', 'ES'), '58': ('Algeria', 'DZ'), '59': ('Slovenia', 'SI'), '60': ('Bangladesh', 'BD'), '61': ('Senegal', 'SN'), '62': ('Turkey', 'TR'), '63': ('Czech Republic', 'CZ'), '64': ('Sri Lanka', 'LK'), '65': ('Peru', 'PE'), '66': ('Pakistan', 'PK'), '67': ('New Zealand', 'NZ'), '68': ('Guinea', 'GN'), '69': ('Mali', 'ML'), '71': ('Ethiopia', 'ET'), '73': ('Brazil', 'BR'), '74': ('Afghanistan', 'AF'), '75': ('Uganda', 'UG'), '76': ('Angola', 'AO'), '77': ('Cyprus', 'CY'), '78': ('France', 'FR'), '79': ('Papua New Guinea', 'PG'), '80': ('Mozambique', 'MZ'), '81': ('Nepal', 'NP'), '82': ('Belgium', 'BE'), '83': ('Bulgaria', 'BG'), '84': ('Hungary', 'HU'), '86': ('Italy', 'IT'), '87': ('Paraguay', 'PY'), '88': ('Honduras', 'HN'), '89': ('Tunisia', 'TN'), '90': ('Nicaragua', 'NI'), '91': ('Timor-Leste', 'TL'), '92': ('Bolivia', 'BO'), '93': ('Costa Rica', 'CR'), '94': ('Guatemala', 'GT'), '95': ('United Arab Emirates', 'AE'), '96': ('Zimbabwe', 'ZW'), '97': ('Puerto Rico', 'PR'), '99': ('Togo', 'TG'), '100': ('Kuwait', 'KW'), '101': ('El Salvador', 'SV'), '102': ('Tonga', 'TO'), '103': ('Jamaica', 'JM'), '104': ('Trinidad and Tobago', 'TT'), '105': ('Ecuador', 'EC'), '106': ('Eswatini', 'SZ'), '107': ('Oman', 'OM'), '108': ('Bosnia and Herzegovina', 'BA'), '109': ('Dominican Republic', 'DO'), '111': ('Qatar', 'QA'), '112': ('Panama', 'PA'), '114': ('Mauritania', 'MR'), '115': ('Sierra Leone', 'SL'), '116': ('Jordan', 'JO'), '117': ('Portugal', 'PT'), '118': ('Barbados', 'BB'), '119': ('Burundi', 'BI'), '120': ('Benin', 'BJ'), '121': ('Brunei Darussalam', 'BN'), '122': ('Bahamas', 'BS'), '123': ('Botswana', 'BW'), '124': ('Belize', 'BZ'), '125': ('Central African Republic', 'CF'), '128': ('Georgia', 'GE'), '129': ('Greece', 'GR'), '130': ('Guinea-Bissau', 'GW'), '131': ('Guyana', 'GY'), '132': ('Iceland', 'IS'), '133': ('Comoros', 'KM'), '134': ('Saint Kitts and Nevis', 'KN'), '135': ('Liberia', 'LR'), '136': ('Lesotho', 'LS'), '137': ('Malawi', 'MW'), '138': ('Namibia', 'NA'), '139': ('Niger', 'NE'), '140': ('Rwanda', 'RW'), '141': ('Slovakia', 'SK'), '142': ('Suriname', 'SR'), '143': ('Tajikistan', 'TJ'), '145': ('Bahrain', 'BH'), '146': ('Reunion', 'RE'), '147': ('Zambia', 'ZM'), '148': ('Armenia', 'AM'), '149': ('Somalia', 'SO'), '150': ('Republic of the Congo', 'CG'), '151': ('Chile', 'CL'), '152': ('Burkina Faso', 'BF'), '154': ('Gabon', 'GA'), '155': ('Albania', 'AL'), '156': ('Uruguay', 'UY'), '157': ('Mauritius', 'MU'), '158': ('Bhutan', 'BT'), '159': ('Maldives', 'MV'), '161': ('Turkmenistan', 'TM'), '162': ('French Guiana', 'GF'), '163': ('Finland', 'FI'), '164': ('Saint Lucia', 'LC'), '165': ('Luxembourg', 'LU'), '166': ('Saint Vincent', 'VC'), '167': ('Equatorial Guinea', 'GQ'), '168': ('Djibouti', 'DJ'), '169': ('Antigua and Barbuda', 'AG'), '170': ('Cayman Islands', 'KY'), '171': ('Montenegro', 'ME'), '172': ('Denmark', 'DK'), '173': ('Switzerland', 'CH'), '174': ('Norway', 'NO'), '175': ('Australia', 'AU'), '176': ('Eritrea', 'ER'), '177': ('South Sudan', 'SS'), '178': ('Sao Tome and Principe', 'ST'), '179': ('Aruba', 'AW'), '180': ('Montserrat', 'MS'), '181': ('Anguilla', 'AI'), '182': ('Japan', 'JP'), '183': ('North Macedonia', 'MK'), '184': ('Seychelles', 'SC'), '185': ('New Caledonia', 'NC'), '186': ('Cape Verde', 'CV'), '187': ('USA', 'US'), '188': ('Palestine', 'PS'), '189': ('Fiji', 'FJ'), '199': ('Malta', 'MT'), '201': ('Gibraltar', 'GI'), '203': ('Kosovo', 'XK'), '204': ('Niue', 'NU'), '1003': ('Bermuda', 'BM'), '1007': ('Vanuatu', 'VU'), '1008': ('Greenland', 'GL'), '1011': ('Martinique', 'MQ'), '1012': ('French Polynesia', 'PF'), '10161': ('American Samoa', 'AS'), '10348': ('Liechtenstein', 'LI'), '10349': ('Sint Maarten', 'SX'), '10350': ('South Korea', 'KR'), '10351': ('Singapore', 'SG')}

_OTP_SERVICE_EMOJIS = {
    'whatsapp':'🟢','facebook':'🔵','telegram':'✈️','instagram':'📸','google':'🔎','gmail':'✉️','youtube':'▶️',
    'tiktok':'🎵','twitter':'🐦','x':'❎','discord':'🎮','signal':'🔐','snapchat':'👻','viber':'📞','line':'💚',
    'messenger':'💬','paypal':'💳','apple':'🍎','amazon':'📦','netflix':'🎬','uber':'🚕','airbnb':'🏠','tinder':'🔥',
    'linkedin':'💼','reddit':'👽','github':'🐙','steam':'🎮','microsoft':'🪟','outlook':'📧','yahoo':'💜','binance':'🟡',
    'coinbase':'🪙','chatgpt':'🤖','openai':'🤖','claude':'🧠','foodpanda':'🍔','doordash':'🍟','spotify':'🎧',
}

def _otp_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def _otp_flag_from_iso(code):
    code=str(code or '').upper().strip()
    if len(code) != 2 or not code.isalpha(): return '🌍'
    return ''.join(chr(127397 + ord(ch)) for ch in code)

def _otp_price(row, service_row=None):
    cost=float(row['grizzly_cost'] or 0)
    # A manual country price is a hard override and NEVER follows the global percentage.
    if row['explicit_price'] is not None:
        return round(float(row['explicit_price']),2)
    pct=None
    if service_row is not None:
        try: pct=service_row['global_profit_percent']
        except Exception: pct=None
    if pct is None:
        try: pct=row['markup_percent']
        except Exception: pct=0
    return round(cost + cost*float(pct or 0)/100 + float(row['markup_fixed'] or 0),2)

def otp_db_init():
    with db_tx() as conn:
        conn.executescript(OTP_SCHEMA)
        # SQLite migrations for existing installations.
        try: conn.execute('ALTER TABLE otp_services ADD COLUMN global_profit_percent REAL')
        except Exception: pass
        # Backward-compatible migration: keep old WA settings usable in the new composite table.
        old=conn.execute('SELECT * FROM otp_countries').fetchall()
        for r in old:
            conn.execute('INSERT OR IGNORE INTO otp_services(service_code,service_name,emoji,enabled,updated_at) VALUES(?,?,?,?,?)',
                         (r['service_code'] or 'wa','WhatsApp','🟢',1,_otp_now()))
            conn.execute("""INSERT OR IGNORE INTO otp_service_countries
                (service_code,country_code,name,flag,grizzly_cost,explicit_price,markup_percent,markup_fixed,available_count,enabled,profit_active,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r['service_code'] or 'wa',r['code'],r['name'],r['flag'],r['grizzly_cost'],r['explicit_price'],r['markup_percent'],r['markup_fixed'],r['available_count'],r['enabled'],r['profit_active'],r['updated_at']))

def _otp_http(action, **params):
    if not GRIZZLY_API_KEY:
        raise RuntimeError('GRIZZLY_API_KEY is not configured')
    q={'api_key':GRIZZLY_API_KEY,'action':action,**params}
    url=GRIZZLY_BASE_URL+'?'+urllib.parse.urlencode(q)
    req=urllib.request.Request(url,headers={'User-Agent':os.environ.get('GRIZZLY_USER_AGENT','MobileDigitalHub-QuickOTP/3.0').strip() or 'MobileDigitalHub-QuickOTP/3.0'})
    try:
        with urllib.request.urlopen(req,timeout=30) as r:
            body=r.read().decode('utf-8','replace').strip()
    except Exception as exc:
        raise ConnectionError(str(exc)) from exc
    try: data=json.loads(body)
    except Exception: data=None
    if isinstance(data,dict):
        activation_id=data.get('activationId') or data.get('activation_id') or data.get('id')
        phone=data.get('phoneNumber') or data.get('phone_number')
        cost=data.get('activationCost') or data.get('activation_cost') or data.get('cost')
        sms=data.get('sms') if isinstance(data.get('sms'),dict) else {}
        otp=sms.get('code') or data.get('code') or data.get('otp')
        if activation_id and phone: return {'status':'ok','raw':body,'activation_id':str(activation_id),'phone':str(phone),'cost':cost}
        if otp: return {'status':'ok','raw':body,'otp':str(otp)}
        return {'status':str(data.get('status') or 'ok').lower(),'raw':body,'data':data}
    if body.startswith('ACCESS_NUMBER:') or body.startswith('ACCESS_NUMBER_V2:'):
        p=body.split(':',2); return {'status':'ok','raw':body,'activation_id':p[1],'phone':p[2]} if len(p)==3 else {'status':'error','raw':body}
    if body.startswith('STATUS_OK:'): return {'status':'ok','raw':body,'otp':body.split(':',1)[1]}
    if body in {'STATUS_WAIT_CODE','STATUS_WAIT_RETRY','STATUS_CANCEL','NO_ACTIVATION','ACCESS_CANCEL','ACCESS_ACTIVATION'}: return {'status':body,'raw':body}
    return {'status':'error','raw':body}

def _otp_flag_from_iso(iso):
    iso=(iso or '').upper()
    if len(iso)!=2 or not iso.isalpha(): return '🌍'
    return ''.join(chr(127397+ord(c)) for c in iso)

def _otp_parse_rows(payload, service='wa'):
    data=payload.get('data',payload) if isinstance(payload,dict) else payload
    out=[]
    if isinstance(data,dict):
        # API may return service -> countries OR countries -> service/cost nodes.
        for code,node in data.items():
            if not isinstance(node,dict): continue
            svc=node.get(service) if isinstance(node.get(service),dict) else node
            if not isinstance(svc,dict): continue
            country_code=str(code)
            if not country_code.isdigit():
                # Some responses wrap countries inside a named object.
                continue
            cost=svc.get('cost',svc.get('price',svc.get('activationCost')))
            count=svc.get('count',svc.get('available',svc.get('stock',svc.get('qty',svc.get('total',0)))))
            meta=_GRIZZLY_COUNTRY_META.get(country_code,('Country '+country_code,''))
            name=svc.get('name') or node.get('name') or meta[0]
            iso=svc.get('iso') or node.get('iso') or meta[1]
            flag=_otp_flag_from_iso(iso) if iso else (svc.get('flag') or node.get('flag') or '🌍')
            try: count=int(count or 0)
            except: count=0
            try: cost=float(cost) if cost is not None else None
            except: cost=None
            if cost is not None: out.append({'code':country_code,'name':str(name),'flag':flag,'cost':cost,'count':count})
    return out

def _otp_parse_services(payload):
    data=payload.get('services',payload.get('data',payload)) if isinstance(payload,dict) else payload
    out=[]
    if isinstance(data,dict):
        for code,node in data.items():
            if isinstance(node,dict): name=node.get('name') or node.get('title') or node.get('service') or str(code)
            else: name=str(node or code)
            out.append((str(code),str(name)))
    elif isinstance(data,list):
        for node in data:
            if isinstance(node,dict):
                code=node.get('code') or node.get('id') or node.get('service')
                name=node.get('name') or node.get('title') or node.get('service_name') or code
                if code: out.append((str(code),str(name)))
    # Some providers expose a list of {service_code: ..., service_name: ...} under data.
    unique={c:n for c,n in out if c}
    return list(unique.items())

def _otp_service_emoji(name):
    low=str(name).lower()
    for key,emoji in _OTP_SERVICE_EMOJIS.items():
        if key in low: return emoji
    return '🧩'

def otp_sync_services():
    last=None
    for action in ('getServices','get_services'):
        try:
            r=_otp_http(action)
            if r.get('status')=='error': last=r; continue
            rows=_otp_parse_services(r.get('data',r))
            if not rows: last=r; continue
            with db_tx() as conn:
                for code,name in rows:
                    conn.execute("""INSERT INTO otp_services(service_code,service_name,emoji,enabled,updated_at)
                                    VALUES(?,?,?,?,?) ON CONFLICT(service_code) DO UPDATE SET service_name=excluded.service_name,emoji=excluded.emoji,updated_at=excluded.updated_at""",
                                 (code,name,_otp_service_emoji(name),1,_otp_now()))
            return len(rows)
        except Exception as exc: last=exc
    raise RuntimeError(f'Grizzly service sync failed: {last}')

def _otp_notify_price_alerts(service_code, alerts):
    if not alerts: return
    svc=fetchone('SELECT service_name FROM otp_services WHERE service_code=?',(service_code,))
    sname=svc['service_name'] if svc else service_code
    lines=['🔔 <b>QUICK OTP • GRIZZLY PRICE CHANGE</b>', '', f'🧩 Service: <b>{html.escape(str(sname))}</b>']
    for code,name,old_cost,new_cost,direction,flag in alerts[:25]:
        arrow='📈' if direction=='increased' else '📉'
        lines.append(f'{arrow} {flag} <b>{html.escape(str(name))}</b>: {old_cost:.4f} → {new_cost:.4f} USDT')
    if len(alerts)>25: lines.append(f'… and {len(alerts)-25} more changes.')
    text='\n'.join(lines)
    for admin_id in ADMIN_IDS:
        try: bot.send_message(int(admin_id),text,parse_mode='HTML')
        except Exception as exc: logger.warning('OTP price alert send failed to %s: %s',admin_id,exc)
    try:
        ids=','.join(str(x[0]) for x in alerts)
        with db_tx() as conn:
            conn.execute('UPDATE otp_price_alerts SET notified=1 WHERE service_code=? AND notified=0 AND id IN (SELECT id FROM otp_price_alerts WHERE service_code=? ORDER BY id DESC LIMIT ?)',(service_code,service_code,len(alerts)))
    except Exception: pass

def otp_sync_service_stock(service_code):
    last=None
    for action in ('getPricesV3','getPricesV2','getPrices'):
        try:
            r=_otp_http(action,service=service_code)
            if r.get('status')=='error': last=r; continue
            rows=_otp_parse_rows(r,service=service_code)
            if not rows: last=r; continue
            alerts=[]
            with db_tx() as conn:
                svc=conn.execute('SELECT service_name FROM otp_services WHERE service_code=?',(service_code,)).fetchone()
                if not svc: conn.execute('INSERT OR IGNORE INTO otp_services(service_code,service_name,emoji,enabled,updated_at) VALUES(?,?,?,?,?)',(service_code,service_code,_otp_service_emoji(service_code),1,_otp_now()))
                for x in rows:
                    old=conn.execute('SELECT name,grizzly_cost FROM otp_service_countries WHERE service_code=? AND country_code=?',(service_code,x['code'])).fetchone()
                    old_cost=float(old['grizzly_cost']) if old and old['grizzly_cost'] is not None else None
                    new_cost=float(x['cost'] or 0)
                    if old_cost is not None and abs(old_cost-new_cost) > 1e-9:
                        direction='increased' if new_cost > old_cost else 'decreased'
                        conn.execute('INSERT INTO otp_price_alerts(service_code,country_code,country_name,old_cost,new_cost,direction,created_at,notified) VALUES(?,?,?,?,?,?,?,0)',(service_code,x['code'],x['name'],old_cost,new_cost,direction,_otp_now()))
                        alerts.append((x['code'],x['name'],old_cost,new_cost,direction,x['flag']))
                    conn.execute("""INSERT INTO otp_service_countries(service_code,country_code,name,flag,grizzly_cost,explicit_price,markup_percent,markup_fixed,available_count,enabled,profit_active,updated_at)
                                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                                    ON CONFLICT(service_code,country_code) DO UPDATE SET name=excluded.name,flag=excluded.flag,grizzly_cost=excluded.grizzly_cost,available_count=excluded.available_count,updated_at=excluded.updated_at""",
                                 (service_code,x['code'],x['name'],x['flag'] or _otp_flag_from_iso(x.get('iso2')),x['cost'],None,0,0,x['count'],0,0,_otp_now()))
            if alerts: _otp_notify_price_alerts(service_code, alerts)
            return len(rows)
        except Exception as exc: last=exc
    raise RuntimeError(f'Grizzly stock sync failed for {service_code}: {last}')

def otp_sync_stock():
    # Backward-compatible alias: sync services and WhatsApp stock only.
    try: otp_sync_services()
    except Exception as exc: logger.warning('Legacy service sync failed: %s',exc)
    return otp_sync_service_stock('wa')

def _otp_profit_active(row, service_row=None):
    return bool(row['profit_active']) and bool(row['enabled']) and _otp_price(row, service_row) > float(row['grizzly_cost'] or 0)

def _otp_order_id(): return 'QOTP-'+secrets.token_hex(4).upper()
def _otp_balance(user_id):
    row=fetchone('SELECT usdt FROM wallets WHERE user_id=?',(str(user_id),)); return float(row['usdt']) if row else 0.0

def _otp_update_message(chat_id,message_id,text,kb):
    try: bot.edit_message_text(text,chat_id=chat_id,message_id=message_id,parse_mode='HTML',reply_markup=kb)
    except Exception as exc:
        if 'message is not modified' not in str(exc).lower(): logger.warning('OTP UI update failed: %s',exc)

def _otp_waiting_text(o,remaining,manual_remaining):
    a=max(0,int(remaining)); m=max(0,int(manual_remaining));
    return (f'📱 <b>QUICK OTP • MOBILE BUSINESS HUB</b>\n\n'
            f'{_otp_service_emoji(o.get("service_name") or o.get("service_code"))} Service: <b>{html.escape(str(o.get("service_name") or o.get("service_code")))}</b>\n'
            f'🌍 Country: <b>{html.escape(str(o["country_name"]))}</b>\n'
            f'📞 Number: <code>{html.escape(str(o["phone_number"]))}</code>\n'
            f'💰 Price: <b>{float(o["selling_price"]):.2f} USDT</b>\n'
            f'🧾 Order ID: <code>{o["order_id"]}</code>\n\n'
            f'🔐 <b>Waiting for your OTP…</b>\n'
            f'⏱ Auto cancel: <b>{a//60:02d}:{a%60:02d}</b>\n'
            f'✋ Cancel available in: <b>{m//60:02d}:{m%60:02d}</b>\n\n'
            f'💳 Balance: <b>{_otp_balance(o["user_id"]):.2f} USDT</b>\n━━━━━━━━━━━━━━\n'
            f'💡 Keep this screen open. Your OTP will appear here automatically.')

def _otp_kb(order_id,service_code,country_code,manual_remaining):
    label=f'✋ Cancel available {manual_remaining//60:02d}:{manual_remaining%60:02d}' if manual_remaining>0 else '❌ Cancel'
    return types.InlineKeyboardMarkup().add(types.InlineKeyboardButton('🆕 Get New Number',callback_data=f'otp_new:{service_code}:{country_code}')).add(types.InlineKeyboardButton(label,callback_data=f'otp_cancel:{order_id}'))

def _otp_create_activation(user_id,service_code,country_code,source_chat_id):
    try: otp_sync_service_stock(service_code)
    except Exception as exc: logger.warning('OTP service stock refresh failed: %s',exc)
    row=fetchone('SELECT * FROM otp_service_countries WHERE service_code=? AND country_code=? AND enabled=1 AND profit_active=1',(service_code,str(country_code)))
    if not row: return None,'This service/country is no longer available.'
    if int(row['available_count'] or 0)<=0: return None,'❌ No number is currently available for this service and country.'
    svc_cfg=fetchone('SELECT * FROM otp_services WHERE service_code=?',(service_code,))
    price=_otp_price(row,svc_cfg)
    if price<=0: return None,'❌ Invalid price configured for this service/country.'
    svc=fetchone('SELECT service_name,emoji FROM otp_services WHERE service_code=?',(service_code,))
    service_name=svc['service_name'] if svc else service_code
    now=_otp_now(); order_id=_otp_order_id()
    with db_tx() as conn:
        ensure_wallet(conn,user_id)
        w=conn.execute('SELECT usdt FROM wallets WHERE user_id=?',(str(user_id),)).fetchone()
        if not w or float(w['usdt'])+1e-9<price: return None,f'💰 Insufficient balance.\nRequired: {price:.2f} USDT\nBalance: {float(w["usdt"]):.2f} USDT'
        adjust_balance(conn,user_id,'usdt',-price,'OTP_PURCHASE',reason=f'Quick OTP {service_name} purchase {order_id}',related_txn=order_id,processed_by=user_id)
        conn.execute('INSERT INTO otp_orders(order_id,user_id,country_code,country_name,service_code,status,selling_price,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',(order_id,str(user_id),row['country_code'],row['name'],service_code,'processing',price,now,now))
    try: result=_otp_http('getNumberV2',service=service_code,country=str(row['country_code']),maxPrice=str(row['grizzly_cost']))
    except Exception:
        with db_tx() as conn: conn.execute('UPDATE otp_orders SET status="manual_reconciliation",updated_at=? WHERE order_id=? AND status="processing"',(_otp_now(),order_id))
        return None,f'⚠️ Grizzly did not confirm the request. Order <code>{order_id}</code> is under safe reconciliation; balance was not auto-refunded.'
    if result.get('status')!='ok' or not result.get('activation_id'):
        with db_tx() as conn:
            conn.execute('UPDATE otp_orders SET status="refunded",refunded=1,updated_at=? WHERE order_id=?',(_otp_now(),order_id))
            adjust_balance(conn,user_id,'usdt',price,'OTP_REFUND',reason=f'Quick OTP failed {order_id}',related_txn=order_id,processed_by=user_id)
        return None,'❌ No number is currently available. Your funds were refunded.'
    activation_id=result['activation_id']; phone=result['phone']; raw_cost=float(result.get('cost') or row['grizzly_cost'] or 0)
    with db_tx() as conn:
        conn.execute('UPDATE otp_orders SET status="waiting",phone_number=?,activation_id=?,raw_cost=?,updated_at=? WHERE order_id=?',(phone,activation_id,raw_cost,_otp_now(),order_id))
        conn.execute("INSERT INTO audit_log(admin_id,action,target_user,amount,txn_id,reason,created_at) VALUES(?,?,?,?,?,?,?)",(str(user_id),"OTP_PURCHASE",str(user_id),price,order_id,f'service={service_name}; country={row["name"]}; provider_cost={raw_cost:.6f}',_otp_now()))
    return fetchone('SELECT o.*,s.service_name FROM otp_orders o LEFT JOIN otp_services s ON s.service_code=o.service_code WHERE o.order_id=?',(order_id,)),None

def _otp_show_services(chat_id, page=0, edit=None):
    per=20; page=max(0,int(page))
    services=fetchall('SELECT * FROM otp_services WHERE enabled=1 ORDER BY service_name')
    eligible=[]
    for svc in services:
        countries=fetchall('SELECT * FROM otp_service_countries WHERE service_code=? AND enabled=1 AND profit_active=1 AND available_count>0',(svc['service_code'],))
        if any(_otp_profit_active(r,svc) for r in countries): eligible.append(svc)
    total=len(eligible)
    rows=eligible[page*per:(page+1)*per]
    if not rows and page>0:
        page=max(0,page-1); rows=eligible[page*per:(page+1)*per]
    if not rows:
        text='📱 <b>QUICK OTP</b>\n\n⏳ No service is active yet. Please check back shortly.'
        if edit: bot.edit_message_text(text,chat_id,edit,parse_mode='HTML')
        else: bot.send_message(chat_id,text,parse_mode='HTML')
        return
    kb=types.InlineKeyboardMarkup()
    for r in rows: kb.add(types.InlineKeyboardButton(f'{r["emoji"]} {r["service_name"]}',callback_data=f'otp_service:{r["service_code"]}'))
    nav=[]
    if page>0: nav.append(types.InlineKeyboardButton('⬅️ Previous',callback_data=f'otp_services_page:{page-1}'))
    if (page+1)*per<total: nav.append(types.InlineKeyboardButton('Next ➡️',callback_data=f'otp_services_page:{page+1}'))
    if nav: kb.row(*nav)
    text=f'📱 <b>QUICK OTP</b>\n\n🎯 Choose the service you want to verify.\n🔐 Secure • ⚡ Fast • 💳 Pay from your USDT balance\n\n👇 <b>Select a service:</b>\n📄 Page {page+1}/{max(1,(total+per-1)//per)}'
    if edit: bot.edit_message_text(text,chat_id,edit,parse_mode='HTML',reply_markup=kb)
    else: bot.send_message(chat_id,text,parse_mode='HTML',reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_services_page:'))
@safe_handler
def otp_services_page_cb(c):
    bot.answer_callback_query(c.id); _otp_show_services(c.message.chat.id,int(c.data.split(':',1)[1]),edit=c.message.message_id)

@bot.message_handler(func=lambda m: m.text == '📱 Quick OTP')
@safe_handler
def otp_entry(m):
    if feature_blocked_message(m,'quick_otp'): return
    try: otp_sync_services()
    except Exception as exc: logger.warning('Quick OTP service sync failed: %s',exc)
    _otp_show_services(m.chat.id, 0, edit=None)

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_service:'))
@safe_handler
def otp_service_cb(c):
    service=c.data.split(':',1)[1]; svc=fetchone('SELECT * FROM otp_services WHERE service_code=? AND enabled=1',(service,))
    if not svc: return bot.answer_callback_query(c.id,'Service unavailable.',show_alert=True)
    try: otp_sync_service_stock(service)
    except Exception as exc: logger.warning('OTP service refresh failed: %s',exc)
    _otp_show_countries(c.message.chat.id, service, 0, svc, edit=c.message.message_id); bot.answer_callback_query(c.id)

def _otp_show_countries(chat_id, service, page, svc, edit=None):
    per=20; page=max(0,int(page))
    where='service_code=? AND enabled=1 AND profit_active=1 AND available_count>0'
    service_cfg=fetchone('SELECT * FROM otp_services WHERE service_code=?',(service,))
    all_rows=fetchall(f'SELECT * FROM otp_service_countries WHERE {where} ORDER BY name', (service,))
    rows=[r for r in all_rows if _otp_profit_active(r,service_cfg)]
    total=len(rows)
    rows=rows[page*per:(page+1)*per]
    if not rows:
        if page>0: page=max(0,page-1); rows=rows[page*per:(page+1)*per]
        if not rows: return
    kb=types.InlineKeyboardMarkup()
    for r in rows: kb.add(types.InlineKeyboardButton(f'{r["flag"]} {r["name"]} • {_otp_price(r,service_cfg):.2f} USDT',callback_data=f'otp_country:{service}:{r["country_code"]}'))
    nav=[]
    if page>0: nav.append(types.InlineKeyboardButton('⬅️ Previous',callback_data=f'otp_countries_page:{service}:{page-1}'))
    if (page+1)*per<total: nav.append(types.InlineKeyboardButton('Next ➡️',callback_data=f'otp_countries_page:{service}:{page+1}'))
    if nav: kb.row(*nav)
    kb.add(types.InlineKeyboardButton('⬅️ Services',callback_data='otp_services_back'))
    text=f'{svc["emoji"]} <b>{html.escape(svc["service_name"])}</b>\n\n🌍 Choose a country:\n💰 Prices shown are the final Mobile Business Hub price.\n📄 Page {page+1}/{max(1,(total+per-1)//per)}'
    if edit: bot.edit_message_text(text,chat_id,edit,parse_mode='HTML',reply_markup=kb)
    else: bot.send_message(chat_id,text,parse_mode='HTML',reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_countries_page:'))
@safe_handler
def otp_countries_page_cb(c):
    _,service,page=c.data.split(':',2); svc=fetchone('SELECT service_name,emoji FROM otp_services WHERE service_code=? AND enabled=1',(service,))
    if not svc: return bot.answer_callback_query(c.id,'Service unavailable.',show_alert=True)
    try: otp_sync_service_stock(service)
    except Exception as exc: logger.warning('OTP country refresh failed for %s: %s',service,exc)
    _otp_show_countries(c.message.chat.id,service,int(page),svc,edit=c.message.message_id); bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data=='otp_services_back')
@safe_handler
def otp_services_back_cb(c): bot.answer_callback_query(c.id); _otp_show_services(c.message.chat.id,0,edit=c.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_country:'))
@safe_handler
def otp_country_cb(c):
    _,service,code=c.data.split(':',2); row=fetchone('SELECT * FROM otp_service_countries WHERE service_code=? AND country_code=? AND enabled=1 AND profit_active=1',(service,code))
    svc=fetchone('SELECT service_name,emoji FROM otp_services WHERE service_code=?',(service,))
    if not row: return bot.answer_callback_query(c.id,'Country unavailable.',show_alert=True)
    kb=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton('✅ Buy Number',callback_data=f'otp_buy:{service}:{code}')).add(types.InlineKeyboardButton('⬅️ Countries',callback_data=f'otp_service:{service}'))
    bot.answer_callback_query(c.id); bot.edit_message_text(f'{svc["emoji"] if svc else "📱"} <b>{html.escape(str(svc["service_name"] if svc else service))}</b>\n🌍 {row["flag"]} <b>{html.escape(row["name"])}</b>\n\n📦 Available: <b>{int(row["available_count"]):,}</b>\n💰 Price: <b>{_otp_price(row,svc):.2f} USDT</b>\n\nTap <b>Buy Number</b> to continue.',c.message.chat.id,c.message.message_id,parse_mode='HTML',reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_buy:'))
@safe_handler
def otp_buy_cb(c):
    _,service,code=c.data.split(':',2); bot.answer_callback_query(c.id,'Processing…')
    result,msg=_otp_create_activation(c.from_user.id,service,code,c.message.chat.id)
    if not result: return bot.send_message(c.message.chat.id,msg,parse_mode='HTML')
    r=fetchone('SELECT * FROM otp_service_countries WHERE service_code=? AND country_code=?',(service,code))
    text=_otp_waiting_text(dict(result),1200,300)
    sent=bot.send_message(c.message.chat.id,text,parse_mode='HTML',reply_markup=_otp_kb(result['order_id'],service,code,300))
    with db_tx() as conn: conn.execute('UPDATE otp_orders SET chat_id=?,message_id=?,updated_at=? WHERE order_id=?',(str(sent.chat.id),sent.message_id,_otp_now(),result['order_id']))

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_new:'))
@safe_handler
def otp_new_cb(c):
    _,service,code=c.data.split(':',2); bot.answer_callback_query(c.id,'Getting a new number…')
    result,msg=_otp_create_activation(c.from_user.id,service,code,c.message.chat.id)
    if not result: return bot.send_message(c.message.chat.id,msg,parse_mode='HTML')
    text=_otp_waiting_text(dict(result),1200,300)
    sent=bot.send_message(c.message.chat.id,text,parse_mode='HTML',reply_markup=_otp_kb(result['order_id'],service,code,300))
    with db_tx() as conn: conn.execute('UPDATE otp_orders SET chat_id=?,message_id=?,updated_at=? WHERE order_id=?',(str(sent.chat.id),sent.message_id,_otp_now(),result['order_id']))

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_cancel:'))
@safe_handler
def otp_cancel_cb(c):
    order_id=c.data.split(':',1)[1]; o=fetchone('SELECT * FROM otp_orders WHERE order_id=? AND user_id=?',(order_id,str(c.from_user.id)))
    if not o or o['status']!='waiting': return bot.answer_callback_query(c.id,'This order is no longer cancellable.',show_alert=True)
    elapsed=(datetime.now(timezone.utc)-datetime.fromisoformat(o['created_at'])).total_seconds(); remaining=300-elapsed
    if remaining>0: return bot.answer_callback_query(c.id,f'⏱ Cancel will be available in {int(remaining)//60:02d}:{int(remaining)%60:02d}',show_alert=True)
    try: r=_otp_http('setStatus',id=o['activation_id'],status='8')
    except Exception: return bot.answer_callback_query(c.id,'Network error. Please try again.',show_alert=True)
    if r.get('raw') not in {'ACCESS_CANCEL','STATUS_CANCEL','NO_ACTIVATION'}: return bot.answer_callback_query(c.id,'Cancel was not accepted by Grizzly yet.',show_alert=True)
    with db_tx() as conn:
        cur=conn.execute('SELECT * FROM otp_orders WHERE order_id=? AND status="waiting" AND refunded=0',(order_id,)).fetchone()
        if not cur: return bot.answer_callback_query(c.id,'Order already closed.',show_alert=True)
        adjust_balance(conn,cur['user_id'],'usdt',float(cur['selling_price']),'OTP_REFUND',reason=f'Quick OTP manual cancel {order_id}',related_txn=order_id,processed_by=cur['user_id'])
        conn.execute('UPDATE otp_orders SET status="cancelled",refunded=1,updated_at=? WHERE order_id=?',(_otp_now(),order_id))
        conn.execute("INSERT INTO audit_log(admin_id,action,target_user,amount,txn_id,reason,created_at) VALUES(?,?,?,?,?,?,?)",(str(cur['user_id']),"OTP_REFUND",str(cur['user_id']),cur['selling_price'],order_id,"manual cancel",_otp_now()))
    bot.answer_callback_query(c.id,'Cancelled and refunded.')
    try: bot.delete_message(o['chat_id'],o['message_id'])
    except Exception: pass

@bot.message_handler(func=lambda m: m.text == '📱 Quick OTP Settings' and is_super_admin(m.chat.id))
@safe_handler
def otp_admin_menu(m):
    kb=types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton('🔄 Sync All Services',callback_data='otp_admin_sync_services'))
    kb.row(types.InlineKeyboardButton('🧩 Services & Pricing',callback_data='otp_admin_services:0'))
    kb.row(types.InlineKeyboardButton('📊 Active Services',callback_data='otp_admin_active'))
    kb.row(types.InlineKeyboardButton('🔔 Price Change Alerts',callback_data='otp_admin_alerts'))
    bot.send_message(m.chat.id,'📱 <b>QUICK OTP SETTINGS</b>\n\n🧩 Services → 🌍 Countries → 💹 Profit → 🟢 Active\n\nOnly Super Admins can control these settings.',parse_mode='HTML',reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data=='otp_admin_sync_services')
@safe_handler
def otp_admin_sync_services(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,'Super admin only',show_alert=True)
    try: n=otp_sync_services(); bot.answer_callback_query(c.id,f'✅ Synced {n:,} Grizzly services.')
    except Exception as exc: bot.answer_callback_query(c.id,f'❌ Sync failed: {str(exc)[:180]}',show_alert=True)

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_admin_services:'))
@safe_handler
def otp_admin_services(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,'Super admin only',show_alert=True)
    try: otp_sync_services()
    except Exception: pass
    page=max(0,int(c.data.split(':',1)[1] or 0)); per=20; rows=fetchall('SELECT * FROM otp_services ORDER BY service_name LIMIT ? OFFSET ?',(per, page*per))
    total=fetchone('SELECT COUNT(*) AS n FROM otp_services')['n']; kb=types.InlineKeyboardMarkup()
    for r in rows: kb.add(types.InlineKeyboardButton(f'{r["emoji"]} {r["service_name"]}',callback_data=f'otp_admin_svc:{r["service_code"]}'))
    nav=[]
    if page>0: nav.append(types.InlineKeyboardButton('⬅️ Prev',callback_data=f'otp_admin_services:{page-1}'))
    if (page+1)*per<total: nav.append(types.InlineKeyboardButton('Next ➡️',callback_data=f'otp_admin_services:{page+1}'))
    if nav: kb.row(*nav)
    kb.row(types.InlineKeyboardButton('🔄 Sync',callback_data='otp_admin_sync_services'))
    bot.answer_callback_query(c.id); bot.send_message(c.from_user.id,f'🧩 <b>GRIZZLY SERVICES</b>\n\nShowing {page*per+1 if total else 0}-{min((page+1)*per,total)} of {total:,}.\n\nSelect a service to configure its countries and profit.',parse_mode='HTML',reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_admin_svc:'))
@safe_handler
def otp_admin_svc(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,'Super admin only',show_alert=True)
    service=c.data.split(':',1)[1]; svc=fetchone('SELECT * FROM otp_services WHERE service_code=?',(service,))
    if not svc: return bot.answer_callback_query(c.id,'Service not found.',show_alert=True)
    try: otp_sync_service_stock(service)
    except Exception as exc: logger.warning('Admin stock sync failed for %s: %s',service,exc)
    _otp_admin_show_countries(c.from_user.id,service,0,svc)
    bot.answer_callback_query(c.id)

def _otp_admin_show_countries(chat_id, service, page, svc):
    per=20; page=max(0,int(page)); total=int(fetchone('SELECT COUNT(*) AS n FROM otp_service_countries WHERE service_code=?',(service,))['n'])
    rows=fetchall('SELECT * FROM otp_service_countries WHERE service_code=? ORDER BY name LIMIT ? OFFSET ?',(service,per,page*per))
    if not rows and page>0: page-=1; rows=fetchall('SELECT * FROM otp_service_countries WHERE service_code=? ORDER BY name LIMIT ? OFFSET ?',(service,per,page*per))
    service_cfg=fetchone('SELECT * FROM otp_services WHERE service_code=?',(service,))
    kb=types.InlineKeyboardMarkup()
    for r in rows:
        status='🟢' if _otp_profit_active(r,service_cfg) else '⚪'; kb.add(types.InlineKeyboardButton(f'{status} {r["flag"]} {r["name"]} | {_otp_price(r,service_cfg):.2f}',callback_data=f'otp_admin_sc:{service}:{r["country_code"]}'))
    nav=[]
    if page>0: nav.append(types.InlineKeyboardButton('⬅️ Previous',callback_data=f'otp_admin_countries:{service}:{page-1}'))
    if (page+1)*per<total: nav.append(types.InlineKeyboardButton('Next ➡️',callback_data=f'otp_admin_countries:{service}:{page+1}'))
    if nav: kb.row(*nav)
    kb.row(types.InlineKeyboardButton('⬅️ Services',callback_data='otp_admin_services:0'))
    bot.send_message(chat_id,f'{svc["emoji"]} <b>{html.escape(svc["service_name"])}</b>\n\n🌍 Configure a country below.\n🟢 = visible to users\n⚪ = hidden until profit is activated.\n📄 Page {page+1}/{max(1,(total+per-1)//per)}',parse_mode='HTML',reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_admin_countries:'))
@safe_handler
def otp_admin_countries_page(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,'Super admin only',show_alert=True)
    _,service,page=c.data.split(':',2); svc=fetchone('SELECT service_name,emoji FROM otp_services WHERE service_code=?',(service,))
    if not svc: return bot.answer_callback_query(c.id,'Service not found.',show_alert=True)
    try: otp_sync_service_stock(service)
    except Exception as exc: logger.warning('Admin stock sync failed for %s: %s',service,exc)
    _otp_admin_show_countries(c.from_user.id,service,int(page),svc); bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_admin_sc:'))
@safe_handler
def otp_admin_sc(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,'Super admin only',show_alert=True)
    _,service,code=c.data.split(':',2); r=fetchone('SELECT * FROM otp_service_countries WHERE service_code=? AND country_code=?',(service,code)); svc=fetchone('SELECT service_name,emoji FROM otp_services WHERE service_code=?',(service,))
    if not r: return bot.answer_callback_query(c.id,'Country not found.',show_alert=True)
    kb=types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton('💵 Manual Price',callback_data=f'otp_set_price:{service}:{code}'))
    if r['explicit_price'] is not None:
        kb.add(types.InlineKeyboardButton('🧹 Remove Manual Price',callback_data=f'otp_clear_price:{service}:{code}'))
    kb.add(types.InlineKeyboardButton('📈 Service Global %',callback_data=f'otp_global_profit:{service}'))
    kb.add(types.InlineKeyboardButton('🔴 OFF' if r['enabled'] else '🟢 ACTIVE',callback_data=f'otp_toggle:{service}:{code}'))
    kb.add(types.InlineKeyboardButton('⬅️ Countries',callback_data=f'otp_admin_svc:{service}'))
    status='🟢 LIVE FOR USERS' if _otp_profit_active(r,svc) else '⚪ Hidden from users'
    bot.answer_callback_query(c.id); bot.send_message(c.from_user.id,f'{svc["emoji"] if svc else "🧩"} <b>{html.escape(str(svc["service_name"] if svc else service))}</b>\n🌍 {r["flag"]} <b>{html.escape(r["name"])}</b>\n\n🏷 Grizzly cost: {float(r["grizzly_cost"] or 0):.4f}\n💰 User price: <b>{_otp_price(r,svc):.2f} USDT</b>\n📈 Global profit: <b>{(str(svc["global_profit_percent"]) + "%") if svc["global_profit_percent"] is not None else "OFF"}</b>\n✍️ Manual country price: <b>{(f"{float(r["explicit_price"]):.2f} USDT") if r["explicit_price"] is not None else "OFF"}</b>\n📦 Available: <b>{int(r["available_count"]):,}</b>\n🔘 Status: {status}',parse_mode='HTML',reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_toggle:'))
@safe_handler
def otp_toggle(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,'Super admin only',show_alert=True)
    _,service,code=c.data.split(':',2)
    with db_tx() as conn: conn.execute('UPDATE otp_service_countries SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END,updated_at=? WHERE service_code=? AND country_code=?',(_otp_now(),service,code))
    bot.answer_callback_query(c.id,'Updated'); otp_admin_sc(c)

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_profit:'))
@safe_handler
def otp_profit_cb(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,'Super admin only',show_alert=True)
    _,service,code=c.data.split(':',2); r=fetchone('SELECT * FROM otp_service_countries WHERE service_code=? AND country_code=?',(service,code))
    if not r: return bot.answer_callback_query(c.id,'Not found',show_alert=True)
    kb=types.InlineKeyboardMarkup()
    for pct in (5,10,15,30,50): kb.add(types.InlineKeyboardButton(f'➕ {pct}%',callback_data=f'otp_profit_set:{service}:{code}:{pct}'))
    kb.add(types.InlineKeyboardButton('✍️ Manual percentage',callback_data=f'otp_profit_manual:{service}:{code}'))
    bot.answer_callback_query(c.id); bot.send_message(c.from_user.id,f'📈 <b>{r["name"]}</b>\n\nChoose profit above the live Grizzly cost:',parse_mode='HTML',reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_profit_set:'))
@safe_handler
def otp_profit_set_cb(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,'Super admin only',show_alert=True)
    _,service,code,pct=c.data.split(':',3); pct=float(pct)
    with db_tx() as conn: conn.execute('UPDATE otp_service_countries SET markup_percent=?,markup_fixed=0,explicit_price=NULL,profit_active=1,enabled=1,updated_at=? WHERE service_code=? AND country_code=?',(pct,_otp_now(),service,code))
    bot.answer_callback_query(c.id,f'{pct:g}% active'); otp_admin_sc(c)

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_profit_manual:'))
@safe_handler
def otp_profit_manual_cb(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,'Super admin only',show_alert=True)
    _,service,code=c.data.split(':',2); r=fetchone('SELECT name FROM otp_service_countries WHERE service_code=? AND country_code=?',(service,code))
    if not r: return bot.answer_callback_query(c.id,'Not found',show_alert=True)
    clear_state(c.from_user.id); update_state(c.from_user.id,flow='otp_profit_manual',step=None,otp_service=service,otp_country=code)
    bot.answer_callback_query(c.id); bot.send_message(c.from_user.id,f'✍️ Send profit percentage for <b>{r["name"]}</b>.\nExample: <code>22.5</code>',parse_mode='HTML',reply_markup=back_kb())

def _handle_otp_profit_manual(m,state):
    if not is_super_admin(m.chat.id): clear_state(m.chat.id); return
    service=state.get('otp_service'); code=state.get('otp_country')
    try: pct=float(m.text.strip())
    except Exception: return bot.send_message(m.chat.id,'❌ Send a valid percentage, e.g. 25 or 22.5.')
    if pct<=0 or pct>1000: return bot.send_message(m.chat.id,'❌ Percentage must be greater than 0 and at most 1000%.')
    with db_tx() as conn: conn.execute('UPDATE otp_service_countries SET markup_percent=?,markup_fixed=0,explicit_price=NULL,profit_active=1,enabled=1,updated_at=? WHERE service_code=? AND country_code=?',(pct,_otp_now(),service,code))
    clear_state(m.chat.id); bot.send_message(m.chat.id,f'✅ Profit set to {pct:g}%. This service/country is now 🟢 ACTIVE for users.',reply_markup=main_menu(m.chat.id))

_FLOW_ROUTES[('otp_profit_manual',None)] = _handle_otp_profit_manual

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_global_profit:'))
@safe_handler
def otp_global_profit_cb(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,'Super admin only',show_alert=True)
    service=c.data.split(':',1)[1]; svc=fetchone('SELECT * FROM otp_services WHERE service_code=?',(service,))
    if not svc: return bot.answer_callback_query(c.id,'Service not found.',show_alert=True)
    current='OFF' if svc['global_profit_percent'] is None else f'{float(svc["global_profit_percent"]):g}%'
    kb=types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton('➕ 5%',callback_data=f'otp_global_adjust:{service}:5'),types.InlineKeyboardButton('➕ 10%',callback_data=f'otp_global_adjust:{service}:10'))
    kb.row(types.InlineKeyboardButton('➖ 5%',callback_data=f'otp_global_adjust:{service}:-5'),types.InlineKeyboardButton('➖ 10%',callback_data=f'otp_global_adjust:{service}:-10'))
    kb.add(types.InlineKeyboardButton('✍️ Set exact %',callback_data=f'otp_global_manual:{service}'))
    kb.add(types.InlineKeyboardButton('🧹 Remove / OFF',callback_data=f'otp_global_clear:{service}'))
    bot.answer_callback_query(c.id); bot.send_message(c.from_user.id,f'📈 <b>GLOBAL PROFIT • {html.escape(svc["service_name"])}</b>\n\nCurrent: <b>{current}</b>\n\nThis applies to every country in this service except countries with a manual price. Manual-price countries are excluded and must be priced separately.',parse_mode='HTML',reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_global_adjust:'))
@safe_handler
def otp_global_adjust_cb(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,'Super admin only',show_alert=True)
    _,service,delta=c.data.split(':',2); delta=float(delta); svc=fetchone('SELECT * FROM otp_services WHERE service_code=?',(service,))
    if not svc: return bot.answer_callback_query(c.id,'Service not found.',show_alert=True)
    current=float(svc['global_profit_percent'] or 0); new=round(current+delta,4)
    if new<0: new=0
    with db_tx() as conn:
        conn.execute('UPDATE otp_services SET global_profit_percent=?,updated_at=? WHERE service_code=?',(new,_otp_now(),service))
        conn.execute('UPDATE otp_service_countries SET enabled=1,profit_active=1,updated_at=? WHERE service_code=? AND explicit_price IS NULL',(_otp_now(),service))
    bot.answer_callback_query(c.id,f'Global profit: {new:g}%'); otp_global_profit_cb(c)

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_global_clear:'))
@safe_handler
def otp_global_clear_cb(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,'Super admin only',show_alert=True)
    service=c.data.split(':',1)[1]
    with db_tx() as conn: conn.execute('UPDATE otp_services SET global_profit_percent=NULL,updated_at=? WHERE service_code=?',(_otp_now(),service))
    bot.answer_callback_query(c.id,'Global percentage removed.'); otp_global_profit_cb(c)

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_global_manual:'))
@safe_handler
def otp_global_manual_cb(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,'Super admin only',show_alert=True)
    service=c.data.split(':',1)[1]; svc=fetchone('SELECT service_name FROM otp_services WHERE service_code=?',(service,))
    if not svc: return bot.answer_callback_query(c.id,'Service not found.',show_alert=True)
    clear_state(c.from_user.id); update_state(c.from_user.id,flow='otp_global_manual',step=None,otp_service=service)
    bot.answer_callback_query(c.id); bot.send_message(c.from_user.id,f'✍️ Send exact global profit percentage for <b>{html.escape(svc["service_name"])}</b>.\nExample: <code>25</code>\nUse <code>0</code> to keep prices at Grizzly cost (countries still need to be above cost to show users).',parse_mode='HTML',reply_markup=back_kb())

def _handle_otp_global_manual(m,state):
    if not is_super_admin(m.chat.id): clear_state(m.chat.id); return
    service=state.get('otp_service')
    try: pct=float(m.text.strip())
    except Exception: return bot.send_message(m.chat.id,'❌ Send a valid percentage, e.g. 25 or 12.5.')
    if pct<0 or pct>1000: return bot.send_message(m.chat.id,'❌ Percentage must be between 0 and 1000%.')
    with db_tx() as conn:
        conn.execute('UPDATE otp_services SET global_profit_percent=?,updated_at=? WHERE service_code=?',(pct,_otp_now(),service))
        conn.execute('UPDATE otp_service_countries SET enabled=1,profit_active=1,updated_at=? WHERE service_code=? AND explicit_price IS NULL',(_otp_now(),service))
    clear_state(m.chat.id); bot.send_message(m.chat.id,f'✅ Global profit set to {pct:g}% for this service. Manual-price countries remain excluded from this rule.',reply_markup=main_menu(m.chat.id))

_FLOW_ROUTES[('otp_global_manual',None)] = _handle_otp_global_manual

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_clear_price:'))
@safe_handler
def otp_clear_price_cb(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,'Super admin only',show_alert=True)
    _,service,code=c.data.split(':',2)
    with db_tx() as conn: conn.execute('UPDATE otp_service_countries SET explicit_price=NULL,profit_active=1,updated_at=? WHERE service_code=? AND country_code=?',(_otp_now(),service,code))
    bot.answer_callback_query(c.id,'Manual price removed. Country now follows global service pricing.'); otp_admin_sc(c)

@bot.callback_query_handler(func=lambda c: c.data.startswith('otp_set_price:'))
@safe_handler
def otp_set_price_cb(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,'Super admin only',show_alert=True)
    _,service,code=c.data.split(':',2); r=fetchone('SELECT * FROM otp_service_countries WHERE service_code=? AND country_code=?',(service,code))
    if not r: return bot.answer_callback_query(c.id,'Not found',show_alert=True)
    clear_state(c.from_user.id); update_state(c.from_user.id,flow='otp_set_price',step=None,otp_service=service,otp_country=code)
    bot.answer_callback_query(c.id); bot.send_message(c.from_user.id,f'💰 Send the final selling price in USDT for <b>{r["name"]}</b>.\nCurrent: {_otp_price(r,fetchone('SELECT * FROM otp_services WHERE service_code=?',(service,))):.2f}\nExample: <code>0.85</code>',parse_mode='HTML',reply_markup=back_kb())

def _handle_otp_set_price(m,state):
    if not is_super_admin(m.chat.id): clear_state(m.chat.id); return
    service=state.get('otp_service'); code=state.get('otp_country')
    try: price=round(float(m.text.strip()),2)
    except: return bot.send_message(m.chat.id,'❌ Invalid price. Send a number like 0.85')
    if price<=0: return bot.send_message(m.chat.id,'❌ Price must be greater than 0.')
    r=fetchone('SELECT grizzly_cost FROM otp_service_countries WHERE service_code=? AND country_code=?',(service,code))
    if not r: return bot.send_message(m.chat.id,'❌ Country not found.')
    if price<=float(r['grizzly_cost'] or 0): return bot.send_message(m.chat.id,f'❌ User price must be above Grizzly cost ({float(r["grizzly_cost"] or 0):.4f} USDT).')
    with db_tx() as conn: conn.execute('UPDATE otp_service_countries SET explicit_price=?,markup_percent=0,markup_fixed=0,profit_active=1,enabled=1,updated_at=? WHERE service_code=? AND country_code=?',(price,_otp_now(),service,code))
    clear_state(m.chat.id); bot.send_message(m.chat.id,f'✅ Final user price updated to {price:.2f} USDT. 🟢 Active.',reply_markup=main_menu(m.chat.id))

_FLOW_ROUTES[('otp_set_price',None)] = _handle_otp_set_price

@bot.callback_query_handler(func=lambda c: c.data=='otp_admin_alerts')
@safe_handler
def otp_admin_alerts(c):
    if not is_super_admin(c.from_user.id): return bot.answer_callback_query(c.id,'Super admin only',show_alert=True)
    rows=fetchall('SELECT a.*,s.service_name FROM otp_price_alerts a LEFT JOIN otp_services s ON s.service_code=a.service_code ORDER BY a.id DESC LIMIT 30')
    if not rows: return bot.answer_callback_query(c.id,'No Grizzly price changes recorded yet.',show_alert=True)
    lines=['🔔 <b>RECENT GRIZZLY PRICE CHANGES</b>','']
    for r in rows:
        arrow='📈' if r['direction']=='increased' else '📉'
        lines.append(f'{arrow} {r["service_name"] or r["service_code"]} • {r["country_name"]}: {float(r["old_cost"]):.4f} → {float(r["new_cost"]):.4f} USDT')
    bot.answer_callback_query(c.id); bot.send_message(c.from_user.id,'\n'.join(lines),parse_mode='HTML')

# OTP worker: keep the existing safe reconciliation/refund behavior, but use the selected service.
def _otp_price_watcher():
    # Watch only services that have at least one configured/active country, avoiding thousands of unnecessary API calls.
    while True:
        try:
            services=fetchall('SELECT DISTINCT service_code FROM otp_service_countries WHERE enabled=1 OR explicit_price IS NOT NULL OR profit_active=1')
            for r in services:
                try: otp_sync_service_stock(r['service_code'])
                except Exception as exc: logger.warning('OTP price watcher failed for %s: %s',r['service_code'],exc)
        except Exception: logger.exception('OTP price watcher error')
        time.sleep(600)

def _otp_worker():
    last_status={}
    while True:
        try:
            active=fetchall('SELECT o.*,s.service_name FROM otp_orders o LEFT JOIN otp_services s ON s.service_code=o.service_code WHERE o.status="waiting" ORDER BY o.created_at LIMIT 300')
            now=datetime.now(timezone.utc)
            for o in active:
                try: elapsed=(now-datetime.fromisoformat(o['created_at'])).total_seconds()
                except Exception: continue
                auto=max(0,1200-int(elapsed)); manual=max(0,300-int(elapsed))
                if elapsed>=1200:
                    try: r=_otp_http('setStatus',id=o['activation_id'],status='8')
                    except Exception: continue
                    if r.get('raw') not in {'ACCESS_CANCEL','STATUS_CANCEL','NO_ACTIVATION'}: continue
                    with db_tx() as conn:
                        cur=conn.execute('SELECT * FROM otp_orders WHERE order_id=? AND status="waiting" AND refunded=0',(o['order_id'],)).fetchone()
                        if cur:
                            adjust_balance(conn,cur['user_id'],'usdt',float(cur['selling_price']),'OTP_REFUND',reason=f'Quick OTP auto cancel {o["order_id"]}',related_txn=o['order_id'],processed_by=cur['user_id'])
                            conn.execute('UPDATE otp_orders SET status="expired",refunded=1,updated_at=? WHERE order_id=?',(_otp_now(),o['order_id']))
                    try: bot.delete_message(o['chat_id'],o['message_id'])
                    except Exception: pass
                    continue
                if o['chat_id'] and o['message_id']:
                    _otp_update_message(o['chat_id'],o['message_id'],_otp_waiting_text(o,auto,manual),_otp_kb(o['order_id'],o['service_code'],o['country_code'],manual))
                if elapsed-last_status.get(o['order_id'],-999)>=5:
                    last_status[o['order_id']]=elapsed
                    try: r=_otp_http('getStatusV2',id=o['activation_id'])
                    except Exception: continue
                    if r.get('otp'):
                        otp=r['otp']
                        try: _otp_http('setStatus',id=o['activation_id'],status='6')
                        except Exception: pass
                        with db_tx() as conn:
                            cur=conn.execute('SELECT * FROM otp_orders WHERE order_id=? AND status="waiting"',(o['order_id'],)).fetchone()
                            if cur:
                                conn.execute('UPDATE otp_orders SET status="completed",otp_code=?,updated_at=? WHERE order_id=?',(otp,_otp_now(),o['order_id']))
                                conn.execute("INSERT INTO audit_log(admin_id,action,target_user,amount,txn_id,reason,created_at) VALUES(?,?,?,?,?,?,?)",(str(cur['user_id']),"OTP_COMPLETED",str(cur['user_id']),cur['selling_price'],o['order_id'],f'service={cur["service_code"]}; country={cur["country_name"]}',_otp_now()))
                        try: bot.delete_message(o['chat_id'],o['message_id'])
                        except Exception: pass
                        kb=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton('🆕 Get New Number',callback_data=f'otp_new:{o["service_code"]}:{o["country_code"]}'))
                        bot.send_message(o['user_id'],f'🔐 <b>OTP RECEIVED</b>\n\n{html.escape(str(o["service_name"] or o["service_code"]))} • {html.escape(str(o["country_name"]))}\n🧾 Order: <code>{o["order_id"]}</code>\n🔑 OTP: <code>{html.escape(str(otp))}</code>\n\n✅ Activation completed.',parse_mode='HTML',reply_markup=kb)
                    elif r.get('raw') in {'STATUS_CANCEL','NO_ACTIVATION'}:
                        with db_tx() as conn:
                            cur=conn.execute('SELECT * FROM otp_orders WHERE order_id=? AND status="waiting" AND refunded=0',(o['order_id'],)).fetchone()
                            if cur:
                                adjust_balance(conn,cur['user_id'],'usdt',float(cur['selling_price']),'OTP_REFUND',reason=f'Quick OTP provider cancel {o["order_id"]}',related_txn=o['order_id'],processed_by=cur['user_id'])
                                conn.execute('UPDATE otp_orders SET status="cancelled",refunded=1,updated_at=? WHERE order_id=?',(_otp_now(),o['order_id']))
                        try: bot.delete_message(o['chat_id'],o['message_id'])
                        except Exception: pass
            live={x['order_id'] for x in active}; last_status={k:v for k,v in last_status.items() if k in live}
        except Exception: logger.exception('Quick OTP worker error')
        time.sleep(1)

@bot.message_handler(func=lambda m: m.content_type == "text")
@safe_handler
def generic_state_router(m):
    state = get_state(m.chat.id)
    flow = state.get("flow")
    step = state.get("step")
    if not flow:
        return  # No active flow and no menu button matched — nothing to do.

    handler = _FLOW_ROUTES.get((flow, step))
    if handler is None:
        bot.send_message(
            m.chat.id,
            "❌ Please use the buttons/menu to continue.\n\n"
            "Your message was not processed — nothing has been sent or changed.",
        )
        return
    handler(m, state)


# ================================================================
# ENTRYPOINT
# ================================================================

def main():
    init_db()
    otp_db_init()
    threading.Thread(target=_otp_worker, daemon=True, name="quick-otp-worker").start()
    threading.Thread(target=_otp_price_watcher, daemon=True, name="quick-otp-price-watcher").start()
    logger.info("%s starting…", BRAND)
    print(f"🚀 {BRAND} is starting…")
    threading.Thread(target=auto_message_scheduler, daemon=True, name="auto-message-scheduler").start()
    threading.Thread(target=audit_channel_dispatcher, daemon=True, name="audit-channel-dispatcher").start()
    bot.infinity_polling(timeout=20, long_polling_timeout=10)


if __name__ == "__main__":
    main()
