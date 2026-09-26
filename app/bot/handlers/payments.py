from aiogram import Router, F
from aiogram.types import Message
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import select
from app.db.session import SessionLocal
from app.db.models import User, PaymentMethod, Deposit, PaymentProof, MaintenanceState
from app.services.access import allowed
from app.config import MIN_DEPOSIT
router=Router()

@router.message(F.text=="💳 Add Funds")
async def add_funds(m, bot):
    async with SessionLocal() as s:
        u=await s.scalar(select(User).where(User.telegram_id==m.from_user.id))
        ok,msg=await allowed(bot,s,u,financial=True)
        if not ok: return await m.answer(msg)
        methods=(await s.scalars(select(PaymentMethod).where(PaymentMethod.enabled==True).order_by(PaymentMethod.display_order))).all()
        if not methods: return await m.answer("💳 No payment methods are currently available.")
        text="💳 *Add Funds*\n\nMinimum deposit: `3.00 USDT`\n\n"+"\n".join(f"• {x.id}. {x.name} — {x.currency} (1 USDT = {x.exchange_rate})" for x in methods)
        await m.answer(text+"\n\nSend the amount in USDT followed by the payment method ID, e.g. `10 1`.",parse_mode="Markdown")

@router.message(F.text.regexp(r"^\d+(?:\.\d+)?\s+\d+$"))
async def create_deposit(m, bot):
    amount_s, method_s=m.text.split(); amount=Decimal(amount_s); method_id=int(method_s)
    if amount <= 0: return await m.answer("❌ Amount must be positive.")
    if amount < MIN_DEPOSIT: return await m.answer("❌ Minimum deposit is 3 USDT.")
    async with SessionLocal() as s:
        u=await s.scalar(select(User).where(User.telegram_id==m.from_user.id).with_for_update()); ok,msg=await allowed(bot,s,u,financial=True)
        if not ok: return await m.answer(msg)
        method=await s.scalar(select(PaymentMethod).where(PaymentMethod.id==method_id,PaymentMethod.enabled==True))
        if not method: return await m.answer("❌ Payment method is unavailable.")
        existing=await s.scalar(select(Deposit).where(Deposit.user_id==u.id,Deposit.status=='pending').order_by(Deposit.created_at.desc()))
        if existing:
            return await m.answer(f"⏳ You already have a pending payment request: `{existing.reference}`\n\nPlease complete it or wait for it to expire before creating another one.", parse_mode="Markdown")
        if amount < Decimal(method.min_deposit): return await m.answer(f"❌ Minimum for this payment method is {method.min_deposit:.2f} USDT.")
        local=amount*Decimal(method.exchange_rate); ref="DEP-"+uuid4().hex[:12].upper(); now=datetime.now(timezone.utc)
        d=Deposit(reference=ref,user_id=u.id,payment_method_id=method.id,usdt_amount=amount,local_amount=local,frozen_rate=method.exchange_rate,status="pending",expires_at=now+timedelta(minutes=20),payment_method_name_snapshot=method.name,payment_country_snapshot=method.country,payment_currency_snapshot=method.currency,payment_details_snapshot=method.details,payment_instructions_snapshot=method.instructions); s.add(d); await s.commit()
        await m.answer(f"🧾 *Payment Request*\n\nRef: `{ref}`\nAmount: `{amount:.2f} USDT`\nPay: `{local:.2f} {method.currency}`\n\n{method.details}\n\n{method.instructions}\n\n⏳ Expires in 20 minutes.\n\nSend your *payment receipt as a photo/image only* after payment.",parse_mode="Markdown")

@router.message(F.text == '/payment')
async def show_payment_request(m, bot):
    async with SessionLocal() as s:
        u=await s.scalar(select(User).where(User.telegram_id==m.from_user.id))
        if not u: return await m.answer('Please use /start first.')
        d=await s.scalar(select(Deposit).where(Deposit.user_id==u.id, Deposit.status.in_(['pending','expired'])).order_by(Deposit.created_at.desc()))
        if not d: return await m.answer('❌ No active or historical payment request is available.')
        status_text = '⏳ Pending' if d.status == 'pending' else '⌛ Expired — admin can still approve/reject after receipt verification'
        await m.answer(
            f'🧾 *Payment Request*\n\nRef: `{d.reference}`\nAmount: `{d.usdt_amount:.2f} USDT`\nPay: `{d.local_amount:.2f} {d.payment_currency_snapshot}`\nStatus: {status_text}\nExpires: `{d.expires_at:%Y-%m-%d %H:%M UTC}`\n\n{d.payment_details_snapshot}\n\n{d.payment_instructions_snapshot}\n\nSend the payment receipt as a photo/image only.',
            parse_mode='Markdown')

@router.message(F.photo)
async def payment_receipt(m, bot):
    async with SessionLocal() as s:
        u=await s.scalar(select(User).where(User.telegram_id==m.from_user.id))
        ok,msg=await allowed(bot,s,u,financial=True)
        if not ok: return await m.answer(msg)
        d=await s.scalar(select(Deposit).where(Deposit.user_id==u.id,Deposit.status.in_(['pending','expired'])).order_by(Deposit.created_at.desc())) if u else None
        if not d: return await m.answer('❌ No payment request found.')
        if d.status == 'expired':
            # Expiry never removes the frozen payment-method/account snapshot.
            pass
        elif d.expires_at <= datetime.now(timezone.utc):
            d.status='expired'; await s.commit()
            await m.answer('⏳ This payment request has expired, but you may still send the payment receipt so an admin can review it manually.')
        p=m.photo[-1]
        duplicate_proof=await s.scalar(select(PaymentProof).where(PaymentProof.file_unique_id==p.file_unique_id).order_by(PaymentProof.created_at.asc()))
        duplicate=duplicate_proof is not None
        if duplicate_proof:
            duplicate_proof.possible_duplicate=True
        proof=PaymentProof(deposit_id=d.id,user_id=u.id,telegram_file_id=p.file_id,file_unique_id=p.file_unique_id,possible_duplicate=duplicate); s.add(proof); await s.commit()
        if duplicate:
            return await m.answer('⚠️ Receipt received, but it matches a previous receipt. It has been flagged for admin review and will not be auto-approved.')
        if d.status == 'expired':
            return await m.answer(
                f'⌛ Receipt received for expired request `{d.reference}`.\n\n'
                f'Payee details retained:\n{d.payment_details_snapshot}\n\n'
                'An admin can still review and approve/reject this payment manually.',
                parse_mode='Markdown')
        await m.answer('✅ Receipt received. Your payment is now awaiting admin verification.')
