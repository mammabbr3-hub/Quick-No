from aiogram import Router,F,Bot
from aiogram.types import Message,CallbackQuery,InlineKeyboardMarkup,InlineKeyboardButton
from sqlalchemy import select,func,desc
from app.db.session import SessionLocal
from app.db.models import *
from app.services.admin import is_admin,is_permanent,audit,setting
from app.services.deposits import approve_deposit,reject_deposit
from app.services.pricing import selling_price
from app.services.notifications import admin_log
from app.services.reports import snapshot,format_report
from app.admin.handlers import router as command_router
router=Router(); router.include_router(command_router)

def menu():
    rows=[
      [InlineKeyboardButton(text='📊 Dashboard',callback_data='a:dash'),InlineKeyboardButton(text='👥 Users',callback_data='a:users')],
      [InlineKeyboardButton(text='💳 Deposits',callback_data='a:deps'),InlineKeyboardButton(text='📦 Orders',callback_data='a:orders')],
      [InlineKeyboardButton(text='💰 Balances',callback_data='a:balances'),InlineKeyboardButton(text='🌍 Countries',callback_data='a:countries')],
      [InlineKeyboardButton(text='🪙 Payment Methods',callback_data='a:payments'),InlineKeyboardButton(text='📈 Reports',callback_data='a:reports')],
      [InlineKeyboardButton(text='📢 Announcements',callback_data='a:ann'),InlineKeyboardButton(text='📣 Broadcasts',callback_data='a:broadcasts')],
      [InlineKeyboardButton(text='⏰ Schedules',callback_data='a:schedules'),InlineKeyboardButton(text='👑 Admins',callback_data='a:admins')],
      [InlineKeyboardButton(text='🔧 Maintenance',callback_data='a:maint'),InlineKeyboardButton(text='⚙️ Settings',callback_data='a:settings')],
      [InlineKeyboardButton(text='📝 Audit Logs',callback_data='a:audit')]
    ]; return InlineKeyboardMarkup(inline_keyboard=rows)
async def guard(c):
    async with SessionLocal() as s: return await is_admin(s,c.from_user.id)
@router.message(F.text=='/admin')
async def admin_cmd(m:Message):
    if not await guard(type('C',(),{'from_user':m.from_user})()): return
    await m.answer('👑 <b>Quick OTP Number Admin</b>\n\nUse the dashboard buttons or admin commands.',parse_mode='HTML',reply_markup=menu())
