from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.db.models import Admin, SystemSetting, AdminAuditLog

async def is_admin(session: AsyncSession, tg_id:int)->bool:
    if tg_id == settings.permanent_admin_id: return True
    return bool(await session.scalar(select(Admin.id).where(Admin.telegram_id==tg_id, Admin.is_active.is_(True))))
async def is_permanent(tg_id:int)->bool: return tg_id == settings.permanent_admin_id
async def setting(session,key,default=''):
    row=await session.scalar(select(SystemSetting).where(SystemSetting.key==key)); return row.value if row else default
async def set_setting(session,key,value,actor=None):
    row=await session.scalar(select(SystemSetting).where(SystemSetting.key==key))
    old=row.value if row else None
    if row: row.value=str(value); row.updated_by=actor
    else: session.add(SystemSetting(key=key,value=str(value),updated_by=actor))
    if actor: session.add(AdminAuditLog(admin_id=actor,action='set_setting',target=key,old_value=old,new_value=str(value)))
async def audit(session,admin_id,action,target=None,old=None,new=None,reason=None,result='success'):
    session.add(AdminAuditLog(admin_id=admin_id,action=action,target=target,old_value=old,new_value=new,reason=reason,result=result))
