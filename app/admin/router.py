from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, func, desc
from sqlalchemy.exc import IntegrityError
from decimal import Decimal

from app.db.session import SessionLocal
from app.db.models import *
from app.services.admin import is_admin, is_permanent, audit, setting
from app.services.deposits import approve_deposit, reject_deposit
from app.services.pricing import selling_price
from app.services.notifications import admin_log
from app.services.reports import snapshot, format_report
from app.services.orders import refund_order
from app.services.wallet import change_balance
from app.admin.handlers import router as command_router

router = Router()
router.include_router(command_router)


def btn(text: str, data: str):
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def menu():
    return kb([
        [btn('📊 Dashboard', 'a:dash'), btn('👥 Users', 'a:users')],
        [btn('💳 Deposits', 'a:deps'), btn('📦 Orders', 'a:orders')],
        [btn('💰 Balances', 'a:balances'), btn('🌍 Countries', 'a:countries')],
        [btn('🪙 Payment Methods', 'a:payments'), btn('📈 Reports', 'a:reports')],
        [btn('📢 Announcements', 'a:ann'), btn('📣 Broadcasts', 'a:broadcasts')],
        [btn('⏰ Schedules', 'a:schedules'), btn('👑 Admins', 'a:admins')],
        [btn('🔧 Maintenance', 'a:maint'), btn('⚙️ Settings', 'a:settings')],
        [btn('📝 Audit Logs', 'a:audit')],
    ])


def back(data='a:back'):
    return [btn('⬅️ Back', data)]


async def guard(c):
    async with SessionLocal() as s:
        return await is_admin(s, c.from_user.id)


async def deny(c):
    await c.answer('Unauthorized', show_alert=True)


@router.message(F.text == '/admin')
async def admin_cmd(m: Message):
    async with SessionLocal() as s:
        if not await is_admin(s, m.from_user.id):
            return
    await m.answer('👑 <b>Quick OTP Number Admin</b>\n\nSelect an action:', parse_mode='HTML', reply_markup=menu())


