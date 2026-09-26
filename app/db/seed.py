from sqlalchemy import select
from app.config import settings
from app.db.models import Admin,SystemSetting,MaintenanceState
async def seed(session):
    if not await session.scalar(select(Admin).where(Admin.telegram_id==settings.permanent_admin_id)):session.add(Admin(telegram_id=settings.permanent_admin_id,is_active=True))
    defaults={'official_group_id':'','official_channel_id':'','official_group_url':'','official_channel_url':'','admin_log_group_id':'','support_username':'','daily_report_time':'00:00','daily_report_timezone':'UTC','maintenance_message':'🔧 Maintenance in progress. Please try again later.','whatsapp_service_code':''}
    for k,v in defaults.items():
        if not await session.scalar(select(SystemSetting).where(SystemSetting.key==k)):session.add(SystemSetting(key=k,value=v))
    if not await session.scalar(select(MaintenanceState).where(MaintenanceState.id==1)):session.add(MaintenanceState(id=1,enabled=False,message=defaults['maintenance_message']))
