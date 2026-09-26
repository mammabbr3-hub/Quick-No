from pathlib import Path

def test_payment_handler_imports_payment_proof():
    text = Path('app/bot/handlers/payments.py').read_text()
    assert 'PaymentProof' in text.split('from app.db.models import', 1)[1].split('\n', 1)[0]
