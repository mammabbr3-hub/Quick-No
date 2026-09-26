from __future__ import annotations
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import ScheduledMessage


def due(schedule: ScheduledMessage, now: datetime) -> bool:
    if not schedule.enabled:
        return False
    tz = ZoneInfo(schedule.timezone or "UTC")
    local_now = now.astimezone(tz).replace(second=0, microsecond=0)
    previous = croniter(schedule.cron, local_now + timedelta(minutes=1)).get_prev(datetime)
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=tz)
    if previous > local_now:
        return False
    if schedule.last_run_at is None:
        return previous == local_now
    return previous.astimezone(timezone.utc) > schedule.last_run_at


async def claim_due(session: AsyncSession, now: datetime | None = None):
    """Claim due schedules using database row locks.

    The caller commits the claim before doing Telegram I/O. The persistent
    ScheduledMessageRun table records the execution result separately, so a
    failed delivery remains auditable instead of disappearing silently.
    """
    now = now or datetime.now(timezone.utc)
    rows = (await session.scalars(
        select(ScheduledMessage)
        .where(ScheduledMessage.enabled.is_(True))
        .with_for_update(skip_locked=True)
    )).all()
    claimed=[]
    for row in rows:
        if due(row, now):
            row.last_run_at=now
            claimed.append(row)
    return claimed
