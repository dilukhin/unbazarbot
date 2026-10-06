from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

from voicebot import diagnostics
from voicebot.handlers import cmd_about,cmd_system


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def message(self,user=1,private=True):
        return SimpleNamespace(from_user=SimpleNamespace(id=user),chat=SimpleNamespace(type='private' if private else 'group'),answer=AsyncMock())

    async def test_about_does_not_disclose_system_or_secrets(self):
        message=self.message()
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'SECRET','ROUTERAI_API_KEY':'OTHER_SECRET'}),patch('voicebot.diagnostics.revision',return_value='abc1234'):
            await cmd_about(message)
        text=message.answer.call_args.args[0]
        self.assertIn('0.2.0',text)
        self.assertIn('abc1234',text)
        self.assertNotIn('SECRET',text)
        self.assertNotIn('Каталог',text)
        self.assertNotIn('Сервер',text)

    async def test_system_is_private_admin_only(self):
        ctx=SimpleNamespace(db=SimpleNamespace(is_admin=AsyncMock(side_effect=lambda user:user==1)))
        for user,private in ((2,True),(1,False)):
            with patch('voicebot.handlers.system_text') as text:
                await cmd_system(self.message(user,private),ctx)
                text.assert_not_called()
        with patch('voicebot.handlers.system_text',return_value='Сервер: тест'):
            message=self.message();await cmd_system(message,ctx)
            self.assertIn('Сервер',message.answer.call_args.args[0])

    def test_system_allowlist_never_dumps_environment(self):
        ctx=SimpleNamespace(db=SimpleNamespace(path=Path('/private/bot.sqlite3')))
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'TOKEN_SECRET','ROUTERAI_API_KEY':'KEY_SECRET','EXTRA_SECRET':'OTHER_SECRET','INVOCATION_ID':'PRIVATE_INVOCATION'}):
            text=diagnostics.system_text(ctx)
        for value in ('TOKEN_SECRET','KEY_SECRET','OTHER_SECRET','PRIVATE_INVOCATION'):
            self.assertNotIn(value,text)
        self.assertIn('systemd',text)
        self.assertIn('/private/bot.sqlite3',text)

    def test_archive_revision_and_unknown_revision_are_distinguished(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(diagnostics,'APP_ROOT',Path(tmp)):
            diagnostics.revision.cache_clear()
            self.assertEqual(diagnostics.revision(),'не определена')
            (Path(tmp)/'REVISION').write_text('a'*40)
            diagnostics.revision.cache_clear()
            self.assertEqual(diagnostics.revision(),'a'*12)
        diagnostics.revision.cache_clear()
