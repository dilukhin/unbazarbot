"""Establish personal anchors for explicit actions, never passive group voice."""
from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message

COMMANDS={'start','help','helphelp','balance','status','model','tr','tr_raw','trf',
          'auto_on','auto_off','access','requests','groups','revoke','about','system','cancel'}


class ExplicitMoneyInteraction(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = event.from_user
        explicit = isinstance(event, CallbackQuery)
        if isinstance(event, Message):
            text = event.text or event.caption or ''
            explicit = event.chat.type == ChatType.PRIVATE
            if not explicit and text.startswith('/'):
                command=text.split(maxsplit=1)[0][1:]
                name,separator,target=command.partition('@')
                if separator:
                    configured=getattr(getattr(data['ctx'],'config',None),'bot_username','').lstrip('@').casefold()
                    explicit=bool(configured and target.casefold()==configured)
                else:
                    explicit=name.casefold() in COMMANDS
            if event.sender_chat is not None:
                explicit = False
        if explicit and user and not user.is_bot:
            # Use receipt time, not an untrusted/backlogged Telegram event timestamp.
            await data['ctx'].db.touch_money_user(user.id)
        return await handler(event, data)
