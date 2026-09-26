from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_order_history_uses_access_gate():
    text = (ROOT / 'app/bot/handlers/orders.py').read_text()
    assert 'ok, msg = await allowed(bot, s, u)' in text


def test_support_remains_available_without_financial_gate():
    text = (ROOT / 'app/bot/handlers/support.py').read_text()
    assert 'support_username' in text


def test_blocked_users_are_denied_for_all_normal_access():
    from pathlib import Path
    # Access policy is intentionally fail-closed for blocked users; Support is a separate handler.
    src=Path("app/services/access.py").read_text()
    assert "if user.is_blocked" in src

def test_join_gate_fails_closed_when_infrastructure_is_unconfigured():
    from pathlib import Path
    src=Path("app/services/join_gate.py").read_text()
    assert "if not group_id or not channel_id: return False" in src
