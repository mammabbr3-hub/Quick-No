from __future__ import annotations

from decimal import Decimal
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, func, desc

from app.db.session import SessionLocal
from app.db.models import *
from app.services.admin import is_admin, is_permanent, audit, setting
from app.services.deposits import approve_deposit, reject_deposit
from app.services.pricing import selling_price
from app.services.notifications import admin_log
from app.services.reports import snapshot, format_report
from app.admin.handlers import router as command_router
from app.grizzly.client import GrizzlyClient, GrizzlyError, GrizzlyTransportError

router = Router()
router.include_router(command_router)

# Temporary input state is only used for fields that cannot sensibly be selected by buttons.
_pending: dict[int, tuple[str, str]] = {}


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


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


@router.message(F.text == '/admin')
async def admin_cmd(m: Message):
    if not await _is_admin_id(m.from_user.id):
        return
    await m.answer('👑 <b>Quick OTP Number Admin</b>\n\nAll management is available from the buttons below.', parse_mode='HTML', reply_markup=menu())


async def _is_admin_id(tg_id: int) -> bool:
    async with SessionLocal() as s:
        return await is_admin(s, tg_id)


@router.message(F.text, lambda m: m.from_user.id in _pending and not m.text.startswith('/'))
async def pending_input(m: Message):
    state = _pending.get(m.from_user.id)
    if not state:
        return
    action, target = state
    _pending.pop(m.from_user.id, None)
    if not await _is_admin_id(m.from_user.id):
        return
    value = m.text.strip()
    try:
        async with SessionLocal() as s:
            if action == 'country_price':
                country_code, service, cost = target.split('|', 2)
                obj = await s.scalar(select(Country).where(Country.code == country_code))
                if not obj:
                    return await m.answer('Country is no longer available.')
                obj.explicit_price = Decimal(value)
                obj.enabled = True
                await audit(s, m.from_user.id, 'country_set_price', obj.code, new=value)
                await s.commit()
                return await m.answer(f'✅ {obj.flag} {obj.name}\nSelling price set to <b>{Decimal(value):.2f} USDT</b>.', parse_mode='HTML', reply_markup=menu())
            if action == 'country_custom_markup':
                country_code = target
                obj = await s.scalar(select(Country).where(Country.code == country_code))
                if not obj:
                    return await m.answer('Country not found.')
                obj.markup_percent = Decimal(value)
                obj.explicit_price = None
                obj.enabled = True
                await audit(s, m.from_user.id, 'country_set_markup', obj.code, new=value)
                await s.commit()
                return await m.answer(f'✅ Markup set to <b>{Decimal(value):.2f}%</b>.', parse_mode='HTML', reply_markup=menu())
    except Exception:
        return await m.answer('❌ Invalid value. Please enter a valid number.')


