from sqlalchemy import select
from app.db.models import User, MaintenanceState
from app.config import settings


async def get_user(session, tg_id):
    return await session.scalar(select(User).where(User.telegram_id == tg_id))


async def allowed(bot, session, user, financial=False):
    """
    Temporary TEST MODE:
    Group/Channel membership is not required, so every normal user can test
    the complete bot flow. This affects all protected user actions, not only
    /start.
    """
    if not user:
        return False, 'Please use /start first.'

    # Permanent admin always has access.
    if user.telegram_id == settings.permanent_admin_id:
        return True, ''

    if user.is_blocked:
        return False, '🚫 Your account is blocked. You can still contact Support.'

    # TEST MODE: deliberately skip check_joined(...).
    # This lets users test Buy OTP, Balance, Add Funds, Orders, etc.
    if financial:
        m = await session.scalar(select(MaintenanceState).where(MaintenanceState.id == 1))
        if m and m.enabled:
            return False, f'🔧 {m.message}'

    return True, ''
