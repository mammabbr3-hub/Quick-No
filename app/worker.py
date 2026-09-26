import asyncio, logging, json
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.db.session import SessionLocal
from app.db.models import Order, Deposit, GrizzlyActivation, User, DailyReport, ScheduledMessage, MaintenanceState
from app.grizzly.client import GrizzlyClient
from app.services.orders import refund_order
from app.services.reports import snapshot, format_report
from app.services.admin import setting
from app.services.scheduler import claim_due
from app.config import settings

log=logging.getLogger(__name__)

async def poll_orders(bot=None):
    client=GrizzlyClient()
    while True:
        try:
            async with SessionLocal() as s:
                ids=(await s.scalars(select(Order.id).where(Order.status=='waiting_for_otp',Order.activation_id.is_not(None)).limit(100))).all()
                processing_ids=(await s.scalars(select(Order.id).where(Order.status=='processing',Order.activation_id.is_(None),Order.created_at <= datetime.now(timezone.utc)-timedelta(minutes=10)).limit(100))).all()
            # Never hold DB row locks while making external HTTP calls.
            for oid in ids:
                async with SessionLocal() as s:
                    o=await s.scalar(select(Order).where(Order.id==oid))
                    if not o or o.status!='waiting_for_otp' or not o.activation_id: continue
                    activation_id=o.activation_id; user_id=o.user_id
                try:
                    r=await client.get_status(activation_id)
                except Exception as e:
                    log.warning('status %s: %s',activation_id,e); continue
                async with SessionLocal() as s:
                    o=await s.scalar(select(Order).where(Order.id==oid).with_for_update())
                    if not o or o.status!='waiting_for_otp': continue
                    activation=await s.scalar(select(GrizzlyActivation).where(GrizzlyActivation.activation_id==activation_id))
                    if not activation:
                        activation=GrizzlyActivation(activation_id=activation_id,order_id=o.id); s.add(activation)
                    activation.last_raw=r.get('raw',''); activation.last_status=r.get('status')
                    otp=r.get('otp')
                    notify_tg = None
                    notify_order = None
                    notify_otp = None
                    if otp:
                        o.otp_code=str(otp); o.status='completed'
                        u=await s.scalar(select(User).where(User.id==o.user_id))
                        if u:
                            notify_tg=u.telegram_id; notify_order=o.order_id; notify_otp=o.otp_code
                    elif r.get('raw') in ('STATUS_CANCEL','NO_ACTIVATION'):
                        if not o.refunded: await refund_order(s,o)
                    await s.commit()
                if notify_tg and bot:
                    try:
                        await bot.send_message(notify_tg,f'🔐 <b>OTP received</b>\n\nOrder: <code>{notify_order}</code>\nOTP: <code>{notify_otp}</code>',parse_mode='HTML')
                    except Exception: pass
            # A lost HTTP response after getNumber is not safe to blindly retry.
            # Put stale reservations into manual reconciliation instead of risking a duplicate number/charge.
            if processing_ids:
                async with SessionLocal() as s:
                    for oid in processing_ids:
                        o=await s.scalar(select(Order).where(Order.id==oid).with_for_update())
                        if o and o.status=='processing' and not o.activation_id:
                            o.status='manual_reconciliation'
                            s.add(__import__('app.db.models',fromlist=['OrderEvent']).OrderEvent(order_id=o.id,event_type='manual_reconciliation',data='{"reason":"external_request_result_unknown"}'))
                    await s.commit()
        except Exception: log.exception('poll error')
        await asyncio.sleep(settings.poll_interval)

async def expire_deposits():
    while True:
        try:
            async with SessionLocal() as s:
                rows=(await s.scalars(select(Deposit.id).where(Deposit.status=='pending',Deposit.expires_at<=datetime.now(timezone.utc)).limit(200))).all()
                for did in rows:
                    d=await s.scalar(select(Deposit).where(Deposit.id==did).with_for_update())
                    if d and d.status=='pending' and d.expires_at<=datetime.now(timezone.utc): d.status='expired'
                await s.commit()
        except Exception: log.exception('expiry error')
        await asyncio.sleep(30)