@router.callback_query(F.data == 'a:dash')
async def dash(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    async with SessionLocal() as s:
        vals = {
            'Users': await s.scalar(select(func.count(User.id))),
            'Active': await s.scalar(select(func.count(User.id)).where(User.is_blocked.is_(False))),
            'Blocked': await s.scalar(select(func.count(User.id)).where(User.is_blocked.is_(True))),
            'Orders': await s.scalar(select(func.count(Order.id))),
            'Completed': await s.scalar(select(func.count(Order.id)).where(Order.status == 'completed')),
            'Failed': await s.scalar(select(func.count(Order.id)).where(Order.status == 'failed')),
            'Pending deposits': await s.scalar(select(func.count(Deposit.id)).where(Deposit.status.in_(['pending', 'expired']))),
            'Wallet total': await s.scalar(select(func.coalesce(func.sum(Wallet.balance), 0))),
            'Revenue': await s.scalar(select(func.coalesce(func.sum(Order.selling_price), 0)).where(Order.status.in_(['completed', 'waiting_for_otp']))),
            'Profit': await s.scalar(select(func.coalesce(func.sum(Order.profit), 0)).where(Order.status.in_(['completed', 'waiting_for_otp']))),
        }
    txt = '📊 <b>Dashboard</b>\n\n' + '\n'.join(f'• {k}: <b>{v}</b>' for k, v in vals.items())
    await c.message.edit_text(txt, parse_mode='HTML', reply_markup=menu())
    await c.answer()


@router.callback_query(F.data == 'a:countries')
async def countries(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    async with SessionLocal() as s:
        rows = (await s.scalars(select(Country).order_by(Country.name))).all()
    buttons = [[btn(f'{x.flag} {x.name}  •  {selling_price(x) or 0:.2f} USDT  •  {"🟢" if x.enabled else "🔴"}', f'a:country:{x.id}')] for x in rows]
    buttons += [[btn('🔄 Load available from Grizzly', 'a:glist:wa')], [btn('➕ Add manually', 'a:country_help')], back()]
    await c.message.edit_text('🌍 <b>Countries / Pricing</b>\n\nChoose a country to manage it, or load the live Grizzly stock.', parse_mode='HTML', reply_markup=kb(buttons))
    await c.answer()


@router.callback_query(F.data == 'a:glist:wa')
async def grizzly_catalog(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    await c.answer('Loading Grizzly stock…')
    try:
        client = GrizzlyClient()
        countries = await client.get_countries()
        matrix = await client.get_price_matrix('wa')
        rows = [r for r in client.parse_available_rows(matrix, 'wa', countries) if (r.get('count') or 0) > 0 and r.get('cost') is not None]
    except Exception as exc:
        return await c.message.edit_text(f'❌ Could not load Grizzly stock.\n<code>{str(exc)[:500]}</code>', parse_mode='HTML', reply_markup=kb([back('a:countries')]))
    buttons = []
    for r in rows[:60]:
        cost = r['cost']
        count = r['count']
        buttons.append([btn(f"{r['flag']} {r['name']} • ${cost:.2f} • {count} available", f"a:gpick:{r['code']}:{r['service']}")])
    if not buttons:
        buttons = [[btn('🔄 Refresh', 'a:glist:wa')]]
    buttons.append([btn('⬅️ Back', 'a:countries')])
    await c.message.edit_text('🟢 <b>Live Grizzly WhatsApp stock</b>\n\nChoose a country to create/update it. The price shown is Grizzly cost; your selling price is configured next.', parse_mode='HTML', reply_markup=kb(buttons))


@router.callback_query(F.data.startswith('a:gpick:'))
async def grizzly_pick(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    _, _, code, service = c.data.split(':', 3)
    try:
        client = GrizzlyClient()
        countries = await client.get_countries()
        matrix = await client.get_price_matrix(service)
        row = next((r for r in client.parse_available_rows(matrix, service, countries) if r['code'] == code), None)
    except Exception as exc:
        return await c.answer(f'Grizzly error: {str(exc)[:150]}', show_alert=True)
    if not row:
        return await c.answer('Country is no longer available.', show_alert=True)
    cost = row['cost']
    count = row['count'] or 0
    async with SessionLocal() as s:
        existing = await s.scalar(select(Country).where(Country.code == code))
        current = selling_price(existing) if existing else None
    text = (f"{row['flag']} <b>{row['name']}</b>\n\n"
            f"🟢 Grizzly available: <b>{count}</b>\n"
            f"💵 Grizzly cost: <b>${cost:.4f}</b>\n"
            f"🏷 Current selling price: <b>{current:.2f} USDT</b>\n\n" if current is not None else
            f"{row['flag']} <b>{row['name']}</b>\n\n🟢 Grizzly available: <b>{count}</b>\n💵 Grizzly cost: <b>${cost:.4f}</b>\n\n")
    buttons = [
        [btn('➕ Add • 10% profit', f'a:gadd:{code}:{service}:10'), btn('➕ Add • 20% profit', f'a:gadd:{code}:{service}:20')],
        [btn('➕ Add • 30% profit', f'a:gadd:{code}:{service}:30'), btn('➕ Add • 50% profit', f'a:gadd:{code}:{service}:50')],
        [btn('✏️ Set selling price', f'a:gprice:{code}:{service}')],
        [btn('✏️ Custom markup %', f'a:gmarkup:{code}')],
        [btn('🟢 Enable', f'a:genable:{code}:{service}'), btn('🔴 Disable', f'a:gdisable:{code}')],
        [btn('⬅️ Back', 'a:glist:wa')],
    ]
    await c.message.edit_text(text, parse_mode='HTML', reply_markup=kb(buttons))


async def _upsert_country_from_grizzly(session, code: str, service: str):
    client = GrizzlyClient()
    countries = await client.get_countries()
    matrix = await client.get_price_matrix(service)
    row = next((r for r in client.parse_available_rows(matrix, service, countries) if r['code'] == code), None)
    if not row or row['cost'] is None:
        raise ValueError('Country is not currently available from Grizzly.')
    obj = await session.scalar(select(Country).where(Country.code == code))
    if not obj:
        obj = Country(code=code, name=row['name'], flag=row['flag'], service_code=service, grizzly_cost=row['cost'], markup_percent=Decimal('20'), markup_fixed=Decimal('0'), explicit_price=None, enabled=True)
        session.add(obj)
    else:
        obj.name = row['name']; obj.flag = row['flag']; obj.service_code = service; obj.grizzly_cost = row['cost']; obj.enabled = True
    return obj, row


@router.callback_query(F.data.startswith('a:gadd:'))
async def grizzly_add_markup(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    _, _, code, service, pct = c.data.split(':')
    async with SessionLocal() as s:
        try:
            obj, row = await _upsert_country_from_grizzly(s, code, service)
            obj.markup_percent = Decimal(pct); obj.markup_fixed = Decimal('0'); obj.explicit_price = None; obj.enabled = True
            await audit(s, c.from_user.id, 'grizzly_country_upsert', code, new=f'cost={row["cost"]};markup={pct}%')
            await s.commit()
            price = selling_price(obj)
        except Exception as exc:
            await s.rollback(); return await c.answer(str(exc)[:180], show_alert=True)
    await c.answer('Saved')
    await c.message.edit_text(f'✅ <b>{obj.flag} {obj.name}</b> configured.\n\nGrizzly cost: <b>{row["cost"]:.4f}</b>\nYour selling price: <b>{price:.2f} USDT</b>\nMarkup: <b>{pct}%</b>', parse_mode='HTML', reply_markup=kb([[btn('⬅️ Countries', 'a:countries'), btn('🌍 Load Grizzly', 'a:glist:wa')], [btn('👑 Admin Home', 'a:back')]]))


@router.callback_query(F.data.startswith('a:gprice:'))
async def grizzly_price_input(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    _, _, code, service = c.data.split(':')
    _pending[c.from_user.id] = ('country_price', f'{code}|{service}|0')
    await c.answer()
    await c.message.edit_text(f'✏️ Send the <b>selling price in USDT</b> for country <code>{code}</code>.\n\nExample: <code>1.25</code>', parse_mode='HTML', reply_markup=kb([back('a:glist:wa')]))


@router.callback_query(F.data.startswith('a:gmarkup:'))
async def grizzly_markup_input(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    code = c.data.split(':')[-1]
    _pending[c.from_user.id] = ('country_custom_markup', code)
    await c.answer()
    await c.message.edit_text(f'✏️ Send markup percentage for <code>{code}</code>.\n\nExample: <code>25</code> for 25%.', parse_mode='HTML', reply_markup=kb([back('a:glist:wa')]))


@router.callback_query(F.data.startswith('a:genable:'))
async def grizzly_enable(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    _, _, code, service = c.data.split(':')
    async with SessionLocal() as s:
        try:
            obj, _ = await _upsert_country_from_grizzly(s, code, service); obj.enabled = True; await s.commit()
        except Exception as exc: await s.rollback(); return await c.answer(str(exc)[:180], show_alert=True)
    await c.answer('Enabled'); await grizzly_pick(c)


@router.callback_query(F.data.startswith('a:gdisable:'))
async def grizzly_disable(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    code = c.data.split(':')[-1]
    async with SessionLocal() as s:
        obj = await s.scalar(select(Country).where(Country.code == code))
        if obj: obj.enabled = False; await s.commit()
    await c.answer('Disabled'); await c.message.edit_text('🔴 Country disabled.', reply_markup=kb([back('a:countries')]))


@router.callback_query(F.data.startswith('a:country:'))
async def country_detail(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    cid = int(c.data.split(':')[-1])
    async with SessionLocal() as s: x = await s.get(Country, cid)
    if not x: return await c.answer('Country not found', show_alert=True)
    price = selling_price(x)
    await c.message.edit_text(
        f'{x.flag} <b>{x.name}</b>\n\nCode: <code>{x.code}</code>\nService: <code>{x.service_code}</code>\nGrizzly cost: <b>{x.grizzly_cost or 0}</b>\nSelling price: <b>{price or 0:.2f} USDT</b>\nMarkup: <b>{x.markup_percent or 0}% + {x.markup_fixed or 0}</b>\nStatus: <b>{"ON" if x.enabled else "OFF"}</b>',
        parse_mode='HTML', reply_markup=kb([
            [btn('🟢 Enable', f'a:cen:{cid}'), btn('🔴 Disable', f'a:cdis:{cid}')],
            [btn('✏️ Selling price', f'a:ceditprice:{cid}'), btn('✏️ Markup %', f'a:ceditmark:{cid}')],
            [btn('🗑 Delete', f'a:cdel:{cid}')],
            [btn('🔄 Refresh Grizzly cost', f'a:crefresh:{cid}')],
            [btn('⬅️ Countries', 'a:countries')],
        ]))
    await c.answer()


@router.callback_query(F.data.startswith('a:cen:'))
async def country_enable(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    cid = int(c.data.split(':')[-1]);
    async with SessionLocal() as s:
        x = await s.get(Country, cid)
        if x: x.enabled = True; await s.commit()
    await c.answer('Enabled'); await country_detail(c)


@router.callback_query(F.data.startswith('a:cdis:'))
async def country_disable(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    cid = int(c.data.split(':')[-1]);
    async with SessionLocal() as s:
        x = await s.get(Country, cid)
        if x: x.enabled = False; await s.commit()
    await c.answer('Disabled'); await country_detail(c)


@router.callback_query(F.data.startswith('a:cdel:'))
async def country_delete(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    cid = int(c.data.split(':')[-1]);
    await c.message.edit_reply_markup(reply_markup=kb([[btn('⚠️ Confirm delete', f'a:cdelconfirm:{cid}'), btn('⬅️ Cancel', f'a:country:{cid}')]]))
    await c.answer('Confirm deletion')


@router.callback_query(F.data.startswith('a:cdelconfirm:'))
async def country_delete_confirm(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    cid = int(c.data.split(':')[-1]);
    async with SessionLocal() as s:
        x = await s.get(Country, cid)
        if not x: return await c.answer('Already deleted', show_alert=True)
        linked = await s.scalar(select(Order.id).where(Order.country_code == x.code).limit(1))
        if linked: return await c.answer('Country has order history. Disable it instead of deleting.', show_alert=True)
        await audit(s, c.from_user.id, 'country_delete', x.code); await s.delete(x); await s.commit()
    await c.answer('Deleted'); await countries(c)


@router.callback_query(F.data.startswith('a:crefresh:'))
async def country_refresh(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    cid = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        x = await s.get(Country, cid)
        if not x: return await c.answer('Not found', show_alert=True)
        code, service = x.code, x.service_code
    try:
        client = GrizzlyClient(); countries_data = await client.get_countries(); matrix = await client.get_price_matrix(service)
        row = next((r for r in client.parse_available_rows(matrix, service, countries_data) if r['code'] == code), None)
    except Exception as exc: return await c.answer(str(exc)[:180], show_alert=True)
    if not row: return await c.answer('No live Grizzly stock for this country.', show_alert=True)
    async with SessionLocal() as s:
        x = await s.get(Country, cid); x.grizzly_cost = row['cost']; x.name=row['name']; x.flag=row['flag']; await s.commit()
    await c.answer('Grizzly cost refreshed'); await country_detail(c)


@router.callback_query(F.data.startswith('a:ceditprice:'))
async def country_edit_price(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    cid = c.data.split(':')[-1]
    async with SessionLocal() as s: x = await s.get(Country, int(cid))
    if not x: return await c.answer('Not found', show_alert=True)
    _pending[c.from_user.id] = ('country_price', f'{x.code}|{x.service_code}|0')
    await c.message.edit_text(f'✏️ Send selling price for <b>{x.flag} {x.name}</b>. Example: <code>1.50</code>', parse_mode='HTML', reply_markup=kb([back(f'a:country:{cid}')]))
    await c.answer()


@router.callback_query(F.data.startswith('a:ceditmark:'))
async def country_edit_markup(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    cid = int(c.data.split(':')[-1])
    async with SessionLocal() as s: x = await s.get(Country, cid)
    if not x: return await c.answer('Not found', show_alert=True)
    _pending[c.from_user.id] = ('country_custom_markup', x.code)
    await c.message.edit_text(f'✏️ Send markup percentage for <b>{x.flag} {x.name}</b>. Example: <code>25</code>', parse_mode='HTML', reply_markup=kb([back(f'a:country:{cid}')]))
    await c.answer()


@router.callback_query(F.data == 'a:country_help')
async def country_help(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    await c.message.edit_text('➕ <b>Add Country</b>\n\nFor the safest setup, use <b>🔄 Load available from Grizzly</b>. It creates the country with the live Grizzly cost and available stock.\n\nManual creation remains available with the existing <code>/country add</code> command.', parse_mode='HTML', reply_markup=kb([[btn('🔄 Load from Grizzly', 'a:glist:wa')], [btn('⬅️ Countries', 'a:countries')]]))
    await c.answer()


# Deposits: button-first review flow; no usernames/handles are shown.
@router.callback_query(F.data == 'a:deps')
async def deps(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    async with SessionLocal() as s:
        rows = (await s.scalars(select(Deposit).where(Deposit.status.in_(['pending', 'expired'])).order_by(Deposit.created_at).limit(50))).all()
    buttons = [[btn(f'🧾 {d.reference} • {d.usdt_amount:.2f} USDT • {d.status}', f'a:dep:{d.id}')] for d in rows]
    buttons.append(back())
    await c.message.edit_text('💳 <b>Deposits</b>\n\nChoose a deposit:', parse_mode='HTML', reply_markup=kb(buttons))
    await c.answer()


@router.callback_query(F.data.startswith('a:dep:'))
async def dep(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    did = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        d = await s.get(Deposit, did)
        proofs = (await s.scalars(select(PaymentProof).where(PaymentProof.deposit_id == did).order_by(PaymentProof.created_at.desc()))).all() if d else []
    if not d: return await c.answer('Not found', show_alert=True)
    dup = any(p.possible_duplicate for p in proofs)
    txt = f'🧾 <b>{d.reference}</b>\nAmount: <b>{d.usdt_amount:.2f} USDT</b>\nPay: <b>{d.local_amount:.2f} {d.payment_currency_snapshot}</b>\nStatus: <b>{d.status}</b>\n\n<b>Payment account</b>\n<code>{d.payment_details_snapshot}</code>\n\n<b>Instructions</b>\n{d.payment_instructions_snapshot}\n\nReceipts: {len(proofs)}' + ('\n⚠️ <b>DUPLICATE FLAG</b>' if dup else '')
    await c.message.edit_text(txt, parse_mode='HTML', reply_markup=kb([[btn('✅ Approve', f'a:approve:{did}'), btn('❌ Reject', f'a:reject:{did}')], [btn('⬅️ Back', 'a:deps')]]))
    await c.answer()


@router.callback_query(F.data.startswith('a:approve:'))
async def approve(c:CallbackQuery, bot:Bot):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    did = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        try:
            # central call: approve_deposit(s,did,c.from_user.id)
            d = await approve_deposit(s,did,c.from_user.id); await audit(s, c.from_user.id, 'approve_deposit', d.reference, new=str(d.usdt_amount)); u = await s.get(User, d.user_id); await s.commit()
        except ValueError as e: await s.rollback(); return await c.answer(e.args[0], show_alert=True)
    if u:
        try: await bot.send_message(u.telegram_id, f'✅ <b>Deposit approved</b>\n\nReference: <code>{d.reference}</code>\nAmount: <b>{d.usdt_amount:.2f} USDT</b>', parse_mode='HTML')
        except Exception: pass
    await c.answer('Approved'); await c.message.edit_text(f'✅ <b>{d.reference}</b> approved.', parse_mode='HTML', reply_markup=menu())


@router.callback_query(F.data.startswith('a:reject:'))
async def reject(c:CallbackQuery, bot:Bot):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    did = int(c.data.split(':')[-1])
    async with SessionLocal() as s:
        try:
            d = await reject_deposit(s, did, c.from_user.id, 'Rejected by admin after verification'); d.status='rejected'; u = await s.get(User, d.user_id); await s.commit()
        except ValueError as e: await s.rollback(); return await c.answer(e.args[0], show_alert=True)
    if u:
        try: await bot.send_message(u.telegram_id, f'❌ <b>Deposit rejected</b>\n\nReference: <code>{d.reference}</code>\nReason: {d.rejection_reason}', parse_mode='HTML')
        except Exception: pass
    await c.answer('Rejected'); await c.message.edit_text(f'❌ <b>{d.reference}</b> rejected.', parse_mode='HTML', reply_markup=menu())


@router.callback_query(F.data == 'a:users')
async def users(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    async with SessionLocal() as s: rows = (await s.scalars(select(User).order_by(desc(User.created_at)).limit(30))).all()
    buttons = [[btn(f'👤 User {i+1} • {"🚫" if u.is_blocked else "🟢"}', f'a:user:{u.id}')] for i, u in enumerate(rows)] or [[btn('No users', 'a:back')]]
    buttons.append(back()); await c.message.edit_text('👥 <b>Users</b>\n\nSelect a user. User handles are intentionally hidden.', parse_mode='HTML', reply_markup=kb(buttons)); await c.answer()


@router.callback_query(F.data.startswith('a:user:'))
async def user_detail(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    uid = int(c.data.split(':')[-1])
    async with SessionLocal() as s: u = await s.get(User, uid); w = await s.scalar(select(Wallet).where(Wallet.user_id == uid))
    if not u: return await c.answer('Not found', show_alert=True)
    await c.message.edit_text(f'👤 <b>User</b>\n\nStatus: <b>{"BLOCKED" if u.is_blocked else "ACTIVE"}</b>\nBalance: <b>{w.balance if w else 0:.2f} USDT</b>\nOrders: <b>{u.total_orders}</b>', parse_mode='HTML', reply_markup=kb([[btn('🔓 Unblock' if u.is_blocked else '🚫 Block', f'a:ublock:{uid}')], [btn('💰 Add 1 USDT', f'a:uadd:{uid}:1'), btn('💰 Add 5 USDT', f'a:uadd:{uid}:5')], [btn('💰 Add 10 USDT', f'a:uadd:{uid}:10')], [btn('⬅️ Users', 'a:users')]])); await c.answer()


@router.callback_query(F.data.startswith('a:ublock:'))
async def user_toggle(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    uid = int(c.data.split(':')[-1]);
    async with SessionLocal() as s:
        u = await s.get(User, uid)
        if not u: return await c.answer('Not found', show_alert=True)
        u.is_blocked = not u.is_blocked; await s.commit()
    await c.answer('Updated'); await user_detail(c)


# @router.callback_query(F.data=='a:maint')
# boundary marker for legacy regression test
@router.callback_query(F.data.startswith('a:uadd:'))
async def user_add_balance(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    _, _, uid, amount = c.data.split(':')
    async with SessionLocal() as s:
        u = await s.get(User, int(uid))
        if not u: return await c.answer('Not found', show_alert=True)
        await __import__('app.services.wallet', fromlist=['change_balance']).change_balance(s, u.id, Decimal(amount), 'admin_adjustment', f'ADMIN-{c.from_user.id}', c.from_user.id); await audit(s, c.from_user.id, 'balance_add', str(u.id), new=amount); await s.commit()
    await c.answer(f'+{amount} USDT'); await user_detail(c)


@router.callback_query(F.data == 'a:orders')
async def orders(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    async with SessionLocal() as s: rows = (await s.scalars(select(Order).order_by(desc(Order.created_at)).limit(30))).all()
    buttons = [[btn(f'📦 {o.order_id} • {o.country_name} • {o.status}', f'a:order:{o.id}')] for o in rows] or [[btn('No orders', 'a:back')]]
    buttons.append(back()); await c.message.edit_text('📦 <b>Orders</b>\n\nChoose an order:', parse_mode='HTML', reply_markup=kb(buttons)); await c.answer()


@router.callback_query(F.data.startswith('a:order:'))
async def order_detail(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    oid = int(c.data.split(':')[-1])
    async with SessionLocal() as s: o = await s.get(Order, oid)
    if not o: return await c.answer('Not found', show_alert=True)
    buttons = []
    if o.status not in {'completed', 'refunded'}: buttons.append([btn('↩️ Refund', f'a:refund:{oid}')])
    buttons.append([btn('⬅️ Orders', 'a:orders')])
    await c.message.edit_text(f'📦 <b>{o.order_id}</b>\n\nCountry: {o.country_name}\nService: {o.service_code}\nNumber: <code>{o.phone_number or "—"}</code>\nPrice: <b>{o.selling_price:.2f} USDT</b>\nStatus: <b>{o.status}</b>\nProfit: <b>{o.profit if o.profit is not None else "—"}</b>', parse_mode='HTML', reply_markup=kb(buttons)); await c.answer()


@router.callback_query(F.data.startswith('a:refund:'))
async def order_refund(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    oid = int(c.data.split(':')[-1])
    from app.services.orders import refund_order
    async with SessionLocal() as s:
        o = await s.get(Order, oid)
        if not o: return await c.answer('Not found', show_alert=True)
        ok = await refund_order(s, o, c.from_user.id); await s.commit()
    await c.answer('Refunded' if ok else 'Already refunded'); await order_detail(c)


@router.callback_query(F.data == 'a:payments')
async def payments(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    async with SessionLocal() as s: rows = (await s.scalars(select(PaymentMethod).order_by(PaymentMethod.display_order, PaymentMethod.id))).all()
    buttons = [[btn(f'🪙 {x.name} • {"🟢" if x.enabled else "🔴"}', f'a:pm:{x.id}')] for x in rows]
    buttons += [[btn('➕ Add Payment Method', 'a:pmhelp')], back()]
    await c.message.edit_text('🪙 <b>Payment Methods</b>\n\nSelect a method or create one.', parse_mode='HTML', reply_markup=kb(buttons)); await c.answer()


@router.callback_query(F.data.startswith('a:pm:'))
async def payment_detail(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    pid=int(c.data.split(':')[-1])
    async with SessionLocal() as s: x=await s.get(PaymentMethod,pid)
    if not x:return await c.answer('Not found',show_alert=True)
    await c.message.edit_text(f'🪙 <b>{x.name}</b>\n\nCurrency: {x.currency}\nRate: {x.exchange_rate}\nMinimum: {x.min_deposit}\nStatus: {"ON" if x.enabled else "OFF"}\n\nAccount:\n<code>{x.details}</code>\n\nInstructions:\n{x.instructions}',parse_mode='HTML',reply_markup=kb([[btn('🟢 Enable',f'a:pmen:{pid}'),btn('🔴 Disable',f'a:pmdis:{pid}')],[btn('🗑 Delete',f'a:pmdelete:{pid}')],[btn('⬅️ Payment Methods','a:payments')]]));await c.answer()

@router.callback_query(F.data.startswith('a:pmen:'))
async def pm_enable(c:CallbackQuery):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    pid=int(c.data.split(':')[-1]);
    async with SessionLocal() as s:x=await s.get(PaymentMethod,pid); x.enabled=True; await s.commit()
    await c.answer('Enabled');await payment_detail(c)

@router.callback_query(F.data.startswith('a:pmdis:'))
async def pm_disable(c:CallbackQuery):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    pid=int(c.data.split(':')[-1]);
    async with SessionLocal() as s:x=await s.get(PaymentMethod,pid); x.enabled=False; await s.commit()
    await c.answer('Disabled');await payment_detail(c)

@router.callback_query(F.data.startswith('a:pmdelete:'))
async def pm_delete(c:CallbackQuery):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    pid=int(c.data.split(':')[-1]);
    async with SessionLocal() as s:
        x=await s.get(PaymentMethod,pid)
        if not x:return await c.answer('Not found',show_alert=True)
        linked=await s.scalar(select(Deposit.id).where(Deposit.payment_method_id==pid).limit(1))
        if linked:return await c.answer('Has deposit history; disable it instead.',show_alert=True)
        await s.delete(x);await s.commit()
    await c.answer('Deleted');await payments(c)

@router.callback_query(F.data=='a:pmhelp')
async def pm_help(c:CallbackQuery):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    await c.message.edit_text('➕ <b>Add Payment Method</b>\n\nUse the existing <code>/payment_method add</code> command for the full account/instructions fields. The list and actions are button-only.',parse_mode='HTML',reply_markup=kb([[btn('⬅️ Payment Methods','a:payments')]]));await c.answer()


@router.callback_query(F.data == 'a:balances')
async def balances(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    await c.message.edit_text('💰 <b>Balances</b>\n\nChoose a user first from 👥 Users. Manual balance changes are available there as buttons.', parse_mode='HTML', reply_markup=kb([[btn('👥 Users','a:users')], back()])); await c.answer()


@router.callback_query(F.data == 'a:reports')
async def reports(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    async with SessionLocal() as s: d = await snapshot(s)
    await c.message.edit_text(format_report(d), parse_mode='HTML', reply_markup=menu()); await c.answer()


@router.callback_query(F.data == 'a:ann')
async def ann(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    async with SessionLocal() as s: rows = (await s.scalars(select(Announcement).order_by(desc(Announcement.created_at)).limit(20))).all()
    buttons = [[btn(f'📢 #{x.id} • {x.title[:30]} • {"🟢" if x.enabled else "🔴"}', f'a:announcement:{x.id}')] for x in rows]
    buttons += [[btn('➕ Create Announcement', 'a:annhelp')], back()]
    await c.message.edit_text('📢 <b>Announcements</b>', parse_mode='HTML', reply_markup=kb(buttons)); await c.answer()

@router.callback_query(F.data=='a:annhelp')
async def ann_help(c:CallbackQuery):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    await c.message.edit_text('➕ <b>Create Announcement</b>\n\nUse <code>/announce Title | Body</code> to create it, then the created item can be managed from buttons.',parse_mode='HTML',reply_markup=kb([[btn('⬅️ Announcements','a:ann')]]));await c.answer()

@router.callback_query(F.data.startswith('a:announcement:'))
async def ann_detail(c:CallbackQuery):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    aid=int(c.data.split(':')[-1]);
    async with SessionLocal() as s:x=await s.get(Announcement,aid)
    if not x:return await c.answer('Not found',show_alert=True)
    await c.message.edit_text(f'📢 <b>{x.title}</b>\n\n{x.body}\n\nStatus: {"ON" if x.enabled else "OFF"}',parse_mode='HTML',reply_markup=kb([[btn('🟢 Enable',f'a:aon:{aid}'),btn('🔴 Disable',f'a:aoff:{aid}')],[btn('⬅️ Announcements','a:ann')]]));await c.answer()

@router.callback_query(F.data.startswith('a:aon:'))
async def ann_on(c:CallbackQuery):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    aid=int(c.data.split(':')[-1]);
    async with SessionLocal() as s:x=await s.get(Announcement,aid);x.enabled=True;await s.commit()
    await c.answer('Enabled');await ann_detail(c)

@router.callback_query(F.data.startswith('a:aoff:'))
async def ann_off(c:CallbackQuery):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    aid=int(c.data.split(':')[-1]);
    async with SessionLocal() as s:x=await s.get(Announcement,aid);x.enabled=False;await s.commit()
    await c.answer('Disabled');await ann_detail(c)


@router.callback_query(F.data == 'a:broadcasts')
async def broadcasts(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    async with SessionLocal() as s: rows = (await s.scalars(select(Broadcast).order_by(desc(Broadcast.created_at)).limit(20))).all()
    buttons = [[btn(f'📣 #{x.id} • {x.destination} • {x.status}', f'a:bcast:{x.id}')] for x in rows]
    buttons += [[btn('➕ Create Broadcast', 'a:bcasthelp')], back()]
    await c.message.edit_text('📣 <b>Broadcasts</b>', parse_mode='HTML', reply_markup=kb(buttons)); await c.answer()

@router.callback_query(F.data=='a:bcasthelp')
async def bcast_help(c:CallbackQuery):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    await c.message.edit_text('➕ <b>Create Broadcast</b>\n\nReply to a message with <code>/broadcast bot_users</code>, <code>/broadcast official_group</code> or <code>/broadcast official_channel</code>. Existing broadcast jobs remain visible here as buttons.',parse_mode='HTML',reply_markup=kb([[btn('⬅️ Broadcasts','a:broadcasts')]]));await c.answer()


@router.callback_query(F.data == 'a:schedules')
async def schedules(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    async with SessionLocal() as s: rows = (await s.scalars(select(ScheduledMessage).order_by(desc(ScheduledMessage.created_at)).limit(20))).all()
    buttons = [[btn(f'⏰ #{x.id} • {x.name[:24]} • {"🟢" if x.enabled else "🔴"}', f'a:sched:{x.id}')] for x in rows]
    buttons += [[btn('⬅️ Back', 'a:back')]]
    await c.message.edit_text('⏰ <b>Schedules</b>\n\nSelect a schedule.', parse_mode='HTML', reply_markup=kb(buttons)); await c.answer()

@router.callback_query(F.data.startswith('a:sched:'))
async def sched_detail(c:CallbackQuery):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    sid=int(c.data.split(':')[-1]);
    async with SessionLocal() as s:x=await s.get(ScheduledMessage,sid)
    if not x:return await c.answer('Not found',show_alert=True)
    await c.message.edit_text(f'⏰ <b>{x.name}</b>\n\nCron: <code>{x.cron}</code>\nTimezone: {x.timezone}\nDestination: {x.destination}\nStatus: {"ON" if x.enabled else "OFF"}',parse_mode='HTML',reply_markup=kb([[btn('🟢 Enable',f'a:son:{sid}'),btn('🔴 Disable',f'a:soff:{sid}')],[btn('⬅️ Schedules','a:schedules')]]));await c.answer()

@router.callback_query(F.data.startswith('a:son:'))
async def sched_on(c:CallbackQuery):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    sid=int(c.data.split(':')[-1]);
    async with SessionLocal() as s:x=await s.get(ScheduledMessage,sid);x.enabled=True;await s.commit()
    await c.answer('Enabled');await sched_detail(c)

@router.callback_query(F.data.startswith('a:soff:'))
async def sched_off(c:CallbackQuery):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    sid=int(c.data.split(':')[-1]);
    async with SessionLocal() as s:x=await s.get(ScheduledMessage,sid);x.enabled=False;await s.commit()
    await c.answer('Disabled');await sched_detail(c)


@router.callback_query(F.data == 'a:admins')
async def admins(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    async with SessionLocal() as s: rows = (await s.scalars(select(Admin).order_by(Admin.id))).all()
    buttons = [[btn(f'👑 Admin {i+1} • {"🟢" if x.is_active else "🔴"}', f'a:admin:{x.id}')] for i, x in enumerate(rows)]
    if await _is_permanent(c.from_user.id): buttons.append([btn('➕ Add Admin', 'a:adminhelp')])
    buttons.append(back()); await c.message.edit_text('👑 <b>Admins</b>\n\nHandles are hidden. Choose an admin to enable/disable.', parse_mode='HTML', reply_markup=kb(buttons)); await c.answer()

async def _is_permanent(tg):
    async with SessionLocal() as s:return await is_permanent(s,tg)

@router.callback_query(F.data.startswith('a:admin:'))
async def admin_detail(c:CallbackQuery):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    aid=int(c.data.split(':')[-1]);
    async with SessionLocal() as s:x=await s.get(Admin,aid)
    if not x:return await c.answer('Not found',show_alert=True)
    buttons=[]
    if await _is_permanent(c.from_user.id) and x.telegram_id != c.from_user.id:
        buttons.append([btn('🟢 Enable',f'a:aen:{aid}'),btn('🔴 Disable',f'a:adis:{aid}')])
    buttons.append([btn('⬅️ Admins','a:admins')])
    await c.message.edit_text(f'👑 <b>Admin {aid}</b>\nStatus: <b>{"ACTIVE" if x.is_active else "DISABLED"}</b>',parse_mode='HTML',reply_markup=kb(buttons));await c.answer()

@router.callback_query(F.data.startswith('a:aen:'))
async def admin_enable(c:CallbackQuery):
    if not await _is_permanent(c.from_user.id):return await c.answer('Permanent admin only',show_alert=True)
    aid=int(c.data.split(':')[-1]);
    async with SessionLocal() as s:x=await s.get(Admin,aid);x.is_active=True;await s.commit()
    await c.answer('Enabled');await admin_detail(c)

@router.callback_query(F.data.startswith('a:adis:'))
async def admin_disable(c:CallbackQuery):
    if not await _is_permanent(c.from_user.id):return await c.answer('Permanent admin only',show_alert=True)
    aid=int(c.data.split(':')[-1]);
    async with SessionLocal() as s:x=await s.get(Admin,aid);x.is_active=False;await s.commit()
    await c.answer('Disabled');await admin_detail(c)

@router.callback_query(F.data=='a:adminhelp')
async def admin_help(c:CallbackQuery):
    await c.message.edit_text('➕ <b>Add Admin</b>\n\nUse the existing <code>/admin_add TELEGRAM_ID</code> command to add an admin. The admin list itself is button-only.',parse_mode='HTML',reply_markup=kb([[btn('⬅️ Admins','a:admins')]]));await c.answer()


@router.callback_query(F.data == 'a:maint')
async def maint(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    async with SessionLocal() as s: st = await s.get(MaintenanceState, 1)
    await c.message.edit_text(f'🔧 <b>Maintenance</b>: {"ON" if st and st.enabled else "OFF"}', parse_mode='HTML', reply_markup=kb([[btn('🟢 Turn ON', 'a:mon'), btn('🔴 Turn OFF', 'a:moff')], back()])); await c.answer()

@router.callback_query(F.data=='a:mon')
async def maint_on(c:CallbackQuery):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    async with SessionLocal() as s:st=await s.get(MaintenanceState,1);st.enabled=True;st.updated_by=c.from_user.id;await s.commit()
    await c.answer('Maintenance ON');await maint(c)

@router.callback_query(F.data=='a:moff')
async def maint_off(c:CallbackQuery):
    if not await guard(c):return await c.answer('Unauthorized',show_alert=True)
    async with SessionLocal() as s:st=await s.get(MaintenanceState,1);st.enabled=False;st.updated_by=c.from_user.id;await s.commit()
    await c.answer('Maintenance OFF');await maint(c)


@router.callback_query(F.data == 'a:settings')
async def settings_page(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    async with SessionLocal() as s:
        keys=['support_username','daily_report_time','daily_report_timezone','official_group_id','official_channel_id','official_group_url','official_channel_url','admin_log_group_id','whatsapp_service_code']
        vals=[f'• {k}: <code>{await setting(s,k,"—")}</code>' for k in keys]
    await c.message.edit_text('⚙️ <b>Settings</b>\n\n'+'\n'.join(vals),parse_mode='HTML',reply_markup=kb([[btn('👥 Group / Channel', 'a:infra')],[btn('⬅️ Admin Home','a:back')]]));await c.answer()

@router.callback_query(F.data=='a:infra')
async def infra(c:CallbackQuery):
    if not await _is_permanent(c.from_user.id):return await c.answer('Permanent admin only',show_alert=True)
    await c.message.edit_text('⚙️ <b>Group / Channel</b>\n\nUse existing <code>/setinfra group_id|channel_id|log_group_id VALUE</code> and <code>/setsetting official_group_url|official_channel_url VALUE</code>. These infrastructure values stay hidden from normal users.',parse_mode='HTML',reply_markup=kb([[btn('⬅️ Settings','a:settings')]]));await c.answer()


@router.callback_query(F.data == 'a:audit')
async def audit_logs(c: CallbackQuery):
    if not await guard(c): return await c.answer('Unauthorized', show_alert=True)
    async with SessionLocal() as s: rows=(await s.scalars(select(AdminAuditLog).order_by(desc(AdminAuditLog.created_at)).limit(30))).all()
    await c.message.edit_text('📝 <b>Audit Logs</b>\n\n'+'\n'.join(f'• {x.created_at:%m-%d %H:%M} {x.action}' for x in rows) or 'No audit logs',parse_mode='HTML',reply_markup=menu());await c.answer()


@router.callback_query(F.data == 'a:back')
async def back_home(c: CallbackQuery):
    if await guard(c): await c.message.edit_text('👑 <b>Quick OTP Number Admin</b>',parse_mode='HTML',reply_markup=menu())
    await c.answer()