@router.callback_query(F.data=='a:dash')
async def dash(c:CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized',show_alert=True)
    async with SessionLocal() as s:
      vals={
       'users':await s.scalar(select(func.count(User.id))), 'active':await s.scalar(select(func.count(User.id)).where(User.is_blocked.is_(False))),
       'blocked':await s.scalar(select(func.count(User.id)).where(User.is_blocked.is_(True))), 'orders':await s.scalar(select(func.count(Order.id))),
       'completed':await s.scalar(select(func.count(Order.id)).where(Order.status=='completed')), 'failed':await s.scalar(select(func.count(Order.id)).where(Order.status=='failed')),
       'pending':await s.scalar(select(func.count(Deposit.id)).where(Deposit.status.in_(['pending','expired']))), 'wallet':await s.scalar(select(func.coalesce(func.sum(Wallet.balance),0))),
       'revenue':await s.scalar(select(func.coalesce(func.sum(Order.selling_price),0)).where(Order.status.in_(['completed','waiting_for_otp']))),
       'profit':await s.scalar(select(func.coalesce(func.sum(Order.profit),0)).where(Order.status.in_(['completed','waiting_for_otp']))) }
    txt='📊 <b>Dashboard</b>\n\n'+'\n'.join([f'• {k.title()}: <b>{v}</b>' for k,v in vals.items()])
    await c.message.edit_text(txt,parse_mode='HTML',reply_markup=menu())
@router.callback_query(F.data=='a:deps')
async def deps(c):
    if not await guard(c): return await c.answer('Unauthorized',show_alert=True)
    async with SessionLocal() as s: rows=(await s.scalars(select(Deposit).where(Deposit.status.in_(['pending','expired'])).order_by(Deposit.created_at).limit(50))).all()
    kb=[[InlineKeyboardButton(text=f'🧾 {d.reference} • {d.usdt_amount:.2f} • {d.status}',callback_data=f'a:dep:{d.id}')] for d in rows] or [[InlineKeyboardButton(text='No pending/expired deposits',callback_data='a:back')]]
    kb.append([InlineKeyboardButton(text='⬅️ Back',callback_data='a:back')]); await c.message.edit_text('💳 <b>Deposits</b>',parse_mode='HTML',reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
@router.callback_query(F.data.startswith('a:dep:'))
async def dep(c):
    if not await guard(c): return await c.answer('Unauthorized',show_alert=True)
    did=int(c.data.split(':')[-1])
    async with SessionLocal() as s:
      d=await s.get(Deposit,did); proofs=(await s.scalars(select(PaymentProof).where(PaymentProof.deposit_id==did).order_by(PaymentProof.created_at.desc()))).all() if d else []
    if not d:return await c.answer('Not found',show_alert=True)
    dup=any(p.possible_duplicate for p in proofs)
    txt=f'🧾 <b>{d.reference}</b>\nUser: <code>{d.user_id}</code>\nAmount: <b>{d.usdt_amount:.2f} USDT</b>\nPay: <b>{d.local_amount:.2f} {d.payment_currency_snapshot}</b>\nStatus: <b>{d.status}</b>\nCreated: {d.created_at:%Y-%m-%d %H:%M UTC}\nExpires: {d.expires_at:%Y-%m-%d %H:%M UTC}\n\n<b>Payment account</b>\n<code>{d.payment_details_snapshot}</code>\n\n<b>Instructions</b>\n{d.payment_instructions_snapshot}\n\nReceipts: {len(proofs)}'+('\n⚠️ <b>DUPLICATE FLAG — manual verification required</b>' if dup else '')
    kb=[[InlineKeyboardButton(text='✅ Approve',callback_data=f'a:approve:{did}'),InlineKeyboardButton(text='❌ Reject',callback_data=f'a:reject:{did}')],[InlineKeyboardButton(text='⬅️ Back',callback_data='a:deps')]]
    await c.message.edit_text(txt,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
@router.callback_query(F.data.startswith('a:approve:'))
async def approve(c:CallbackQuery, bot:Bot):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    did=int(c.data.split(':')[-1])
    async with SessionLocal() as s:
      try:
       d=await approve_deposit(s,did,c.from_user.id); await audit(s,c.from_user.id,'approve_deposit',d.reference,new=str(d.usdt_amount)); u=await s.get(User,d.user_id); await s.commit()
       if u: await admin_log(bot,s,f'💰 Deposit approved\nRef: <code>{d.reference}</code>\nUser: <code>{u.telegram_id}</code>\nAmount: {d.usdt_amount:.2f} USDT')
      except ValueError as e: await s.rollback(); return await c.answer(e.args[0],show_alert=True)
    if u:
      try: await bot.send_message(u.telegram_id,f'✅ <b>Deposit approved</b>\n\nReference: <code>{d.reference}</code>\nAmount: <b>{d.usdt_amount:.2f} USDT</b>',parse_mode='HTML')
      except Exception: pass
    await c.answer('Approved'); await c.message.edit_text(f'✅ <b>{d.reference}</b> approved.',parse_mode='HTML',reply_markup=menu())
@router.callback_query(F.data.startswith('a:reject:'))
async def reject(c:CallbackQuery, bot:Bot):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    did=int(c.data.split(':')[-1])
    async with SessionLocal() as s:
      try:
       d=await reject_deposit(s,did,c.from_user.id,'Rejected by admin after verification'); d.status='rejected'; await audit(s,c.from_user.id,'reject_deposit',d.reference,new='rejected',reason=d.rejection_reason); u=await s.get(User,d.user_id); await s.commit()
      except ValueError as e: await s.rollback(); return await c.answer(e.args[0],show_alert=True)
    if u:
      try: await bot.send_message(u.telegram_id,f'❌ <b>Deposit rejected</b>\n\nReference: <code>{d.reference}</code>\nReason: {d.rejection_reason}',parse_mode='HTML')
      except Exception: pass
    await c.answer('Rejected'); await c.message.edit_text(f'❌ <b>{d.reference}</b> rejected.',parse_mode='HTML',reply_markup=menu())
@router.callback_query(F.data=='a:users')
async def users(c):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    async with SessionLocal() as s: rows=(await s.scalars(select(User).order_by(desc(User.created_at)).limit(30))).all()
    await c.message.edit_text('👥 <b>Users</b>\n\n'+'\n'.join(f'• <code>{u.telegram_id}</code> @{u.username or "—"} — {"BLOCKED" if u.is_blocked else "active"}' for u in rows) or 'No users',parse_mode='HTML',reply_markup=menu())
@router.callback_query(F.data=='a:orders')
async def orders(c):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    async with SessionLocal() as s: rows=(await s.scalars(select(Order).order_by(desc(Order.created_at)).limit(30))).all()
    await c.message.edit_text('📦 <b>Orders</b>\n\n'+'\n'.join(f'• <code>{o.order_id}</code> — {o.country_name} — {o.status} — {o.selling_price:.2f}' for o in rows) or 'No orders',parse_mode='HTML',reply_markup=menu())
@router.callback_query(F.data=='a:countries')
async def countries(c):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    async with SessionLocal() as s: rows=(await s.scalars(select(Country).order_by(Country.name))).all()
    await c.message.edit_text('🌍 <b>Countries / Pricing</b>\n\n'+'\n'.join(f'{x.flag} {x.name} [{x.code}] — {selling_price(x) or "—"} USDT — {"ON" if x.enabled else "OFF"}' for x in rows) or 'No countries',parse_mode='HTML',reply_markup=menu())
@router.callback_query(F.data=='a:payments')
async def payments(c):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    async with SessionLocal() as s: rows=(await s.scalars(select(PaymentMethod).order_by(PaymentMethod.display_order,PaymentMethod.id))).all()
    await c.message.edit_text('🪙 <b>Payment Methods</b>\n\n'+'\n'.join(f'• #{x.id} {x.name} — 1 USDT={x.exchange_rate} {x.currency} — min {x.min_deposit} — {"ON" if x.enabled else "OFF"}' for x in rows) or 'No payment methods',parse_mode='HTML',reply_markup=menu())
@router.callback_query(F.data=='a:balances')
async def balances(c):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    await c.message.edit_text('💰 <b>Balances</b>\nUse <code>/balance TELEGRAM_ID add|deduct AMOUNT reason</code> for audited manual adjustments.',parse_mode='HTML',reply_markup=menu())
@router.callback_query(F.data=='a:reports')
async def reports(c):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    async with SessionLocal() as s: d=await snapshot(s)
    await c.message.edit_text(format_report(d),parse_mode='HTML',reply_markup=menu())
@router.callback_query(F.data=='a:ann')
async def ann(c):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    async with SessionLocal() as s: rows=(await s.scalars(select(Announcement).order_by(desc(Announcement.created_at)).limit(20))).all()
    await c.message.edit_text('📢 <b>Announcements</b>\n\n'+'\n'.join(f'• #{x.id} <b>{x.title}</b> — {"ON" if x.enabled else "OFF"}' for x in rows) or 'No announcements',parse_mode='HTML',reply_markup=menu())
@router.callback_query(F.data=='a:broadcasts')
async def broadcasts(c):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    async with SessionLocal() as s: rows=(await s.scalars(select(Broadcast).order_by(desc(Broadcast.created_at)).limit(20))).all()
    await c.message.edit_text('📣 <b>Broadcasts</b>\n\n'+'\n'.join(f'• #{x.id} — {x.destination} — {x.status} — {x.sent}/{x.total}' for x in rows) or 'No broadcasts',parse_mode='HTML',reply_markup=menu())
@router.callback_query(F.data=='a:schedules')
async def schedules(c):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    async with SessionLocal() as s: rows=(await s.scalars(select(ScheduledMessage).order_by(desc(ScheduledMessage.created_at)).limit(20))).all()
    await c.message.edit_text('⏰ <b>Scheduled Messages</b>\n\n'+'\n'.join(f'• #{x.id} {x.name} — {x.cron} — {x.timezone} — {x.destination} — {"ON" if x.enabled else "OFF"}' for x in rows) or 'No schedules',parse_mode='HTML',reply_markup=menu())
@router.callback_query(F.data=='a:admins')
async def admins(c):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    async with SessionLocal() as s: rows=(await s.scalars(select(Admin).order_by(Admin.id))).all()
    await c.message.edit_text('👑 <b>Admins</b>\n\n'+'\n'.join(f'• <code>{x.telegram_id}</code> @{x.username or "—"} — {"ACTIVE" if x.is_active else "REMOVED"}' for x in rows)+'\n\nPermanent admin only: /admin_add and /admin_remove',parse_mode='HTML',reply_markup=menu())
@router.callback_query(F.data=='a:maint')
async def maint(c):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    async with SessionLocal() as s: st=await s.get(MaintenanceState,1)
    await c.message.edit_text(f'🔧 <b>Maintenance</b>: {"ON" if st and st.enabled else "OFF"}\n\nUse /maintenance on|off [message].',parse_mode='HTML',reply_markup=menu())
@router.callback_query(F.data=='a:settings')
async def settings_page(c):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    async with SessionLocal() as s:
      keys=['support_username','daily_report_time','daily_report_timezone','official_group_id','official_channel_id','admin_log_group_id']
      vals=[f'• {k}: <code>{await setting(s,k,"—")}</code>' for k in keys]
    await c.message.edit_text('⚙️ <b>Settings</b>\n\n'+'\n'.join(vals)+'\n\nInfrastructure IDs can only be changed by the permanent admin.',parse_mode='HTML',reply_markup=menu())
@router.callback_query(F.data=='a:audit')
async def audit_logs(c):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    async with SessionLocal() as s: rows=(await s.scalars(select(AdminAuditLog).order_by(desc(AdminAuditLog.created_at)).limit(30))).all()
    await c.message.edit_text('📝 <b>Audit Logs</b>\n\n'+'\n'.join(f'• {x.created_at:%m-%d %H:%M} {x.action} → {x.target or ""}' for x in rows) or 'No audit logs',parse_mode='HTML',reply_markup=menu())
@router.callback_query(F.data=='a:back')
async def back(c):
    if await guard(c): await c.message.edit_text('👑 <b>Quick OTP Number Admin</b>',parse_mode='HTML',reply_markup=menu())
