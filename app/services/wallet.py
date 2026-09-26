from decimal import Decimal
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Wallet, LedgerTransaction, User

async def change_balance(session: AsyncSession, user_id: int, amount: Decimal, tx_type: str, reference: str|None=None, actor_id: int|None=None):
    result = await session.execute(select(Wallet).where(Wallet.user_id == user_id).with_for_update())
    wallet = result.scalar_one()
    before = Decimal(wallet.balance or 0)
    after = before + amount
    if after < 0:
        raise ValueError("INSUFFICIENT_BALANCE")
    wallet.balance = after
    tx = LedgerTransaction(transaction_id="TX-"+uuid4().hex[:14].upper(), user_id=user_id, type=tx_type, amount=amount, balance_before=before, balance_after=after, reference=reference, actor_id=actor_id)
    session.add(tx)
    return tx, before, after
