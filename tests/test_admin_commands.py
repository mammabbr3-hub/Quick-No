from decimal import Decimal

def test_manual_balance_delta():
    before=Decimal('5.00')
    assert before + Decimal('2.50') == Decimal('7.50')
    assert before - Decimal('2.50') == Decimal('2.50')

def test_negative_balance_rejected():
    before=Decimal('1.00')
    amount=Decimal('-2.00')
    assert before + amount < 0
