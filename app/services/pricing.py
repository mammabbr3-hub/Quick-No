from decimal import Decimal, ROUND_HALF_UP

def selling_price(country_or_cost, percent=Decimal(0), fixed=Decimal(0), explicit=None):
    # Supports both the production Country model and the pure pricing-test signature.
    if hasattr(country_or_cost, 'grizzly_cost'):
        country=country_or_cost; cost=country.grizzly_cost
        if cost is None:return None
        percent=country.markup_percent or 0; fixed=country.markup_fixed or 0; explicit=country.explicit_price
    else: cost=country_or_cost
    cost=Decimal(cost)
    value=Decimal(explicit) if explicit is not None else cost+(cost*Decimal(percent)/Decimal(100))+Decimal(fixed)
    return value.quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)
