from aiogram import Router,F,Bot
from aiogram.types import Message,CallbackQuery,InlineKeyboardMarkup,InlineKeyboardButton
from sqlalchemy import select,func,desc
from app.db.session import SessionLocal
from app.db.models import User,Wallet,LedgerTransaction,Order
from app.services.access import allowed
router=Router()
@router.message(F.text=='💰 My Balance')
async def balance(m:Message,bot:Bot):
    async with SessionLocal() as s:
      u=await s.scalar(select(User).where(User.telegram_id==m.from_user.id)); ok,msg=await allowed(bot,s,u)
      if not ok:return await m.answer(msg)
      w=await s.scalar(select(Wallet).where(Wallet.user_id==u.id))
    await m.answer(f'💰 <b>My Balance</b>\n\n<b>{w.balance:.2f} USDT</b>',parse_mode='HTML')
@router.message(F.text=='📊 My Statistics')
async def stats(m:Message,bot:Bot):
    async with SessionLocal() as s:
      u=await s.scalar(select(User).where(User.telegram_id==m.from_user.id)); ok,msg=await allowed(bot,s,u)
      if not ok:return await m.answer(msg)
      w=await s.scalar(select(Wallet).where(Wallet.user_id==u.id)); completed=await s.scalar(select(func.count(Order.id)).where(Order.user_id==u.id,Order.status=='completed')) or 0; failed=await s.scalar(select(func.count(Order.id)).where(Order.user_id==u.id,Order.status=='failed')) or 0; refunded=await s.scalar(select(func.count(Order.id)).where(Order.user_id==u.id,Order.status=='refunded')) or 0
      countries=await s.scalar(select(func.count(func.distinct(Order.country_code))).where(Order.user_id==u.id)) or 0
      most=await s.execute(select(Order.country_name,func.count(Order.id).label('n')).where(Order.user_id==u.id).group_by(Order.country_name).order_by(func.count(Order.id).desc()).limit(1)); most=most.first()
    await m.answer(f'📊 <b>Statistics</b>\n\n💰 Balance: <b>{w.balance:.2f} USDT</b>\n💳 Total deposited: <b>{u.total_deposits:.2f}</b>\n💸 Total spent: <b>{u.total_spending:.2f}</b>\n📦 Total orders: <b>{u.total_orders}</b>\n✅ Completed: <b>{completed}</b>\n❌ Failed: <b>{failed}</b>\n↩️ Refunded: <b>{refunded}</b>\n🌍 Countries used: <b>{countries}</b>\n⭐ Most used: <b>{most[0] if most else "—"}</b>\n📅 Joined: <b>{u.created_at:%Y-%m-%d}</b>',parse_mode='HTML')
@router.message(F.text=='🧾 Transactions')
async def transactions(m:Message,bot:Bot): await show_transactions(m,bot,0)
async def show_transactions(m,bot,page):
    async with SessionLocal() as s:
      u=await s.scalar(select(User).where(User.telegram_id==m.from_user.id)); ok,msg=await allowed(bot,s,u)
      if not ok:return await m.answer(msg)
      rows=(await s.scalars(select(LedgerTransaction).where(LedgerTransaction.user_id==u.id).order_by(desc(LedgerTransaction.created_at)).offset(page*10).limit(11))).all()
    has_next=len(rows)>10; rows=rows[:10]
    text='🧾 <b>Transactions</b>\n\n'+('\n'.join(f'• <code>{x.transaction_id}</code> — {x.type} — <b>{x.amount:+.2f}</b> — {x.created_at:%m-%d %H:%M}' for x in rows) if rows else 'No transactions yet.')
    kb=[]
    if page>0:kb.append(InlineKeyboardButton(text='⬅️ Previous',callback_data=f'tx:{page-1}'))
    if has_next:kb.append(InlineKeyboardButton(text='Next ➡️',callback_data=f'tx:{page+1}'))
    await m.answer(text,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(inline_keyboard=[kb]) if kb else None)
@router.callback_query(F.data.startswith('tx:'))
async def tx_page(c:CallbackQuery,bot:Bot):
    page=int(c.data.split(':')[1])
    async with SessionLocal() as s:
      u=await s.scalar(select(User).where(User.telegram_id==c.from_user.id)); ok,msg=await allowed(bot,s,u)
      if not ok:return await c.answer(msg,show_alert=True)
      rows=(await s.scalars(select(LedgerTransaction).where(LedgerTransaction.user_id==u.id).order_by(desc(LedgerTransaction.created_at)).offset(page*10).limit(11))).all()
    has_next=len(rows)>10; rows=rows[:10]
    text='🧾 <b>Transactions</b>\n\n'+('\n'.join(f'• <code>{x.transaction_id}</code> — {x.type} — <b>{x.amount:+.2f}</b> — {x.created_at:%m-%d %H:%M}' for x in rows) if rows else 'No transactions yet.')
    kb=[]
    if page>0:kb.append(InlineKeyboardButton(text='⬅️ Previous',callback_data=f'tx:{page-1}'))
    if has_next:kb.append(InlineKeyboardButton(text='Next ➡️',callback_data=f'tx:{page+1}'))
    await c.message.edit_text(text,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(inline_keyboard=[kb]) if kb else None); await c.answer()
@router.message(F.text=='ℹ️ Help')
async def help_(m:Message,bot:Bot):
    async with SessionLocal() as s:
      u=await s.scalar(select(User).where(User.telegram_id==m.from_user.id)); ok,msg=await allowed(bot,s,u)
      if not ok:return await m.answer(msg)
    await m.answer('ℹ️ <b>Quick OTP Number Help</b>\n\n1. Join the official Group and Channel.\n2. Add at least 3 USDT using an available payment method.\n3. Send your payment receipt as a photo.\n4. After approval, use Buy WhatsApp OTP.\n5. Your order page shows the number and OTP status.\n\nPayment requests expire after 20 minutes, but their payment/account details remain available for admin review. Blocked users can still contact Support.',parse_mode='HTML')
