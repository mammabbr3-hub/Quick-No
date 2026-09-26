from aiogram import Router, F
from aiogram.types import Message
from sqlalchemy import select
from app.db.session import SessionLocal
from app.db.models import SystemSetting
router=Router()
@router.message(F.text=="🎧 Support")
async def support(m):
    async with SessionLocal() as s:
        row=await s.scalar(select(SystemSetting).where(SystemSetting.key=="support_username")); username=row.value if row else None
    await m.answer(f"🎧 Support\n\nPlease contact {username}." if username else "🎧 Support is not configured yet.")