@router.callback_query(F.data == 'a:dash')
async def dash(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    async with SessionLocal() as s:
        vals = {
            'Users': await s.scalar(select(func.count(User.id))),
            'Active': await s.scalar(select(func.count(User.id)).where(User.is_blocked.is_(False))),
            'Blocked': await s.scalar(select(func.count(User.id)).where(User.is_blocked.is_(True))),
            'Orders': await s.scalar(select(func.count(Order.id))),
            'Completed': await s.scalar(select(func.count(Order.id)).where(Order.status == 'completed')),
            'Failed': await s.scalar(select(func.count(Order.id)).where(Order.status == 'failed')),
            'Pending deposits': await s.scalar(select(func.count(Deposit.id)).where(Deposit.status.in_(['pending', 'expired']))),
            'Wallet': await s.scalar(select(func.coalesce(func.sum(Wallet.balance), 0))),
            'Revenue': await s.scalar(select(func.coalesce(func.sum(Order.selling_price), 0)).where(Order.status.in_(['completed', 'waiting_for_otp']))),
            'Profit': await s.scalar(select(func.coalesce(func.sum(Order.profit), 0)).where(Order.status.in_(['completed', 'waiting_for_otp'])))
        }
    txt = '📊 <b>Dashboard</b>\n\n' + '\n'.join(f'• {k}: <b>{v}</b>' for k, v in vals.items())
    await c.message.edit_text(txt, parse_mode='HTML', reply_markup=menu())
    await c.answer()


@router.callback_query(F.data == 'a:deps')
async def deps(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    async with SessionLocal() as s:
        rows = (await s.scalars(select(Deposit).where(Deposit.status.in_(['pending', 'expired'])).order_by(Deposit.created_at).limit(50))).all()
    rows_kb = [[btn(f'🧾 {d.reference} • {d.usdt_amount:.2f} USDT', f'a:dep:{d.id}')] for d in rows]
    if not rows:
        rows_kb = [[btn('ℹ️ No pending deposits', 'a:noop')]]
    rows_kb.append(back())
    await c.message.edit_text('💳 <b>Deposits</b>\n\nSelect a deposit:', parse_mode='HTML', reply_markup=kb(rows_kb))
    await c.answer()


@router.callback_query(F.data.startswith('a:dep:'))
async def dep(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    did = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        d = await s.get(Deposit, did)
        proofs = (await s.scalars(select(PaymentProof).where(PaymentProof.deposit_id == did).order_by(PaymentProof.created_at.desc()))).all() if d else []
    if not d:
        return await c.answer('Not found', show_alert=True)
    dup = any(p.possible_duplicate for p in proofs)
    txt = (
        f'🧾 <b>{d.reference}</b>\n'
        f'Amount: <b>{d.usdt_amount:.2f} USDT</b>\n'
        f'Pay: <b>{d.local_amount:.2f} {d.payment_currency_snapshot}</b>\n'
        f'Status: <b>{d.status}</b>\n'
        f'Created: {d.created_at:%Y-%m-%d %H:%M UTC}\n'
        f'Expires: {d.expires_at:%Y-%m-%d %H:%M UTC}\n\n'
        f'<b>Payment account</b>\n<code>{d.payment_details_snapshot}</code>\n\n'
        f'<b>Instructions</b>\n{d.payment_instructions_snapshot}\n\n'
        f'Receipts: {len(proofs)}'
    )
    if dup:
        txt += '\n⚠️ <b>DUPLICATE FLAG — manual verification required</b>'
    rows = []
    if d.status in ('pending', 'expired'):
        rows.append([btn('✅ Approve', f'a:approve:{did}'), btn('❌ Reject', f'a:reject:{did}')])
    rows.append(back('a:deps'))
    await c.message.edit_text(txt, parse_mode='HTML', reply_markup=kb(rows))
    await c.answer()


@router.callback_query(F.data.startswith('a:approve:'))
async def approve(c:CallbackQuery, bot:Bot):
    if not await guard(c): return await deny(c)
    did = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        try:
            d = await approve_deposit(s,did,c.from_user.id)
            await audit(s, c.from_user.id, 'approve_deposit', d.reference, new=str(d.usdt_amount))
            u = await s.get(User, d.user_id)
            await s.commit()
        except ValueError as e:
            await s.rollback()
            return await c.answer(str(e.args[0]), show_alert=True)
    if u:
        try:
            await bot.send_message(u.telegram_id, f'✅ <b>Deposit approved</b>\n\nReference: <code>{d.reference}</code>\nAmount: <b>{d.usdt_amount:.2f} USDT</b>', parse_mode='HTML')
        except Exception:
            pass
    await c.answer('Approved')
    await c.message.edit_text(f'✅ <b>{d.reference}</b> approved.', parse_mode='HTML', reply_markup=kb([back('a:deps')]))


@router.callback_query(F.data.startswith('a:reject:'))
async def reject(c:CallbackQuery, bot:Bot):
    if not await guard(c): return await deny(c)
    did = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        try:
            d = await reject_deposit(s,did,c.from_user.id,'Rejected by admin after verification')
            await audit(s, c.from_user.id, 'reject_deposit', d.reference, new='rejected', reason=d.rejection_reason)
            u = await s.get(User, d.user_id)
            await s.commit()
        except ValueError as e:
            await s.rollback()
            return await c.answer(str(e.args[0]), show_alert=True)
    if u:
        try:
            await bot.send_message(u.telegram_id, f'❌ <b>Deposit rejected</b>\n\nReference: <code>{d.reference}</code>\nReason: {d.rejection_reason}', parse_mode='HTML')
        except Exception:
            pass
    await c.answer('Rejected')
    await c.message.edit_text(f'❌ <b>{d.reference}</b> rejected.', parse_mode='HTML', reply_markup=kb([back('a:deps')]))


@router.callback_query(F.data == 'a:users')
async def users(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    async with SessionLocal() as s:
        rows = (await s.scalars(select(User).order_by(desc(User.created_at)).limit(50))).all()
    rows_kb = [[btn(f'👤 {u.display_name[:28]}', f'a:user:{u.id}')] for u in rows]
    if not rows:
        rows_kb = [[btn('ℹ️ No users', 'a:noop')]]
    rows_kb.append(back())
    await c.message.edit_text('👥 <b>Users</b>\n\nNo usernames or handles are shown here. Select a user:', parse_mode='HTML', reply_markup=kb(rows_kb))
    await c.answer()


@router.callback_query(F.data.startswith('a:user:'))
async def user_detail(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    uid = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        u = await s.get(User, uid)
        w = await s.scalar(select(Wallet).where(Wallet.user_id == uid))
    if not u: return await c.answer('User not found', show_alert=True)
    status = '🚫 BLOCKED' if u.is_blocked else '✅ ACTIVE'
    txt = f'👤 <b>User</b>\n\nName: <b>{u.display_name}</b>\nStatus: <b>{status}</b>\nOrders: <b>{u.total_orders}</b>\nBalance: <b>{w.balance if w else 0}</b> USDT'
    action = btn('✅ Unblock', f'a:unblock:{uid}') if u.is_blocked else btn('🚫 Block', f'a:block:{uid}')
    await c.message.edit_text(txt, parse_mode='HTML', reply_markup=kb([[action], [btn('⬅️ Back', 'a:users')]]))
    await c.answer()


@router.callback_query(F.data.startswith('a:block:'))
async def block_user(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    uid = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        u = await s.get(User, uid)
        if not u: return await c.answer('User not found', show_alert=True)
        u.is_blocked = True
        u.block_reason = 'Blocked by admin'
        await audit(s, c.from_user.id, 'block_user', str(uid), new='blocked')
        await s.commit()
    await c.answer('Blocked')
    await user_detail(c)


@router.callback_query(F.data.startswith('a:unblock:'))
async def unblock_user(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    uid = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        u = await s.get(User, uid)
        if not u: return await c.answer('User not found', show_alert=True)
        u.is_blocked = False
        u.block_reason = None
        await audit(s, c.from_user.id, 'unblock_user', str(uid), new='active')
        await s.commit()
    await c.answer('Unblocked')
    await user_detail(c)


@router.callback_query(F.data == 'a:orders')
async def orders(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    async with SessionLocal() as s:
        rows = (await s.scalars(select(Order).order_by(desc(Order.created_at)).limit(50))).all()
    rows_kb = [[btn(f'📦 {o.order_id} • {o.country_name} • {o.status}', f'a:order:{o.id}')] for o in rows]
    if not rows: rows_kb = [[btn('ℹ️ No orders', 'a:noop')]]
    rows_kb.append(back())
    await c.message.edit_text('📦 <b>Orders</b>\n\nSelect an order:', parse_mode='HTML', reply_markup=kb(rows_kb))
    await c.answer()


@router.callback_query(F.data.startswith('a:order:'))
async def order_detail(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    oid = int(c.data.split(':')[-1])
    async with SessionLocal() as s: o = await s.get(Order, oid)
    if not o: return await c.answer('Order not found', show_alert=True)
    txt = f'📦 <b>{o.order_id}</b>\nCountry: <b>{o.country_name}</b>\nStatus: <b>{o.status}</b>\nPrice: <b>{o.selling_price}</b> USDT\nNumber: <code>{o.phone_number or "—"}</code>\nOTP: <code>{o.otp_code or "—"}</code>\nRefunded: <b>{"YES" if o.refunded else "NO"}</b>'
    rows = []
    if not o.refunded and o.status != 'refunded': rows.append([btn('↩️ Refund', f'a:refund:{oid}')])
    rows.append(back('a:orders'))
    await c.message.edit_text(txt, parse_mode='HTML', reply_markup=kb(rows))
    await c.answer()


@router.callback_query(F.data.startswith('a:refund:'))
async def refund(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    oid = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        o = await s.get(Order, oid)
        if not o: return await c.answer('Order not found', show_alert=True)
        try:
            ok = await refund_order(s, o, c.from_user.id)
            if not ok: return await c.answer('Already refunded', show_alert=True)
            await audit(s, c.from_user.id, 'refund_order', o.order_id, new='refunded')
            await s.commit()
        except ValueError as e:
            await s.rollback(); return await c.answer(str(e.args[0]), show_alert=True)
    await c.answer('Refunded')
    await c.message.edit_text(f'↩️ <b>{o.order_id}</b> refunded.', parse_mode='HTML', reply_markup=kb([back('a:orders')]))


@router.callback_query(F.data == 'a:countries')
async def countries(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    async with SessionLocal() as s: rows = (await s.scalars(select(Country).order_by(Country.name))).all()
    rows_kb = [[btn(f'{x.flag} {x.name}', f'a:country:{x.id}')] for x in rows]
    if not rows: rows_kb = [[btn('➕ Add Country', 'a:country_help')]]
    else: rows_kb.append([btn('➕ Add Country', 'a:country_help')])
    rows_kb.append(back())
    await c.message.edit_text('🌍 <b>Countries / Pricing</b>\n\nSelect a country:', parse_mode='HTML', reply_markup=kb(rows_kb))
    await c.answer()


@router.callback_query(F.data.startswith('a:country:'))
async def country_detail(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    cid = int(c.data.split(':')[-1])
    async with SessionLocal() as s: x = await s.get(Country, cid)
    if not x: return await c.answer('Country not found', show_alert=True)
    price = selling_price(x)
    txt = f'{x.flag} <b>{x.name}</b>\nCode: <code>{x.code}</code>\nService: <code>{x.service_code}</code>\nCost: <b>{x.grizzly_cost or "—"}</b>\nSelling price: <b>{price or "—"}</b> USDT\nStatus: <b>{"ON" if x.enabled else "OFF"}</b>'
    toggle = btn('🔴 Disable', f'a:country_toggle:{cid}') if x.enabled else btn('🟢 Enable', f'a:country_toggle:{cid}')
    await c.message.edit_text(txt, parse_mode='HTML', reply_markup=kb([[toggle, btn('🗑 Delete', f'a:country_delete:{cid}')], back('a:countries')]))
    await c.answer()


@router.callback_query(F.data == 'a:country_help')
async def country_help(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    await c.message.edit_text('🌍 <b>Add Country</b>\n\nUse the existing <code>/country add</code> command to create/update a country.\n\nThe country will then appear here as an inline button.', parse_mode='HTML', reply_markup=kb([back('a:countries')]))
    await c.answer()


@router.callback_query(F.data.startswith('a:country_toggle:'))
async def country_toggle(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    cid = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        x = await s.get(Country, cid)
        if not x: return await c.answer('Country not found', show_alert=True)
        x.enabled = not x.enabled
        await audit(s, c.from_user.id, 'country_toggle', x.code, new=str(x.enabled)); await s.commit()
    await c.answer('Updated')
    await country_detail(c)


@router.callback_query(F.data.startswith('a:country_delete:'))
async def country_delete(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    cid = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        x = await s.get(Country, cid)
        if not x: return await c.answer('Country not found', show_alert=True)
        code = x.code
        await s.delete(x); await audit(s, c.from_user.id, 'country_delete', code); await s.commit()
    await c.answer('Deleted')
    await countries(c)


@router.callback_query(F.data == 'a:payments')
async def payments(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    async with SessionLocal() as s: rows = (await s.scalars(select(PaymentMethod).order_by(PaymentMethod.display_order, PaymentMethod.id))).all()
    rows_kb = [[btn(f'💳 {x.name}', f'a:payment:{x.id}')] for x in rows]
    if not rows: rows_kb = [[btn('➕ Add Method', 'a:payment_help')]]
    else: rows_kb.append([btn('➕ Add Method', 'a:payment_help')])
    rows_kb.append(back())
    await c.message.edit_text('🪙 <b>Payment Methods</b>\n\nSelect a method:', parse_mode='HTML', reply_markup=kb(rows_kb))
    await c.answer()


@router.callback_query(F.data.startswith('a:payment:'))
async def payment_detail(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    pid = int(c.data.split(':')[-1])
    async with SessionLocal() as s: x = await s.get(PaymentMethod, pid)
    if not x: return await c.answer('Payment method not found', show_alert=True)
    txt = f'💳 <b>{x.name}</b>\nCurrency: <b>{x.currency}</b>\nRate: <b>{x.exchange_rate}</b>\nMinimum: <b>{x.min_deposit}</b>\nStatus: <b>{"ON" if x.enabled else "OFF"}</b>'
    toggle = btn('🔴 Disable', f'a:payment_toggle:{pid}') if x.enabled else btn('🟢 Enable', f'a:payment_toggle:{pid}')
    await c.message.edit_text(txt, parse_mode='HTML', reply_markup=kb([[toggle, btn('🗑 Delete', f'a:payment_delete:{pid}')], back('a:payments')]))
    await c.answer()


@router.callback_query(F.data == 'a:payment_help')
async def payment_help(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    await c.message.edit_text('🪙 <b>Add Payment Method</b>\n\nUse the existing <code>/payment_method add</code> command to create one. It will appear here as an inline button.', parse_mode='HTML', reply_markup=kb([back('a:payments')]))
    await c.answer()


@router.callback_query(F.data.startswith('a:payment_toggle:'))
async def payment_toggle(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    pid = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        x = await s.get(PaymentMethod, pid)
        if not x: return await c.answer('Payment method not found', show_alert=True)
        x.enabled = not x.enabled; await audit(s, c.from_user.id, 'payment_toggle', str(pid), new=str(x.enabled)); await s.commit()
    await c.answer('Updated'); await payment_detail(c)


@router.callback_query(F.data.startswith('a:payment_delete:'))
async def payment_delete(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    pid = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        x = await s.get(PaymentMethod, pid)
        if not x: return await c.answer('Payment method not found', show_alert=True)
        linked = await s.scalar(select(Deposit.id).where(Deposit.payment_method_id == pid).limit(1))
        if linked: return await c.answer('Has deposit history — disable instead', show_alert=True)
        name = x.name; await s.delete(x); await audit(s, c.from_user.id, 'payment_delete', str(pid), old=name); await s.commit()
    await c.answer('Deleted'); await payments(c)


@router.callback_query(F.data == 'a:balances')
async def balances(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    async with SessionLocal() as s:
        rows = (await s.scalars(select(User).order_by(desc(User.last_activity_at)).limit(50))).all()
    rows_kb = [[btn(f'💰 {u.display_name[:28]}', f'a:bal:{u.id}')] for u in rows]
    if not rows: rows_kb = [[btn('ℹ️ No users', 'a:noop')]]
    rows_kb.append(back())
    await c.message.edit_text('💰 <b>Balances</b>\n\nSelect a user. User handles are hidden:', parse_mode='HTML', reply_markup=kb(rows_kb))
    await c.answer()


@router.callback_query(F.data.startswith('a:bal:'))
async def balance_detail(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    uid = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        u = await s.get(User, uid); w = await s.scalar(select(Wallet).where(Wallet.user_id == uid))
    if not u: return await c.answer('User not found', show_alert=True)
    txt = f'💰 <b>Balance</b>\nUser: <b>{u.display_name}</b>\nCurrent: <b>{w.balance if w else 0}</b> USDT\n\nAmount entry still uses the audited /balance command.'
    await c.message.edit_text(txt, parse_mode='HTML', reply_markup=kb([[btn('➕ Add', f'a:bal_help:{uid}:add'), btn('➖ Deduct', f'a:bal_help:{uid}:deduct')], back('a:balances')]))
    await c.answer()


@router.callback_query(F.data.startswith('a:bal_help:'))
async def balance_help(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    _, _, uid, action = c.data.split(':')
    await c.message.edit_text(f'💰 <b>{action.title()} balance</b>\n\nUse the audited command:\n<code>/balance TELEGRAM_ID {action} AMOUNT reason</code>\n\nThe admin panel deliberately does not expose the user handle.', parse_mode='HTML', reply_markup=kb([back(f'a:bal:{uid}')]))
    await c.answer()


@router.callback_query(F.data == 'a:reports')
async def reports(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    async with SessionLocal() as s: d = await snapshot(s)
    await c.message.edit_text(format_report(d), parse_mode='HTML', reply_markup=kb([back()]))
    await c.answer()


@router.callback_query(F.data == 'a:ann')
async def ann(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    async with SessionLocal() as s: rows = (await s.scalars(select(Announcement).order_by(desc(Announcement.created_at)).limit(30))).all()
    rows_kb = [[btn(f'📢 {x.title[:30]}', f'a:announcement:{x.id}')] for x in rows]
    if not rows: rows_kb = [[btn('ℹ️ No announcements', 'a:noop')]]
    rows_kb.append(back())
    await c.message.edit_text('📢 <b>Announcements</b>\n\nSelect an announcement:', parse_mode='HTML', reply_markup=kb(rows_kb))
    await c.answer()


@router.callback_query(F.data.startswith('a:announcement:'))
async def announcement_detail(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    aid = int(c.data.split(':')[-1])
    async with SessionLocal() as s: x = await s.get(Announcement, aid)
    if not x: return await c.answer('Not found', show_alert=True)
    txt = f'📢 <b>{x.title}</b>\n\n{x.body}\n\nStatus: <b>{"ON" if x.enabled else "OFF"}</b>'
    toggle = btn('🔴 Disable', f'a:announcement_toggle:{aid}') if x.enabled else btn('🟢 Enable', f'a:announcement_toggle:{aid}')
    await c.message.edit_text(txt, parse_mode='HTML', reply_markup=kb([[toggle, btn('🗑 Delete', f'a:announcement_delete:{aid}')], back('a:ann')]))
    await c.answer()


@router.callback_query(F.data.startswith('a:announcement_toggle:'))
async def announcement_toggle(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    aid = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        x = await s.get(Announcement, aid)
        if not x: return await c.answer('Not found', show_alert=True)
        x.enabled = not x.enabled; await audit(s, c.from_user.id, 'announcement_toggle', str(aid), new=str(x.enabled)); await s.commit()
    await c.answer('Updated'); await announcement_detail(c)


@router.callback_query(F.data.startswith('a:announcement_delete:'))
async def announcement_delete(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    aid = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        x = await s.get(Announcement, aid)
        if not x: return await c.answer('Not found', show_alert=True)
        await s.delete(x); await audit(s, c.from_user.id, 'announcement_delete', str(aid)); await s.commit()
    await c.answer('Deleted'); await ann(c)


@router.callback_query(F.data == 'a:broadcasts')
async def broadcasts(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    async with SessionLocal() as s: rows = (await s.scalars(select(Broadcast).order_by(desc(Broadcast.created_at)).limit(30))).all()
    rows_kb = [[btn(f'📣 #{x.id} • {x.status}', f'a:broadcast:{x.id}')] for x in rows]
    if not rows: rows_kb = [[btn('ℹ️ No broadcasts', 'a:noop')]]
    rows_kb.append(back())
    await c.message.edit_text('📣 <b>Broadcasts</b>\n\nSelect a broadcast:', parse_mode='HTML', reply_markup=kb(rows_kb))
    await c.answer()


@router.callback_query(F.data.startswith('a:broadcast:'))
async def broadcast_detail(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    bid = int(c.data.split(':')[-1])
    async with SessionLocal() as s: x = await s.get(Broadcast, bid)
    if not x: return await c.answer('Not found', show_alert=True)
    txt = f'📣 <b>Broadcast #{x.id}</b>\nDestination: <b>{x.destination}</b>\nStatus: <b>{x.status}</b>\nProgress: <b>{x.sent}/{x.total}</b>\nFailed: <b>{x.failed}</b>'
    rows = [[btn('🗑 Delete', f'a:broadcast_delete:{bid}')], back('a:broadcasts')]
    await c.message.edit_text(txt, parse_mode='HTML', reply_markup=kb(rows))
    await c.answer()


@router.callback_query(F.data.startswith('a:broadcast_delete:'))
async def broadcast_delete(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    bid = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        x = await s.get(Broadcast, bid)
        if not x: return await c.answer('Not found', show_alert=True)
        if x.status in ('running', 'sending'): return await c.answer('Cannot delete while sending', show_alert=True)
        await s.delete(x); await audit(s, c.from_user.id, 'broadcast_delete', str(bid)); await s.commit()
    await c.answer('Deleted'); await broadcasts(c)


@router.callback_query(F.data == 'a:schedules')
async def schedules(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    async with SessionLocal() as s: rows = (await s.scalars(select(ScheduledMessage).order_by(desc(ScheduledMessage.created_at)).limit(30))).all()
    rows_kb = [[btn(f'⏰ {x.name[:28]}', f'a:schedule:{x.id}')] for x in rows]
    if not rows: rows_kb = [[btn('ℹ️ No schedules', 'a:noop')]]
    rows_kb.append(back())
    await c.message.edit_text('⏰ <b>Schedules</b>\n\nSelect a schedule:', parse_mode='HTML', reply_markup=kb(rows_kb))
    await c.answer()


@router.callback_query(F.data.startswith('a:schedule:'))
async def schedule_detail(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    sid = int(c.data.split(':')[-1])
    async with SessionLocal() as s: x = await s.get(ScheduledMessage, sid)
    if not x: return await c.answer('Not found', show_alert=True)
    txt = f'⏰ <b>{x.name}</b>\nCron: <code>{x.cron}</code>\nTimezone: <code>{x.timezone}</code>\nDestination: <b>{x.destination}</b>\nStatus: <b>{"ON" if x.enabled else "OFF"}</b>'
    toggle = btn('🔴 Disable', f'a:schedule_toggle:{sid}') if x.enabled else btn('🟢 Enable', f'a:schedule_toggle:{sid}')
    await c.message.edit_text(txt, parse_mode='HTML', reply_markup=kb([[toggle, btn('🗑 Delete', f'a:schedule_delete:{sid}')], back('a:schedules')]))
    await c.answer()


@router.callback_query(F.data.startswith('a:schedule_toggle:'))
async def schedule_toggle(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    sid = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        x = await s.get(ScheduledMessage, sid)
        if not x: return await c.answer('Not found', show_alert=True)
        x.enabled = not x.enabled; await audit(s, c.from_user.id, 'schedule_toggle', str(sid), new=str(x.enabled)); await s.commit()
    await c.answer('Updated'); await schedule_detail(c)


@router.callback_query(F.data.startswith('a:schedule_delete:'))
async def schedule_delete(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    sid = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        x = await s.get(ScheduledMessage, sid)
        if not x: return await c.answer('Not found', show_alert=True)
        await s.delete(x); await audit(s, c.from_user.id, 'schedule_delete', str(sid)); await s.commit()
    await c.answer('Deleted'); await schedules(c)


@router.callback_query(F.data == 'a:admins')
async def admins(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    async with SessionLocal() as s: rows = (await s.scalars(select(Admin).order_by(Admin.id))).all()
    rows_kb = [[btn(f'👑 Admin #{x.id} • {"ACTIVE" if x.is_active else "REMOVED"}', f'a:admin:{x.id}')] for x in rows]
    if not rows: rows_kb = [[btn('ℹ️ No extra admins', 'a:noop')]]
    rows_kb.append(back())
    await c.message.edit_text('👑 <b>Admins</b>\n\nHandles and usernames are hidden. Select an admin:', parse_mode='HTML', reply_markup=kb(rows_kb))
    await c.answer()


@router.callback_query(F.data.startswith('a:admin:'))
async def admin_detail(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    aid = int(c.data.split(':')[-1])
    async with SessionLocal() as s: x = await s.get(Admin, aid)
    if not x: return await c.answer('Not found', show_alert=True)
    txt = f'👑 <b>Admin #{x.id}</b>\nStatus: <b>{"ACTIVE" if x.is_active else "REMOVED"}</b>'
    action = btn('🗑 Remove', f'a:admin_remove:{aid}') if x.is_active else btn('✅ Restore', f'a:admin_restore:{aid}')
    await c.message.edit_text(txt, parse_mode='HTML', reply_markup=kb([[action], back('a:admins')]))
    await c.answer()


@router.callback_query(F.data.startswith('a:admin_remove:'))
async def admin_remove(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    if not await is_permanent(c.from_user.id): return await c.answer('Permanent admin only', show_alert=True)
    aid = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        x = await s.get(Admin, aid)
        if not x: return await c.answer('Not found', show_alert=True)
        x.is_active = False; await audit(s, c.from_user.id, 'admin_remove', str(aid)); await s.commit()
    await c.answer('Removed'); await admins(c)


@router.callback_query(F.data.startswith('a:admin_restore:'))
async def admin_restore(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    if not await is_permanent(c.from_user.id): return await c.answer('Permanent admin only', show_alert=True)
    aid = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        x = await s.get(Admin, aid)
        if not x: return await c.answer('Not found', show_alert=True)
        x.is_active = True; await audit(s, c.from_user.id, 'admin_restore', str(aid)); await s.commit()
    await c.answer('Restored'); await admins(c)


@router.callback_query(F.data == 'a:maint')
async def maint(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    async with SessionLocal() as s: st = await s.get(MaintenanceState, 1)
    enabled = bool(st and st.enabled)
    await c.message.edit_text(f'🔧 <b>Maintenance</b>\nStatus: <b>{"ON" if enabled else "OFF"}</b>', parse_mode='HTML', reply_markup=kb([[btn('🔴 Turn OFF', 'a:maint_toggle')] if enabled else [btn('🟢 Turn ON', 'a:maint_toggle')], back()]))
    await c.answer()


@router.callback_query(F.data == 'a:maint_toggle')
async def maint_toggle(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    async with SessionLocal() as s:
        st = await s.get(MaintenanceState, 1)
        if not st: st = MaintenanceState(id=1, enabled=False); s.add(st)
        st.enabled = not st.enabled; st.updated_by = c.from_user.id
        await audit(s, c.from_user.id, 'maintenance_toggle', '1', new=str(st.enabled)); await s.commit()
    await c.answer('Updated'); await maint(c)


@router.callback_query(F.data == 'a:settings')
async def settings_page(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    async with SessionLocal() as s:
        keys = ['support_username', 'daily_report_time', 'daily_report_timezone', 'official_group_id', 'official_channel_id', 'admin_log_group_id']
        vals = [f'• {k}: <code>{await setting(s, k, "—")}</code>' for k in keys]
    await c.message.edit_text('⚙️ <b>Settings</b>\n\n' + '\n'.join(vals), parse_mode='HTML', reply_markup=kb([back()]))
    await c.answer()


@router.callback_query(F.data == 'a:audit')
async def audit_logs(c: CallbackQuery):
    if not await guard(c): return await deny(c)
    async with SessionLocal() as s: rows = (await s.scalars(select(AdminAuditLog).order_by(desc(AdminAuditLog.created_at)).limit(30))).all()
    txt = '📝 <b>Audit Logs</b>\n\n' + ('\n'.join(f'• {x.created_at:%m-%d %H:%M} — {x.action}' for x in rows) if rows else 'No audit logs')
    await c.message.edit_text(txt, parse_mode='HTML', reply_markup=kb([back()]))
    await c.answer()


@router.callback_query(F.data == 'a:noop')
async def noop(c: CallbackQuery):
    await c.answer('Nothing to show', show_alert=True)


@router.callback_query(F.data == 'a:back')
async def back_handler(c: CallbackQuery):
    if await guard(c):
        await c.message.edit_text('👑 <b>Quick OTP Number Admin</b>\n\nSelect an action:', parse_mode='HTML', reply_markup=menu())
        await c.answer()
