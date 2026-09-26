from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


def test_deposit_model_has_historical_payment_snapshot_fields():
    text = Path('app/db/models.py').read_text()
    for field in (
        'payment_method_name_snapshot',
        'payment_country_snapshot',
        'payment_currency_snapshot',
        'payment_details_snapshot',
        'payment_instructions_snapshot',
    ):
        assert field in text


def test_expired_deposit_is_admin_reviewable():
    text = Path('app/services/deposits.py').read_text()
    assert "('pending','expired')" in text


def test_late_receipt_is_allowed_for_expired_request():
    text = Path('app/bot/handlers/payments.py').read_text()
    assert "Deposit.status.in_(['pending','expired'])" in text
    assert 'payment-method/account snapshot' in text
