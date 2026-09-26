from aiogram import Router
from .handlers import start,user,payments,orders,support,announcements
router=Router()
for r in (start.router,user.router,payments.router,orders.router,support.router,announcements.router): router.include_router(r)
