import json
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from sqlalchemy import select,func
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import User,Order,Deposit,Wallet

async def snapshot(session:AsyncSession,start=None,end=None):
    start=start or datetime.now(timezone.utc)-timedelta(days=1); end=end or datetime.now(timezone.utc)
    def count(model,status=None):
        q=select(func.count(model.id)).where(model.created_at>=start,model.created_at<end)
        if status:q=q.where(model.status==status)
        return session.scalar(q)
    top=(await session.execute(select(Order.country_name,func.count(Order.id).label('n')).where(Order.created_at>=start,Order.created_at<end).group_by(Order.country_name).order_by(func.count(Order.id).desc()).limit(5))).all()
    return {'period_start':start.isoformat(),'period_end':end.isoformat(),'new_users':await session.scalar(select(func.count(User.id)).where(User.created_at>=start,User.created_at<end)),'orders':await count(Order),'completed_orders':await count(Order,'completed'),'failed_orders':await count(Order,'failed'),'cancelled_orders':await count(Order,'cancelled'),'refunded_orders':await count(Order,'refunded'),'numbers_received':await session.scalar(select(func.count(Order.id)).where(Order.created_at>=start,Order.created_at<end,Order.phone_number.is_not(None))),'deposits':await count(Deposit),'approved_deposits':await count(Deposit,'approved'),'rejected_deposits':await count(Deposit,'rejected'),'revenue':str(await session.scalar(select(func.coalesce(func.sum(Order.selling_price),0)).where(Order.created_at>=start,Order.created_at<end,Order.status.in_(['completed','waiting_for_otp'])))),'grizzly_cost':str(await session.scalar(select(func.coalesce(func.sum(Order.raw_cost),0)).where(Order.created_at>=start,Order.created_at<end,Order.raw_cost.is_not(None)))),'profit':str(await session.scalar(select(func.coalesce(func.sum(Order.profit),0)).where(Order.created_at>=start,Order.created_at<end,Order.status.in_(['completed','waiting_for_otp'])))),'top_countries':', '.join(f'{n} ({cnt})' for n,cnt in top) or '—'}

def format_report(d):
    return ('📊 <b>Daily Report</b>\n\n' + '\n'.join([f'• {k.replace("_"," ").title()}: <b>{v}</b>' for k,v in d.items() if k not in ('period_start','period_end')]))
