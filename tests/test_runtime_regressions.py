from pathlib import Path

def test_orders_imports_order_event_for_unknown_grizzly_reconciliation():
    text=Path('app/bot/handlers/orders.py').read_text()
    assert 'OrderEvent' in text

def test_daily_report_does_not_send_telegram_inside_db_transaction():
    text=Path('app/worker.py').read_text()
    start=text.index('async def daily_reports(bot):')
    end=text.index('\nasync def main(bot=None):', start)
    body=text[start:end]
    assert 'await s.commit()' in body
    assert 'await bot.send_message' in body
    assert body.index('await s.commit()') < body.index('await bot.send_message')


def test_admin_approve_callback_uses_central_deposit_service():
    text=Path('app/admin/router.py').read_text()
    start=text.index("async def approve(c:CallbackQuery, bot:Bot):")
    end=text.index("@router.callback_query(F.data.startswith('a:reject:'))", start)
    body=text[start:end]
    assert 'approve_deposit(s,did,c.from_user.id)' in body
    assert 'await change_balance' not in body

def test_admin_reject_callback_is_not_an_approval_path():
    text=Path('app/admin/router.py').read_text()
    start=text.index("async def reject(c:CallbackQuery, bot:Bot):")
    end=text.index("@router.callback_query(F.data=='a:maint')", start)
    body=text[start:end]
    assert "d.status='rejected'" in body
    assert 'change_balance' not in body


def test_worker_fails_fast_without_grizzly_credentials():
    text=Path('app/main.py').read_text()
    assert "GRIZZLY_API_KEY is required for the worker service" in text
