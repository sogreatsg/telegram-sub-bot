from bot.handlers.user_menu import router as user_menu_router
from bot.handlers.payment import router as payment_router
from bot.handlers.admin import router as admin_router
from bot.handlers.channel_events import router as channel_events_router
from bot.handlers.promotion_admin import router as promotion_admin_router
from bot.handlers.promotion_user import router as promotion_user_router

__all__ = [
    "user_menu_router",
    "payment_router",
    "admin_router",
    "channel_events_router",
    "promotion_admin_router",
    "promotion_user_router",
]
