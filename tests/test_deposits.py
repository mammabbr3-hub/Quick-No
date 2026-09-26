from decimal import Decimal
from datetime import datetime, timezone, timedelta
import pytest
from app.db.models import Deposit, PaymentProof, User, Wallet
from app.services.deposits import approve_deposit, reject_deposit

@pytest.mark.asyncio
async def test_reject_requires_pending_and_sets_reason():
    class FakeResult:
        def __init__(self,obj): self.obj=obj
        async def scalar(self): return self.obj
    class FakeSession:
        def __init__(self,d): self.d=d
        async def scalar(self,*args,**kwargs): return self.d
    d=Deposit(id=1,reference='DEP-X',status='pending',expires_at=datetime.now(timezone.utc)+timedelta(minutes=1))
    out=await reject_deposit(FakeSession(d),1,99,'bad receipt')
    assert out.status=='rejected' and out.rejection_reason=='bad receipt'

@pytest.mark.asyncio
async def test_approve_requires_receipt():
    d=Deposit(id=1,reference='DEP-X',status='pending',expires_at=datetime.now(timezone.utc)+timedelta(minutes=1))
    class FakeSession:
        async def scalar(self, stmt, *args, **kwargs):
            return d if 'payment_proofs' not in str(stmt) else None
    with pytest.raises(ValueError, match='RECEIPT_REQUIRED'):
        await approve_deposit(FakeSession(),1,99)
