import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from voicebot.config import load_config
from voicebot.db import Database
from voicebot.handlers import AppContext, cmd_stats, cmd_transcribe, media_auto_or_private, transcribe_message_media, validate_media
from voicebot.media import MediaRef
from voicebot.paid_store import BudgetLimits


class DatabaseCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'db.sqlite3'
        self.db = Database(self.path)
        await self.db.open()
        await self.db.upsert_config_admins({1})
        self.limits = BudgetLimits(100,100,120,60,30,20)
        self.at = datetime(2026,10,6,12,tzinfo=timezone.utc)

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def group(self, once=False):
        await self.db.ensure_group(-10,'Тест',None,'m')
        req = await self.db.create_access_request('group',-10,'Тест',1,None,'Тест',0)
        await self.db.decide_access_request(req.id,'approve_once' if once else 'approve_forever',1)

    async def reserve(self, key='file', *, private=True, user=1, chat=1, seconds=10, at=None, limits=None, auto=False):
        job = await self.db.create_job(chat,user,1,key,key,'m','provider')
        reason = await self.db.reserve_paid_call(job_id=job,chat_id=chat,user_id=user,private=private,
            file_unique_id=key,model_alias='m',seconds=seconds,limits=limits or self.limits,
            require_auto=auto,at=at or self.at)
        return job,reason


class BudgetTests(DatabaseCase):
    async def test_concurrent_same_file_across_connections_has_one_owner(self):
        other=Database(self.path);await other.open()
        async def reserve(db,n):
            job=await db.create_job(1,1,n,'f','f','m','provider')
            return await db.reserve_paid_call(job_id=job,chat_id=1,user_id=1,private=True,
                file_unique_id='f',model_alias='m',seconds=10,limits=self.limits,at=self.at)
        try:
            results=await asyncio.gather(reserve(self.db,1),reserve(other,2))
            self.assertEqual(results.count(None),1)
        finally:
            await other.close()

    async def test_once_is_spent_before_provider_and_cannot_be_used_twice(self):
        await self.group(once=True)
        results=await asyncio.gather(self.reserve('a',private=False,chat=-10),self.reserve('b',private=False,chat=-10))
        self.assertEqual(sum(reason is None for _,reason in results),1)
        self.assertEqual((await self.db.get_group(-10)).remaining_jobs,0)

    async def test_revoked_access_and_disabled_auto_do_not_reserve(self):
        await self.group()
        self.assertIsNotNone((await self.reserve(private=False,chat=-10,auto=True))[1])
        self.assertTrue(await self.db.set_group_auto(-10,True))
        row=await self.db.access_subject('group',-10)
        await self.db.change_access('group',-10,'revoke',1,row['revision'])
        self.assertIsNotNone((await self.reserve(private=False,chat=-10))[1])

    async def test_private_denied_user_and_disabled_admin_do_not_reserve(self):
        self.assertIsNotNone((await self.reserve(user=2))[1])
        job=await self.db.create_job(1,1,1,'f','f','m','p')
        reason=await self.db.reserve_paid_call(job_id=job,chat_id=1,user_id=1,private=True,
            file_unique_id='f',model_alias='m',seconds=1,limits=self.limits,allow_private_admins=False)
        self.assertIsNotNone(reason)

    async def test_removed_config_administrator_cannot_manage_or_pay(self):
        await self.db.register_admin_private_chat(1,1,'test','Тест')
        await self.db.upsert_config_admins(set())
        self.assertFalse(await self.db.is_admin(1))
        self.assertIsNotNone((await self.reserve())[1])
        await self.db.upsert_config_admins({1})
        self.assertTrue(await self.db.is_admin(1))
        row=await (await self.db.db.execute('SELECT private_chat_id FROM admins WHERE user_id=1')).fetchone()
        self.assertEqual(row[0],1)

    async def test_chat_rate_limit_and_minute_reset(self):
        limits=replace(self.limits,requests_per_minute_chat=1)
        self.assertIsNone((await self.reserve('a',limits=limits))[1])
        self.assertIn('минутный',(await self.reserve('b',limits=limits))[1])
        self.assertIsNone((await self.reserve('c',limits=limits,at=self.at+timedelta(seconds=61)))[1])

    async def test_user_rate_spans_chats(self):
        await self.group()
        limits=replace(self.limits,requests_per_minute_user=1)
        self.assertIsNone((await self.reserve('a',limits=limits))[1])
        self.assertIn('пользователя',(await self.reserve('b',private=False,chat=-10,limits=limits))[1])

    async def test_daily_caps_and_utc_reset(self):
        for name in ('daily_minutes_chat','daily_minutes_user','daily_minutes_total'):
            with self.subTest(name=name):
                # Отдельная дата исключает влияние остальных подпроверок.
                self.at+=timedelta(days=2)
                limits=replace(self.limits,**{name:1})
                prefix=name+str(self.at)
                self.assertIsNone((await self.reserve(prefix+'a',seconds=60,limits=limits))[1])
                self.assertIn('суточный',(await self.reserve(prefix+'b',seconds=1,limits=limits))[1])
                self.assertIsNone((await self.reserve(prefix+'c',seconds=60,limits=limits,at=self.at+timedelta(days=1)))[1])

    async def test_inflight_limit_and_errors_do_not_refund_or_retry(self):
        limits=replace(self.limits,max_inflight=1,daily_minutes_total=1)
        job,reason=await self.reserve('a',seconds=60,limits=limits)
        self.assertIsNone(reason)
        self.assertIn('занят',(await self.reserve('b',limits=limits))[1])
        await self.db.finish_paid_call(job,'error')
        self.assertIn('исходом',(await self.reserve('a',limits=limits))[1])
        self.assertIn('суточный',(await self.reserve('b',limits=limits))[1])

    async def test_recovery_keeps_unknown_payment_blocked(self):
        _,reason=await self.reserve()
        self.assertIsNone(reason)
        await self.db.close();await self.db.open();await self.db.recover_paid_calls()
        self.assertIn('исходом',(await self.reserve())[1])

    async def test_unknown_legacy_job_is_imported_only_once(self):
        await self.db.db.execute("DELETE FROM paid_meta")
        await self.db.db.commit()
        await self.db.create_job(1,1,1,'legacy','legacy','m','p')
        await self.db.close();await self.db.open()
        self.assertIn('исходом',(await self.reserve('legacy'))[1])
        job=await self.db.create_job(1,1,1,'downloadfailed','downloadfailed','m','p')
        await self.db.fail_job(job,'DownloadError')
        await self.db.close();await self.db.open()
        self.assertIsNone((await self.reserve('downloadfailed'))[1])

    async def test_statistics_distinguish_known_and_unknown_cost(self):
        job,_=await self.reserve('a')
        await self.db.finish_paid_call(job,'done',0)
        await self.reserve('b')
        rows=await self.db.usage_today(self.at)
        self.assertEqual((rows[0]['calls'],rows[0]['seconds'],rows[0]['known_cost'],rows[0]['unknown_cost']),(2,20,0,1))


class PipelineTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config=load_config('config.example.yaml')
        self.ctx=AppContext(self.config,self.db,AsyncMock())
        self.bot=AsyncMock()
        self.ctx.transcriber.transcribe.return_value=SimpleNamespace(text='Текст.',cost=0,duration_seconds=3)

    def message(self, user=1, chat=1, key='unique'):
        voice=SimpleNamespace(file_id=key,file_unique_id=key,duration=3,file_size=100,mime_type='audio/ogg')
        return SimpleNamespace(chat=SimpleNamespace(id=chat,type='private' if chat>0 else 'group',title='Тест',username=None),
            from_user=SimpleNamespace(id=user),voice=voice,audio=None,document=None,message_id=1,
            reply_to_message=None,answer=AsyncMock())

    async def download(self, *args):
        path=Path(self.tmp.name)/'audio.ogg';path.write_bytes(b'audio');return path,'voice.ogg'

    async def test_cache_hit_never_downloads_or_pays_again(self):
        message=self.message()
        with patch('voicebot.handlers.download_media_to_temp',side_effect=self.download) as download:
            await transcribe_message_media(message,message,self.ctx,self.bot,self.config.default_model)
            await transcribe_message_media(message,message,self.ctx,self.bot,self.config.default_model)
        self.assertEqual(download.call_count,1)
        self.ctx.transcriber.transcribe.assert_awaited_once()

    async def test_parallel_pipeline_calls_only_pay_once(self):
        release=asyncio.Event();started=asyncio.Event()
        async def transcribe(*args,**kwargs):
            started.set();await release.wait()
            return SimpleNamespace(text='Текст.',cost=0,duration_seconds=3)
        self.ctx.transcriber.transcribe.side_effect=transcribe
        async def download(*args):
            path=Path(self.tmp.name)/('audio'+str(id(asyncio.current_task()))+'.ogg')
            path.write_bytes(b'audio');return path,'voice.ogg'
        message=self.message()
        with patch('voicebot.handlers.download_media_to_temp',side_effect=download):
            first=asyncio.create_task(transcribe_message_media(message,message,self.ctx,self.bot,self.config.default_model))
            await asyncio.wait_for(started.wait(),5)
            await transcribe_message_media(message,message,self.ctx,self.bot,self.config.default_model)
            release.set();await first
        self.ctx.transcriber.transcribe.assert_awaited_once()

    async def test_provider_error_is_private_and_not_retried(self):
        self.ctx.transcriber.transcribe.side_effect=RuntimeError('SECRET_PAYLOAD')
        message=self.message()
        with patch('voicebot.handlers.download_media_to_temp',side_effect=self.download):
            await transcribe_message_media(message,message,self.ctx,self.bot,self.config.default_model)
            await transcribe_message_media(message,message,self.ctx,self.bot,self.config.default_model)
        self.ctx.transcriber.transcribe.assert_awaited_once()
        self.assertNotIn('SECRET_PAYLOAD',str(message.answer.call_args_list))

    async def test_cancellation_does_not_allow_a_second_paid_call(self):
        started=asyncio.Event()
        async def transcribe(*args,**kwargs):
            started.set();await asyncio.Event().wait()
        self.ctx.transcriber.transcribe.side_effect=transcribe
        message=self.message()
        with patch('voicebot.handlers.download_media_to_temp',side_effect=self.download):
            task=asyncio.create_task(transcribe_message_media(message,message,self.ctx,self.bot,self.config.default_model))
            await asyncio.wait_for(started.wait(),5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await transcribe_message_media(message,message,self.ctx,self.bot,self.config.default_model)
        self.ctx.transcriber.transcribe.assert_awaited_once()

    async def test_delivery_failure_keeps_successful_transcript_in_cache(self):
        message=self.message();attempts=0
        async def answer(*args,**kwargs):
            nonlocal attempts
            attempts+=1
            if attempts==2:
                raise RuntimeError('Delivery failed')
        message.answer.side_effect=answer
        with patch('voicebot.handlers.download_media_to_temp',side_effect=self.download):
            await transcribe_message_media(message,message,self.ctx,self.bot,self.config.default_model)
            await transcribe_message_media(message,message,self.ctx,self.bot,self.config.default_model)
        self.ctx.transcriber.transcribe.assert_awaited_once()
        self.assertIsNotNone(await self.db.cached_transcript('unique',self.config.default_model))

    async def test_limits_apply_before_download_and_provider(self):
        for change in ({'duration':601},{'file_size':21*1024*1024}):
            message=self.message();message.voice.__dict__.update(change)
            with patch('voicebot.handlers.download_media_to_temp',new_callable=AsyncMock) as download:
                await transcribe_message_media(message,message,self.ctx,self.bot,self.config.default_model)
                download.assert_not_awaited()
            self.ctx.transcriber.transcribe.assert_not_awaited()

    async def test_manual_reply_and_auto_gate(self):
        message=self.message();command=self.message();command.voice=None;command.reply_to_message=message
        with patch('voicebot.handlers.download_media_to_temp',side_effect=self.download):
            await cmd_transcribe(command,None,self.ctx,self.bot)
        self.ctx.transcriber.transcribe.assert_awaited_once()
        self.ctx.transcriber.transcribe.reset_mock()
        await media_auto_or_private(self.message(chat=-10),self.ctx,self.bot)
        self.ctx.transcriber.transcribe.assert_not_awaited()

    async def test_statistics_are_admin_private_only(self):
        for message in (self.message(user=2),self.message(chat=-10)):
            with patch.object(self.db,'usage_today',new_callable=AsyncMock) as stats:
                await cmd_stats(message,self.ctx)
                stats.assert_not_awaited()
