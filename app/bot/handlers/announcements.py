from aiogram import Router,F,Bot
from aiogram.types import Message
from sqlalchemy import select
from app.db.session import SessionLocal
from app.db.models import User,Announcement
from app.services.access import allowed
router=Router()
@router.message(F.text=='📢 Announcements')
async def announcements(m:Message,bot:Bot):
    async with SessionLocal() as s:
        u=await s.scalar(select(User).where(User.telegram_id==m.from_user.id));ok,msg=await allowed(bot,s,u)
        if not ok:return await m.answer(msg)
        rows=(await s.scalars(select(Announcement).where(Announcement.enabled.is_(True)).order_by(Announcement.created_at.desc()).limit(10))).all()
    if not rows:return await m.answer('📢 No announcements yet.')
    await m.answer('📢 <b>Announcements</b>\n\n'+'\n\n'.join(f'<b>{a.title}</b>\n{a.body}' for a in rows),parse_mode='HTML')