async def process_broadcasts(bot):
    """Deliver queued broadcasts without blocking the admin handler.

    A row lock claims one broadcast at a time; delivery happens outside the
    transaction. The final update is locked again so worker restarts remain
    auditable and do not corrupt counters.
    """
    while True:
        try:
            async with SessionLocal() as s:
                b=await s.scalar(select(__import__('app.db.models',fromlist=['Broadcast']).Broadcast).where(__import__('app.db.models',fromlist=['Broadcast']).Broadcast.status=='queued').order_by(__import__('app.db.models',fromlist=['Broadcast']).Broadcast.created_at).with_for_update(skip_locked=True))
                if b:
                    b.status='sending'
                    bid=b.id
                    content_type=b.content_type; content=b.content; media=b.media_file_id; caption=b.caption; destination=b.destination
                    await s.commit()
                else:
                    bid=None
            if bid is not None:
                sent=failed=0; error=None
                if destination=='bot_users':
                    async with SessionLocal() as s:
                        targets=(await s.scalars(select(User.telegram_id).where(User.is_blocked.is_(False)))).all()
                else:
                    async with SessionLocal() as s:
                        key={'official_group':'official_group_id','official_channel':'official_channel_id','admin_log_group':'admin_log_group_id'}.get(destination)
                        cid=await setting(s,key,'') if key else ''
                        targets=[int(cid)] if cid else []
                for target in targets:
                    try:
                        if content_type=='photo': await bot.send_photo(target,media,caption=caption or '')
                        elif content_type=='document': await bot.send_document(target,media,caption=caption or '')
                        else: await bot.send_message(target,content)
                        sent+=1
                    except Exception as exc:
                        failed+=1; error=str(exc)[:1000]
                async with SessionLocal() as s:
                    b=await s.scalar(select(__import__('app.db.models',fromlist=['Broadcast']).Broadcast).where(__import__('app.db.models',fromlist=['Broadcast']).Broadcast.id==bid).with_for_update())
                    if b:
                        b.sent=sent; b.failed=failed; b.status='completed' if failed==0 else ('partial' if sent else 'failed'); b.finished_at=datetime.now(timezone.utc)
                        await s.commit()
        except Exception: log.exception('broadcast worker error')
        await asyncio.sleep(5)

async def scheduled_messages(bot):
    while True:
        try:
            async with SessionLocal() as s:
                due_rows=await claim_due(s)
                claims=[(job.id, job.last_run_at, job.content_type, job.content, job.media_file_id, job.caption, job.destination) for job in due_rows]
                await s.commit()

            for job_id, scheduled_for, content_type, content, media_file_id, caption, destination in claims:
                sent=failed=0; error=None
                async with SessionLocal() as s:
                    from app.db.models import ScheduledMessageRun
                    run=ScheduledMessageRun(schedule_id=job_id, scheduled_for=scheduled_for, status='running', started_at=datetime.now(timezone.utc))
                    s.add(run)
                    try:
                        await s.flush()
                    except Exception:
                        await s.rollback()
                        # Unique constraint means another worker already created the run.
                        continue
                    await s.commit()

                try:
                    async with SessionLocal() as s:
                        if destination == 'bot_users':
                            users=(await s.scalars(select(User).where(User.is_blocked.is_(False)))).all()
                            targets=[u.telegram_id for u in users]
                        else:
                            chat_key={'group':'official_group_id','channel':'official_channel_id','admin_log_group':'admin_log_group_id'}.get(destination)
                            chat_id=await setting(s,chat_key,'') if chat_key else ''
                            targets=[int(chat_id)] if chat_id else []

                    for target in targets:
                        try:
                            if content_type=='photo': await bot.send_photo(target,media_file_id,caption=caption or '')
                            elif content_type=='document': await bot.send_document(target,media_file_id,caption=caption or '')
                            else: await bot.send_message(target,content)
                            sent+=1
                        except Exception as exc:
                            failed+=1
                            error=str(exc)[:1000]
                except Exception as exc:
                    error=str(exc)[:1000]

                async with SessionLocal() as s:
                    from app.db.models import ScheduledMessageRun
                    run=await s.scalar(select(ScheduledMessageRun).where(ScheduledMessageRun.schedule_id==job_id, ScheduledMessageRun.scheduled_for==scheduled_for).with_for_update())
                    if run:
                        run.sent=sent; run.failed=failed; run.error=error
                        run.status='completed' if failed == 0 else ('partial' if sent else 'failed')
                        run.finished_at=datetime.now(timezone.utc)
                        await s.commit()
        except Exception: log.exception('scheduler error')
        await asyncio.sleep(20)

async def daily_reports(bot):
    while True:
        try:
            now=datetime.now(timezone.utc)
            report_to_send=None
            async with SessionLocal() as s:
                hhmm=await setting(s,'daily_report_time','00:00'); tz_name=await setting(s,'daily_report_timezone','UTC')
                from zoneinfo import ZoneInfo
                local=now.astimezone(ZoneInfo(tz_name)); marker=local.date().isoformat()
                if local.strftime('%H:%M')==hhmm:
                    exists=await s.scalar(select(DailyReport.id).where(DailyReport.report_date==marker).with_for_update())
                    if not exists:
                        d=await snapshot(s,now-timedelta(days=1),now)
                        s.add(DailyReport(report_date=marker,payload=json.dumps(d)))
                        await s.commit()
                        chat=await setting(s,'admin_log_group_id','')
                        if chat:
                            report_to_send=(int(chat), format_report(d))
            if report_to_send:
                try:
                    await bot.send_message(report_to_send[0],report_to_send[1],parse_mode='HTML')
                except Exception:
                    log.exception('daily report delivery error')
        except Exception: log.exception('report error')
        await asyncio.sleep(30)

async def main(bot=None):
    if not bot: return
    await asyncio.gather(poll_orders(bot),expire_deposits(),scheduled_messages(bot),daily_reports(bot))
