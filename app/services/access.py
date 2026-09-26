from sqlalchemy import select
from app.db.models import User,MaintenanceState
from app.services.join_gate import check_joined
async def get_user(session,tg_id):return await session.scalar(select(User).where(User.telegram_id==tg_id))
async def allowed(bot,session,user,financial=False):
    if not user:return False,'Please use /start first.'
    if user.is_blocked:return False,'🚫 Your account is blocked. You can still contact Support.'
    joined=await check_joined(bot,session,user.telegram_id)
    if not joined:return False,'🔐 Please join both the official Group and Channel first.'
    if financial:
        m=await session.scalar(select(MaintenanceState).where(MaintenanceState.id==1))
        if m and m.enabled:return False,f'🔧 {m.message}'
    return True,''
