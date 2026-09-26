from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Deposit, PaymentProof, User
from app.services.wallet import change_balance

async def expire_deposit(session: AsyncSession, deposit_id: int) -> bool:
    d = await session.scalar(select(Deposit).where(Deposit.id == deposit_id).with_for_update())
    if not d or d.status != 'pending':
        return False
    if d.expires_at <= datetime.now(timezone.utc):
        d.status = 'expired'
        return True
    return False

async def approve_deposit(session: AsyncSession, deposit_id: int, admin_id: int) -> Deposit:
    d = await session.scalar(select(Deposit).where(Deposit.id == deposit_id).with_for_update())
    if not d:
        raise ValueError('DEPOSIT_NOT_FOUND')
    if d.status not in ('pending','expired'):
        raise ValueError(f'DEPOSIT_{d.status.upper()}')
    proof = await session.scalar(select(PaymentProof).where(PaymentProof.deposit_id == d.id).order_by(PaymentProof.created_at.desc()))
    if not proof:
        raise ValueError('RECEIPT_REQUIRED')
    duplicate = await session.scalar(select(PaymentProof.id).where(
        PaymentProof.possible_duplicate.is_(True),
        PaymentProof.deposit_id != d.id,
        PaymentProof.file_unique_id == proof.file_unique_id,
    ))
    if duplicate:
        raise ValueError('DUPLICATE_RECEIPT')
    _, _, _ = await change_balance(session, d.user_id, d.usdt_amount, 'deposit', d.reference, admin_id)
    user = await session.scalar(select(User).where(User.id == d.user_id).with_for_update())
    if user:
        user.total_deposits = (user.total_deposits or 0) + d.usdt_amount
    d.status = 'approved'
    d.approved_by = admin_id
    return d

async def reject_deposit(session: AsyncSession, deposit_id: int, admin_id: int, reason: str) -> Deposit:
    d = await session.scalar(select(Deposit).where(Deposit.id == deposit_id).with_for_update())
    if not d:
        raise ValueError('DEPOSIT_NOT_FOUND')
    if d.status not in ('pending','expired'):
        raise ValueError(f'DEPOSIT_{d.status.upper()}')
    d.status = 'rejected'
    d.rejection_reason = reason.strip()[:2000]
    d.approved_by = admin_id
    return d
