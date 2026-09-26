from aiogram import Router, F, Bot
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from sqlalchemy import select
from app.db.session import SessionLocal
from app.db.models import User, Wallet
from app.bot.keyboards import main_menu, join_gate
from app.services.join_gate import check_joined, get_setting

router=Router()

async def upsert_user(tg):
    async with SessionLocal() as s:
        u=await s.scalar(select(User).where(User.telegram_id==tg.id))
        if not u:
            u=User(telegram_id=tg.id,username=tg.username,first_name=tg.first_name,last_name=tg.last_name,display_name=tg.full_name); s.add(u); await s.flush(); s.add(Wallet(user_id=u.id)); await s.commit()
        else:
            u.username=tg.username; u.first_name=tg.first_name; u.last_name=tg.last_name; u.display_name=tg.full_name; await s.commit()
        return u

@router.message(CommandStart())
async def start(message: Message, bot: Bot):
    u=await upsert_user(message.from_user)
    async with SessionLocal() as s:
        if await check_joined(bot,s,u.telegram_id):
            await message.answer("👋 Welcome to *Quick OTP Number*\n\nYour account is ready. Choose an option below.",parse_mode="Markdown",reply_markup=main_menu())
        else:
            group=await get_setting(s,"official_group_url"); channel=await get_setting(s,"official_channel_url")
            await message.answer("🔐 *Join Required*\n\nPlease join both our official Group and Channel, then tap *I’ve Joined*.",parse_mode="Markdown",reply_markup=join_gate(group,channel))

@router.callback_query(F.data=="join_check")
async def join_check(cb: CallbackQuery, bot: Bot):
    async with SessionLocal() as s:
        if await check_joined(bot,s,cb.from_user.id):
            await cb.message.edit_text("✅ Membership verified. You can now use Quick OTP Number.")
            await cb.message.answer("🏠 Main menu",reply_markup=main_menu())
        else: await cb.answer("Please join both the Group and Channel first.",show_alert=True)
