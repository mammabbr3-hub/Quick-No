from aiogram import Router, F, Bot
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from sqlalchemy import select
from app.db.session import SessionLocal
from app.db.models import User, Wallet
from app.bot.keyboards import main_menu
from app.services.admin import is_permanent
from app.admin.router import menu as admin_menu

router = Router()

async def upsert_user(tg):
    async with SessionLocal() as s:
        u = await s.scalar(select(User).where(User.telegram_id == tg.id))
        if not u:
            u = User(
                telegram_id=tg.id,
                username=tg.username,
                first_name=tg.first_name,
                last_name=tg.last_name,
                display_name=tg.full_name,
            )
            s.add(u)
            await s.flush()
            s.add(Wallet(user_id=u.id))
            await s.commit()
        else:
            u.username = tg.username
            u.first_name = tg.first_name
            u.last_name = tg.last_name
            u.display_name = tg.full_name
            await s.commit()
        return u


@router.message(CommandStart())
async def start(message: Message, bot: Bot):
    u = await upsert_user(message.from_user)

    async with SessionLocal() as s:
        # Permanent admin still goes directly to the admin dashboard.
        if await is_permanent(u.telegram_id):
            await message.answer(
                "👑 <b>Quick OTP Number Admin</b>\n\n"
                "You have permanent admin access. Use the dashboard to configure the bot.",
                parse_mode="HTML",
                reply_markup=admin_menu(),
            )
            return

        # TEMPORARY TEST MODE:
        # Mandatory Group/Channel join verification is disabled so users can
        # test the complete bot flow without joining first.
        await message.answer(
            "👋 <b>Welcome to Quick OTP Number</b>\n\n"
            "Your account is ready. Choose an option below.",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )


@router.callback_query(F.data == "join_check")
async def join_check(cb: CallbackQuery, bot: Bot):
    # TEMPORARY TEST MODE: always allow access without membership verification.
    if await is_permanent(cb.from_user.id):
        await cb.message.edit_text(
            "👑 <b>Admin access confirmed.</b>",
            parse_mode="HTML",
            reply_markup=admin_menu(),
        )
    else:
        await cb.message.edit_text(
            "✅ <b>Access confirmed.</b> You can now use Quick OTP Number.",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
    await cb.answer()
