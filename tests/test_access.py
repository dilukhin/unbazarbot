import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageReplyMarkup, SendMessage
import aiosqlite
from aiogram.types import CallbackQuery, Chat, Message, Update, User, Voice

from voicebot.access_ui import (AccessInput, access_callback, access_command, admin_search,
                                notify_request, router as access_router, show_page,
                                submit_user_request)
from voicebot.config import AppConfig, ModelConfig, load_config
from voicebot.db import Database
from voicebot.handlers import (AppContext, cb_approve_forever, check_access_for_transcription,
                               cmd_revoke, cmd_start, media_auto_or_private, router,
                               transcribe_message_media)
from aiogram.filters import CommandObject


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(message_id=len(self.calls), date=datetime.now(timezone.utc),
                           chat=Chat(id=int(method.chat_id), type='private' if int(method.chat_id) > 0 else 'group'),
                           text=method.text).as_(bot)
        return True

    async def stream_content(self, *args, **kwargs):
        yield b''


def create_legacy_database(path):
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE groups(chat_id INTEGER PRIMARY KEY,title TEXT,username TEXT,status TEXT,
              approval_type TEXT,approved_by_user_id INTEGER,approved_at TEXT,revoked_by_user_id INTEGER,
              revoked_at TEXT,default_model TEXT,auto_enabled INTEGER,remaining_jobs INTEGER,
              created_at TEXT,updated_at TEXT);
            INSERT INTO groups VALUES(-1,'Старая группа',NULL,'approved','forever',1,'date',NULL,NULL,'test',1,0,'date','date');
            CREATE TABLE access_requests(id TEXT PRIMARY KEY,chat_id INTEGER,chat_title TEXT,
              requester_user_id INTEGER,requester_username TEXT,reason TEXT,status TEXT,
              created_at TEXT,updated_at TEXT,decided_by_user_id INTEGER,decided_at TEXT);
            INSERT INTO access_requests VALUES('old',-1,'Старая группа',2,NULL,'old','approved_forever','date','date',1,'date');
            INSERT INTO groups VALUES(-2,'Отозванная группа',NULL,'revoked',NULL,1,'date',1,'date','test',0,0,'date','date');
            INSERT INTO access_requests VALUES('stale',-2,'Отозванная группа',2,NULL,'old','pending','date','date',NULL,NULL);
        ''')


def config():
    return AppConfig('testbot', {1}, True, False, 1, True, 600, 20, 300,
                     'test', 'ru', None, 'https://example.invalid',
                     {'test': ModelConfig('test', 'test-model')})


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'bot.sqlite3')
        await self.db.open()
        await self.db.upsert_config_admins({1})
        await self.db.register_admin_private_chat(1, 1, 'admin', 'Администратор')
        self.session = RecordingSession()
        self.bot = Bot('123456:TEST', session=self.session)
        self.ctx = AppContext(config(), self.db, AsyncMock())
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=123456, chat_id=2, user_id=2))

    async def asyncTearDown(self):
        await self.db.close()
        await self.bot.session.close()
        await self.storage.close()
        self.tmp.cleanup()

    def message(self, user_id=2, chat_id=None, text=None, voice=False):
        chat_id = user_id if chat_id is None else chat_id
        return Message(message_id=10, date=datetime.now(timezone.utc),
                       chat=Chat(id=chat_id, type='private' if chat_id > 0 else 'group', title='Тестовая группа'),
                       from_user=User(id=user_id, is_bot=False, first_name='Алексей', username='alex'),
                       text=text, voice=Voice(file_id='test-file', file_unique_id='unique', duration=5) if voice else None).as_(self.bot)

    def callback(self, data, user_id=1, chat_id=None):
        return CallbackQuery(id='callback', from_user=User(id=user_id, is_bot=False, first_name='Имя'),
                             chat_instance='test', data=data, message=self.message(user_id, chat_id)).as_(self.bot)

    async def request(self, kind='user', target=2, cooldown=0):
        if kind == 'user':
            await self.db.ensure_private_user(target, 'Алексей', 'alex')
        else:
            await self.db.ensure_group(target, 'Совещание', 'meeting')
        return await self.db.create_access_request(kind, target, 'Алексей' if kind == 'user' else 'Совещание',
                                                   2, 'alex', 'Я от Дмитрия', cooldown)

    async def test_user_approval_is_separate_from_admin_and_group_access(self):
        self.assertFalse(await check_access_for_transcription(self.message(voice=True), self.ctx, self.bot))
        req = await self.request()
        result = await self.db.decide_access_request(req.id, 'approve_forever', 1)
        self.assertIsNotNone(result)
        self.assertTrue(await check_access_for_transcription(self.message(voice=True), self.ctx, self.bot))
        self.assertFalse(await self.db.is_admin(2))
        self.assertFalse(await check_access_for_transcription(self.message(chat_id=-10), self.ctx, self.bot))
        row = await self.db.access_subject('user', 2)
        self.assertTrue(await self.db.change_access('user', 2, 'revoke', 1, row['revision']))
        self.assertFalse(await self.db.private_user_allowed(2))
        self.assertFalse(await check_access_for_transcription(self.message(), self.ctx, self.bot))

    async def test_group_approval_does_not_grant_private_access(self):
        req = await self.request('group', -10)
        await self.db.decide_access_request(req.id, 'approve_forever', 1)
        self.assertTrue(await check_access_for_transcription(self.message(chat_id=-10), self.ctx, self.bot))
        self.assertFalse(await self.db.private_user_allowed(2))

    async def test_duplicate_requests_do_not_repeat_notifications(self):
        first, second = await asyncio.gather(self.request(), self.request())
        self.assertEqual(first.id, second.id)
        self.assertEqual(sum([first.is_new, second.is_new]), 1)
        await notify_request(self.bot, self.ctx, first)
        await notify_request(self.bot, self.ctx, second)
        self.assertEqual(len(self.session.calls), 1)
        await self.db.decide_access_request(first.id, 'reject', 1)
        self.assertIsNone(await self.request(cooldown=300))

    async def test_two_administrators_can_only_decide_request_once(self):
        req = await self.request()
        other = Database(self.db.path)
        await other.open()
        try:
            result = await asyncio.gather(self.db.decide_access_request(req.id, 'approve_forever', 1),
                                          other.decide_access_request(req.id, 'reject', 1))
            self.assertEqual(sum(r is not None for r in result), 1)
        finally:
            await other.close()

    async def test_revoke_invalidates_pending_requests_and_old_confirmations(self):
        req = await self.request()
        self.assertTrue(await self.db.change_access('user', 2, 'revoke', 1, 0))
        self.assertIsNone(await self.db.decide_access_request(req.id, 'approve_forever', 1))
        self.assertFalse(await self.db.change_access('user', 2, 'approve_forever', 1, 0))
        self.assertTrue(await self.db.change_access('user', 2, 'approve_forever', 1, 1))
        self.assertFalse(await self.db.change_access('user', 2, 'revoke', 1, 1))
        self.assertTrue(await self.db.private_user_allowed(2))

    async def test_non_admin_and_group_callbacks_cannot_manage_permissions(self):
        req = await self.request()
        await access_callback(self.callback(f'acc:decide:{req.id}:approve_forever', 2), self.ctx, self.bot, self.state)
        await access_callback(self.callback(f'acc:decide:{req.id}:approve_forever', 1, -10), self.ctx, self.bot, self.state)
        self.assertFalse(await self.db.private_user_allowed(2))
        self.assertIsNone(await self.db.decide_access_request(req.id, 'approve_forever', 2))
        self.assertFalse(await self.db.change_access('user', 2, 'approve_forever', 2, 0))

    async def test_admin_membership_is_not_changed_by_access_panel(self):
        await self.db.ensure_private_user(1, 'Администратор', 'admin')
        self.assertFalse(await self.db.change_access('user', 1, 'revoke', 1, 0))
        self.assertTrue(await self.db.is_admin(1))
        self.ctx.config = replace(config(), allow_private_transcription_for_admins=False)
        self.assertFalse(await check_access_for_transcription(self.message(1), self.ctx, self.bot))

    async def test_group_once_persistent_revoke_and_restore(self):
        req = await self.request('group', -10)
        self.assertIsNotNone(await self.db.decide_access_request(req.id, 'approve_once', 1))
        self.assertFalse(await self.db.set_group_auto(-10, True))
        self.assertTrue(await self.db.consume_one_time_job_if_needed(-10))
        self.assertFalse(await self.db.consume_one_time_job_if_needed(-10))
        row = await self.db.access_subject('group', -10)
        self.assertTrue(await self.db.change_access('group', -10, 'approve_forever', 1, row['revision']))
        self.assertTrue(await self.db.set_group_auto(-10, True))
        row = await self.db.access_subject('group', -10)
        self.assertTrue(await self.db.change_access('group', -10, 'revoke', 1, row['revision']))
        row = await self.db.access_subject('group', -10)
        self.assertFalse(row['auto_enabled'])
        self.assertFalse(await self.db.set_group_auto(-10, True))
        self.assertTrue(await self.db.change_access('group', -10, 'approve_forever', 1, row['revision']))
        self.assertFalse((await self.db.access_subject('group', -10))['auto_enabled'])

    async def test_search_pagination_and_parameterized_input(self):
        for user_id in range(2, 23):
            await self.db.ensure_private_user(user_id, f'АлЕКСЕЙ {user_id}', f'person{user_id}')
        rows, total, page = await self.db.access_page('user', 1, 'алексей', 8)
        self.assertEqual((len(rows), total, page), (8, 21, 1))
        rows, total, page = await self.db.access_page('user', 99, '@PERSON22', 8)
        self.assertEqual((rows[0]['user_id'], total, page), (22, 1, 0))
        rows, total, _ = await self.db.access_page('user', 0, "' OR 1=1 --")
        self.assertEqual((rows, total), ([], 0))
        await show_page(self.message(1), self.ctx, 'user', 1, 'алексей')
        self.assertTrue(any('Далее' == b.text for row in self.session.calls[-1].reply_markup.inline_keyboard for b in row))

    async def test_legacy_buttons_cannot_approve_people_or_replay_groups(self):
        user_request = await self.request()
        await cb_approve_forever(self.callback(f'apf:{user_request.id}'), self.ctx, self.bot)
        self.assertFalse(await self.db.private_user_allowed(2))
        group_request = await self.request('group', -10)
        await cb_approve_forever(self.callback(f'apf:{group_request.id}'), self.ctx, self.bot)
        row = await self.db.access_subject('group', -10)
        await self.db.change_access('group', -10, 'revoke', 1, row['revision'])
        await cb_approve_forever(self.callback(f'apf:{group_request.id}'), self.ctx, self.bot)
        self.assertEqual((await self.db.get_group(-10)).status, 'revoked')

    async def test_manual_revoke_requires_confirmation(self):
        req = await self.request('group', -10)
        await self.db.decide_access_request(req.id, 'approve_forever', 1)
        await cmd_revoke(self.message(1), CommandObject(command='revoke', args='-10'), self.ctx)
        self.assertEqual((await self.db.get_group(-10)).status, 'approved')
        data = self.session.calls[-1].reply_markup.inline_keyboard[0][0].callback_data
        await access_callback(self.callback(data), self.ctx, self.bot, self.state)
        self.assertEqual((await self.db.get_group(-10)).status, 'revoked')

    async def test_start_note_submission_and_admin_decision(self):
        await cmd_start(self.message(), self.ctx, self.state)
        await access_callback(self.callback('acc:request', 2), self.ctx, self.bot, self.state)
        self.assertEqual(await self.state.get_state(), AccessInput.note.state)
        await access_callback(self.callback('acc:skip', 2), self.ctx, self.bot, self.state)
        req = (await self.db.list_pending_requests())[0]
        await access_callback(self.callback(f'acc:decide:{req.id}:approve_forever'), self.ctx, self.bot, self.state)
        self.assertTrue(await self.db.private_user_allowed(2))
        self.assertFalse(await self.db.is_admin(2))
        self.assertTrue(any(getattr(call, 'chat_id', None) == 2 and 'Личный доступ разрешён' in (getattr(call, 'text', '') or '')
                            for call in self.session.calls))

    async def test_revocation_during_download_prevents_provider_call(self):
        req = await self.request()
        await self.db.decide_access_request(req.id, 'approve_forever', 1)
        audio = Path(self.tmp.name) / 'test.ogg'
        async def download(*args):
            audio.write_bytes(b'test')
            row = await self.db.access_subject('user', 2)
            await self.db.change_access('user', 2, 'revoke', 1, row['revision'])
            return audio, 'test.ogg'
        with patch('voicebot.handlers.download_media_to_temp', side_effect=download):
            await transcribe_message_media(self.message(), self.message(voice=True), self.ctx, self.bot, 'test')
        self.ctx.transcriber.transcribe.assert_not_awaited()
        self.assertFalse(audio.exists())

    async def test_unauthorized_media_does_not_download_or_transcribe(self):
        with patch('voicebot.handlers.download_media_to_temp', new_callable=AsyncMock) as download:
            await media_auto_or_private(self.message(voice=True), self.ctx, self.bot)
            download.assert_not_awaited()
            self.ctx.transcriber.transcribe.assert_not_awaited()

    async def test_approved_user_can_transcribe_and_cache_cannot_bypass_revoke(self):
        req = await self.request()
        await self.db.decide_access_request(req.id, 'approve_forever', 1)
        audio = Path(self.tmp.name) / 'test.ogg'
        audio.write_bytes(b'test')
        self.ctx.transcriber.transcribe.return_value = SimpleNamespace(text='Тестовая расшифровка.', cost=None, duration_seconds=5)
        with patch('voicebot.handlers.download_media_to_temp', new_callable=AsyncMock, return_value=(audio, 'test.ogg')):
            await media_auto_or_private(self.message(voice=True), self.ctx, self.bot)
        self.ctx.transcriber.transcribe.assert_awaited_once()
        self.assertIsNotNone(await self.db.cached_transcript('unique', 'test'))
        row = await self.db.access_subject('user', 2)
        await self.db.change_access('user', 2, 'revoke', 1, row['revision'])
        self.session.calls.clear()
        await media_auto_or_private(self.message(voice=True), self.ctx, self.bot)
        self.assertFalse(any('Тестовая расшифровка.' in (getattr(call, 'text', '') or '') for call in self.session.calls))

    async def test_recent_rejection_throttles_even_old_requests(self):
        req = await self.request()
        await self.db.db.execute("UPDATE access_requests SET created_at='2020-01-01T00:00:00+00:00', updated_at='2020-01-01T00:00:00+00:00' WHERE id=?", (req.id,))
        await self.db.db.commit()
        await self.db.decide_access_request(req.id, 'reject', 1)
        self.assertIsNone(await self.request(cooldown=300))

    async def test_stale_confirmation_cannot_override_newer_revoke(self):
        req = await self.request()
        await self.db.decide_access_request(req.id, 'approve_forever', 1)
        row = await self.db.access_subject('user', 2)
        await access_callback(self.callback(f'acc:confirm:user:2:{row["revision"]}:revoke'), self.ctx, self.bot, self.state)
        await self.db.change_access('user', 2, 'revoke', 1, row['revision'])
        current = await self.db.access_subject('user', 2)
        await access_callback(self.callback(f'acc:apply:user:2:{row["revision"]}:approve_forever'), self.ctx, self.bot, self.state)
        self.assertEqual((await self.db.access_subject('user', 2))['revision'], current['revision'])
        self.assertFalse(await self.db.private_user_allowed(2))

    async def test_request_skip_is_bound_to_active_input_and_bad_callback_is_safe(self):
        await access_callback(self.callback('acc:skip', 2), self.ctx, self.bot, self.state)
        self.assertEqual(await self.db.list_pending_requests(), [])
        await access_callback(self.callback('acc:apply:user:two:0:revoke'), self.ctx, self.bot, self.state)
        self.assertIn('недействительна', self.session.calls[-1].text)

    async def test_permissions_survive_reopen(self):
        req = await self.request()
        await self.db.decide_access_request(req.id, 'approve_forever', 1)
        await self.db.close()
        await self.db.open()
        self.assertTrue(await self.db.private_user_allowed(2))
        self.assertTrue(await self.db.is_admin(1))
        self.assertEqual((await self.db.get_request(req.id)).subject_type, 'user')

    async def test_safe_migration_of_legacy_group_data(self):
        old_path = Path(self.tmp.name) / 'legacy.sqlite3'
        create_legacy_database(old_path)
        db = Database(old_path)
        await db.open()
        try:
            row = await db.access_subject('group', -1)
            self.assertEqual((row['status'], row['auto_enabled'], row['revision']), ('approved', 1, 0))
            self.assertEqual((await db.get_request('old')).subject_type, 'group')
            self.assertEqual((await db.get_request('stale')).status, 'superseded')
            self.assertFalse(await db.private_user_allowed(2))
            await db.close()
            await db.open()
            self.assertEqual((await db.access_subject('group', -1))['auto_enabled'], 1)
        finally:
            await db.close()

    async def test_interrupted_migration_rolls_back_schema_and_retries_safely(self):
        path = Path(self.tmp.name) / 'interrupted.sqlite3'
        create_legacy_database(path)
        db = Database(path)
        execute = aiosqlite.Connection.execute
        async def interrupt_after_column(conn, sql, *args, **kwargs):
            result = await execute(conn, sql, *args, **kwargs)
            if 'ADD COLUMN subject_type' in sql:
                raise RuntimeError('Прерывание миграции')
            return result
        try:
            with patch.object(aiosqlite.Connection, 'execute', interrupt_after_column):
                with self.assertRaisesRegex(RuntimeError, 'Прерывание миграции'):
                    await db.open()
            await db.close()
            with sqlite3.connect(path) as conn:
                self.assertNotIn('revision', [row[1] for row in conn.execute('PRAGMA table_info(groups)')])
                self.assertNotIn('subject_type', [row[1] for row in conn.execute('PRAGMA table_info(access_requests)')])
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='private_users'").fetchone())
                self.assertEqual(conn.execute("SELECT status FROM access_requests WHERE id='stale'").fetchone()[0], 'pending')
            await db.open()
            self.assertEqual((await db.get_request('stale')).status, 'superseded')
            self.assertEqual((await db.access_subject('group', -1))['status'], 'approved')
            await db.upsert_config_admins({1})
            self.assertIsNone(await db.decide_request('stale', 'approve_forever', 1, 1))
            self.assertEqual((await db.access_subject('group', -2))['status'], 'revoked')
        finally:
            await db.close()

    async def test_saved_decisions_notify_even_if_keyboard_edit_fails(self):
        make_request = self.session.make_request
        async def refuse_edit(bot, method, timeout=None):
            if isinstance(method, EditMessageReplyMarkup):
                raise TelegramBadRequest(method, 'message cannot be edited')
            return await make_request(bot, method, timeout)
        for target_id, action in ((2, 'request'), (-3, 'legacy'), (4, 'revoke'), (5, 'restore')):
            with self.subTest(action=action):
                kind = 'group' if action == 'legacy' else 'user'
                req = await self.request(kind, target_id)
                if action in {'revoke', 'restore'}:
                    await self.db.decide_access_request(req.id, 'approve_forever', 1)
                    row = await self.db.access_subject(kind, target_id)
                    if action == 'restore':
                        await self.db.change_access(kind, target_id, 'revoke', 1, row['revision'])
                        row = await self.db.access_subject(kind, target_id)
                    decision = 'revoke' if action == 'revoke' else 'approve_forever'
                    data = f'acc:apply:{kind}:{target_id}:{row["revision"]}:{decision}'
                else:
                    data = f'apf:{req.id}' if action == 'legacy' else f'acc:decide:{req.id}:approve_forever'
                self.session.calls.clear()
                with patch.object(self.session, 'make_request', refuse_edit):
                    if action == 'legacy':
                        await cb_approve_forever(self.callback(data), self.ctx, self.bot)
                    else:
                        await access_callback(self.callback(data), self.ctx, self.bot, self.state)
                self.assertTrue(any(isinstance(call, SendMessage) and call.chat_id == target_id
                                    for call in self.session.calls))
                self.assertEqual((await self.db.access_subject(kind, target_id))['status'],
                                 'revoked' if action == 'revoke' else 'approved')

    async def test_admin_search_is_private_and_searches_russian(self):
        await self.db.ensure_private_user(2, 'АлЕКСЕЙ', 'alex')
        await self.state.set_data({'kind': 'user'})
        await admin_search(self.message(1, text='алексей'), self.ctx, self.state)
        self.assertIn('Всего записей: 1', self.session.calls[-1].text)
        await access_command(self.message(2), self.ctx, self.state)
        self.assertIn('администратору', self.session.calls[-1].text)


class ConfigTests(unittest.TestCase):
    def test_legacy_open_private_access_is_explicitly_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.yaml'
            path.write_text('access:\n  allow_private_transcription_for_non_admins: true\nstt:\n  default_model: test\n  models:\n    test:\n      provider_model: test\n')
            with self.assertRaisesRegex(ValueError, '/access'):
                load_config(path)


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_router_order_handles_note_and_search_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / 'bot.sqlite3')
            await db.open()
            await db.upsert_config_admins({1})
            bot = Bot('123456:TEST', session=RecordingSession())
            dp = Dispatcher()
            dp.include_router(access_router)
            dp.include_router(router)
            ctx = AppContext(config(), db, AsyncMock())
            user = User(id=2, is_bot=False, first_name='Маша')
            def message(text):
                return Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=2, type='private'), from_user=user, text=text)
            try:
                await dp.feed_update(bot, Update(update_id=1, message=message('/start')), ctx=ctx)
                callback = CallbackQuery(id='c', from_user=user, chat_instance='c', data='acc:request', message=message('Запрос'))
                await dp.feed_update(bot, Update(update_id=2, callback_query=callback), ctx=ctx)
                await dp.feed_update(bot, Update(update_id=3, message=message('Я Маша, от Дмитрия')), ctx=ctx)
                req = (await db.list_pending_requests())[0]
                self.assertEqual((req.subject_type, req.reason), ('user', 'Я Маша, от Дмитрия'))
                ctx.transcriber.transcribe.assert_not_awaited()
            finally:
                await db.close()
                await bot.session.close()
                await dp.storage.close()
