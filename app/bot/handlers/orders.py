from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram import Bot
from sqlalchemy import select
from decimal import Decimal
from app.db.session import SessionLocal
from app.db.models import User, Country, Order, Wallet, MaintenanceState, OrderEvent
from app.services.pricing import selling_price
from app.services.orders import create_order, refund_order
from app.services.access import allowed
from app.grizzly.client import GrizzlyClient, GrizzlyTransportError

router = Router()

@router.message(F.text == '📱 Buy WhatsApp OTP')
async def buy(m: Message):
    async with SessionLocal() as s:
        u = await s.scalar(select(User).where(User.telegram_id == m.from_user.id))
        if not u:
            return
        if u.is_blocked:
            return await m.answer('🚫 Your account is blocked. You may still contact Support.')
        maint = await s.scalar(select(MaintenanceState).where(MaintenanceState.id == 1))
        if maint and maint.enabled:
            return await m.answer(f'🔧 {maint.message}')
        countries = (await s.scalars(select(Country).where(Country.enabled.is_(True), Country.service_code != '').order_by(Country.name))).all()
        countries = [c for c in countries if selling_price(c) is not None]
    if not countries:
        return await m.answer('🌍 No WhatsApp countries are currently available.')
    rows = [[InlineKeyboardButton(text=f'{c.flag} {c.name} — {selling_price(c):.2f} USDT', callback_data=f'country:{c.id}')] for c in countries[:50]]
    await m.answer('📱 <b>WhatsApp OTP</b>\n\nChoose a country:', parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@router.callback_query(F.data.startswith('country:'))
async def country(c: CallbackQuery, bot: Bot):
    cid = int(c.data.split(':', 1)[1])
    async with SessionLocal() as s:
        u = await s.scalar(select(User).where(User.telegram_id == c.from_user.id))
        ok, msg = await allowed(bot, s, u, financial=True)
        if not ok:
            return await c.answer(msg, show_alert=True)
        row = await s.scalar(select(Country).where(Country.id == cid, Country.enabled.is_(True)))
        if not row:
            return await c.answer('Country unavailable', show_alert=True)
        price = selling_price(row)
    await c.message.edit_text(
        f'{row.flag} <b>{row.name}</b>\n\nService: WhatsApp\nPrice: <b>{price:.2f} USDT</b>\n\nConfirm purchase?',
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ Buy Number', callback_data=f'buy:{cid}'), InlineKeyboardButton(text='⬅️ Back', callback_data='buy_back')]])
    )
    await c.answer()

@router.callback_query(F.data == 'buy_back')
async def buy_back(c: CallbackQuery, bot: Bot):
    async with SessionLocal() as s:
        u = await s.scalar(select(User).where(User.telegram_id == c.from_user.id))
        ok, msg = await allowed(bot, s, u, financial=True)
        if not ok:
            return await c.answer(msg, show_alert=True)
    await c.message.edit_text('📱 Tap <b>Buy WhatsApp OTP</b> from the main menu to choose a country.', parse_mode='HTML')
    await c.answer()

@router.callback_query(F.data.startswith('buy:'))
async def confirm_buy(c: CallbackQuery, bot: Bot):
    cid = int(c.data.split(':', 1)[1])
    await c.answer('Processing…')
    client = GrizzlyClient()
    # Reserve funds and create the order inside a DB transaction before calling Grizzly.
    # This closes the concurrent double-spend window. If Grizzly fails, refund atomically.
    async with SessionLocal() as s:
        u = await s.scalar(select(User).where(User.telegram_id == c.from_user.id).with_for_update())
        ok, msg = await allowed(bot, s, u, financial=True)
        if not ok:
            return await c.message.answer(msg)
        row = await s.scalar(select(Country).where(Country.id == cid, Country.enabled.is_(True)))
        maint = await s.scalar(select(MaintenanceState).where(MaintenanceState.id == 1))
        if not u or not row:
            return await c.message.answer('Unavailable.')
        if u.is_blocked:
            return await c.message.answer('🚫 Your account is blocked.')
        if maint and maint.enabled:
            return await c.message.answer(f'🔧 {maint.message}')
        price = selling_price(row)
        if price is None:
            return await c.message.answer('This country is currently unavailable.')
        wallet = await s.scalar(select(Wallet).where(Wallet.user_id == u.id).with_for_update())
        if not wallet or Decimal(wallet.balance) < price:
            return await c.message.answer('💰 Insufficient balance.')
        order = await create_order(s, u.id, row, price)
        await s.commit()
        order_id = order.id
        public_id = order.order_id
        service_code = row.service_code
        country_code = row.code
        country_name = row.name
        country_flag = row.flag
        configured_cost = Decimal(row.grizzly_cost) if row.grizzly_cost is not None else None

    # External API call occurs after the reservation commit. No second user can spend the same funds.
    try:
        result = await client.get_number(service_code, country_code, max_price=configured_cost)
    except GrizzlyTransportError:
        # A timeout/network failure does not prove the external request failed.
        # Never refund or blindly retry because Grizzly may have allocated a number.
        async with SessionLocal() as s:
            order = await s.scalar(select(Order).where(Order.id == order_id).with_for_update())
            if order and order.status == 'processing':
                order.status = 'manual_reconciliation'
                s.add(OrderEvent(order_id=order.id, event_type='manual_reconciliation', data='{"reason":"grizzly_transport_unknown"}'))
                await s.commit()
        return await c.message.answer('⚠️ We could not confirm the Grizzly response. Your order was placed into safe manual reconciliation; your balance was not automatically refunded to avoid a possible duplicate allocation. Please contact Support with your order ID.')
    except Exception:
        result = {'status': 'error', 'error': 'API_ERROR'}

    async with SessionLocal() as s:
        order = await s.scalar(select(Order).where(Order.id == order_id).with_for_update())
        if not order:
            return await c.message.answer('Order reconciliation error. Please contact Support.')
        if result.get('status') != 'ok':
            await refund_order(s, order)
            await s.commit()
            return await c.message.answer('❌ No number is currently available. Your funds were refunded.')
        order.activation_id = result['activation_id']
        order.phone_number = result['phone_number']
        actual_cost = result.get('activation_cost')
        if actual_cost is None:
            actual_cost = configured_cost
        order.raw_cost = actual_cost
        order.profit = (price - actual_cost) if actual_cost is not None else None
        order.status = 'waiting_for_otp'
        await s.commit()

    await c.message.edit_text(
        f'📱 <b>Number received</b>\n\nOrder: <code>{public_id}</code>\nCountry: {country_flag} {country_name}\nNumber: <code>{result["phone_number"]}</code>\nPrice: <b>{price:.2f} USDT</b>\n\n⏳ Waiting for OTP…',
        parse_mode='HTML'
    )

@router.message(F.text == '📦 My Orders')
async def orders(m: Message, bot: Bot):
    async with SessionLocal() as s:
        u = await s.scalar(select(User).where(User.telegram_id == m.from_user.id))
        ok, msg = await allowed(bot, s, u)
        if not ok:
            return await m.answer(msg)
        rows = (await s.scalars(select(Order).where(Order.user_id == u.id).order_by(Order.created_at.desc()).limit(10))).all()
    if not rows:
        return await m.answer('📦 You have no orders yet.')
    kb=[[InlineKeyboardButton(text=f'📦 {o.order_id}',callback_data=f'order:{o.id}')] for o in rows]
    await m.answer('📦 <b>Recent orders</b>\n\n' + '\n'.join(f'• <code>{o.order_id}</code> — {o.country_name} — {o.status} — {o.selling_price:.2f} USDT' for o in rows), parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith('order:'))
async def order_detail(c: CallbackQuery, bot: Bot):
    oid=int(c.data.split(':')[1])
    async with SessionLocal() as s:
      u=await s.scalar(select(User).where(User.telegram_id==c.from_user.id)); ok,msg=await allowed(bot,s,u)
      if not ok:return await c.answer(msg,show_alert=True)
      o=await s.scalar(select(Order).where(Order.id==oid,Order.user_id==u.id))
      events=(await s.scalars(select(OrderEvent).where(OrderEvent.order_id==oid).order_by(OrderEvent.created_at.desc()).limit(10))).all() if o else []
    if not o:return await c.answer('Order not found.',show_alert=True)
    txt=f'📦 <b>Order {o.order_id}</b>\n\nCountry: {o.country_name}\nService: WhatsApp\nNumber: <code>{o.phone_number or "—"}</code>\nPrice: <b>{o.selling_price:.2f} USDT</b>\nStatus: <b>{o.status}</b>\nOTP: <code>{o.otp_code or "Waiting"}</code>\nCreated: {o.created_at:%Y-%m-%d %H:%M UTC}\n\n<b>Events</b>\n' + ('\n'.join(f'• {e.event_type} — {e.created_at:%m-%d %H:%M}' for e in events) if events else 'No events')
    await c.message.edit_text(txt,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='⬅️ My Orders',callback_data='orders_back')]])); await c.answer()

@router.callback_query(F.data=='orders_back')
async def orders_back(c:CallbackQuery,bot:Bot):
    await c.message.edit_text('📦 Use <b>My Orders</b> from the main menu to refresh your order list.',parse_mode='HTML'); await c.answer()
