from decimal import Decimal
from app.services.pricing import selling_price

def test_percent(): assert selling_price(Decimal("0.70"),Decimal("20"))==Decimal("0.84")
def test_fixed(): assert selling_price(Decimal("0.70"),fixed=Decimal("0.20"))==Decimal("0.90")
def test_explicit(): assert selling_price(Decimal("0.70"),explicit=Decimal("1.00"))==Decimal("1.00")
