from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from app.config import settings
from app.db.models import SystemSetting
from sqlalchemy import select

async def get_setting(session,key,default=None):
    row=await session.scalar(select(SystemSetting).where(SystemSetting.key==key)); return row.value if row else default

async def check_joined(bot: Bot, session, user_id: int) -> bool:
    group_id=await get_setting(session,"official_group_id")
    channel_id=await get_setting(session,"official_channel_id")
    if not group_id or not channel_id: return False
    for chat_id in (group_id, channel_id):
        try:
            m=await bot.get_chat_member(int(chat_id), user_id)
            if m.status in {"left","kicked"}: return False
        except TelegramBadRequest: return False
    return True
