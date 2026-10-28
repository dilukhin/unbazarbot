"""Establish personal anchors for explicit actions, never passive group voice."""
from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message


class ExplicitMoneyInteraction(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = event.from_user
        explicit = isinstance(event, CallbackQuery)
        if isinstance(event, Message):
            text = event.text or event.caption or ''
            explicit = event.chat.type == ChatType.PRIVATE or text.startswith('/')
            if event.sender_chat is not None:
                explicit = False
        if explicit and user and not user.is_bot:
            # Use receipt time, not an untrusted/backlogged Telegram event timestamp.
            await data['ctx'].db.touch_money_user(user.id)
        return await handler(event, data)
