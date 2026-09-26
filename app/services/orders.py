from decimal import Decimal
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Country, Order, OrderEvent, User
from app.services.wallet import change_balance

async def create_order(session: AsyncSession, user_id: int, country: Country, selling_price: Decimal) -> Order:
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if not user or user.is_blocked:
        raise ValueError("USER_BLOCKED")
    order = Order(
        order_id="ORD-" + uuid4().hex[:14].upper(), user_id=user_id,
        country_code=country.code, country_name=country.name,
        service_code=country.service_code, status="processing",
        selling_price=selling_price,
    )
    session.add(order)
    await session.flush()
    await change_balance(session, user_id, -selling_price, "otp_purchase", order.order_id)
    user.total_orders = (user.total_orders or 0) + 1
    user.total_spending = (user.total_spending or Decimal(0)) + selling_price
    session.add(OrderEvent(order_id=order.id, event_type="created", data='{"status":"processing"}'))
    return order

async def refund_order(session: AsyncSession, order: Order, actor_id: int | None = None) -> bool:
    locked = await session.scalar(select(Order).where(Order.id == order.id).with_for_update())
    if not locked or locked.refunded:
        return False
    await change_balance(session, locked.user_id, Decimal(locked.selling_price), "refund", locked.order_id, actor_id)
    locked.refunded = True
    locked.status = "refunded"
    session.add(OrderEvent(order_id=locked.id, event_type="refunded", data='{}'))
    return True
