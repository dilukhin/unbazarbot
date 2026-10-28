import asyncio
import json
import shutil
import tempfile
import unittest
import wave
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.filters import Command, CommandObject
from aiogram.types import Chat, Document, Message, User

from tests.test_access import RecordingSession
from tests.tariff_fixtures import catalog, prepare_synthetic_audio
from voicebot.audio_validation import AudioError, magic_format, prepare_audio, _run
from voicebot.config import load_config
from voicebot.db import Database
from voicebot.handlers import AppContext, cmd_trf, media_auto_or_private, transcribe_message_media
from voicebot.media import MediaRef, extract_media, download_media_to_temp
from voicebot.money import MoneySettings, MoneyError
from voicebot.paid_store import BudgetLimits
from voicebot.stt_routerai import RouterAITranscriber, ProviderCallError


def wav(path, seconds=1):
    with wave.open(str(path),'wb') as output:
        output.setnchannels(1);output.setsampwidth(2);output.setframerate(16000)
        output.writeframes(b'\0\0'*(16000*seconds))


@unittest.skipUnless(all(shutil.which(n) for n in ('ffmpeg','ffprobe','prlimit')),'Media tools unavailable')
class RealAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_audio_formats_decode_without_provider_or_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'safe.wav';wav(source)
            for fmt in ('ogg','mp3','wav','flac'):
                with self.subTest(fmt=fmt):
                    try:
                        prepared=await prepare_audio(source,max_seconds=2,max_bytes=1024*1024,audio_format=fmt)
                    except AudioError as exc:
                        self.fail('Synthetic audio resource check: '+exc.code)
                    self.assertLess(prepared.duration,Decimal('1.1'))
                    self.assertGreater(prepared.path.stat().st_size,0)
                    parent=prepared.path.parent
                    again=await prepare_audio(prepared.path,max_seconds=2,max_bytes=1024*1024)
                    self.assertEqual(again.audio_format,'ogg')
                    again.close();prepared.close()
                    self.assertFalse(parent.exists())

    async def test_pdf_playlist_fake_audio_and_real_overlength_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'named.mp3'
            for raw in (b'%PDF-1.7 synthetic',b'#EXTM3U\nhttps://example.invalid/audio',b'ID3 synthetic not mp3'):
                path.write_bytes(raw)
                with self.subTest(raw=raw),self.assertRaises(AudioError):
                    await prepare_audio(path,max_seconds=1,max_bytes=1024*1024)
            wav(path,seconds=2)
            with self.assertRaises(AudioError):await prepare_audio(path,max_seconds=1,max_bytes=1024*1024)


class AudioBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_base64_body_limit_refuses_before_http_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'safe.ogg';path.write_bytes(b'OggS'+b'x'*200)
            with patch('voicebot.stt_routerai.MAX_STT_REQUEST_BYTES',128),patch('voicebot.stt_routerai.httpx.AsyncClient') as client:
                with self.assertRaises(ProviderCallError) as caught:
                    await RouterAITranscriber('TEST_ONLY').transcribe(path,model='synthetic/asr',audio_format='ogg')
                self.assertFalse(caught.exception.sent);client.assert_not_called()

    async def test_missing_tools_fail_closed(self):
        with patch('voicebot.audio_validation.shutil.which',return_value=None):
            with self.assertRaises(AudioError):await _run('ffprobe',[],1024)

    async def test_timeout_kills_child_group_and_drains_readers(self):
        async def pending(*args):await asyncio.Event().wait()
        process=SimpleNamespace(pid=123456,stdout=SimpleNamespace(read=AsyncMock(side_effect=pending)),
                    stderr=SimpleNamespace(read=AsyncMock(side_effect=pending)),wait=AsyncMock(return_value=-9))
        original=asyncio.wait_for
        async def short_wait(awaitable,timeout):return await original(awaitable,0.01)
        with patch('voicebot.audio_validation.asyncio.create_subprocess_exec',AsyncMock(return_value=process)),\
             patch('voicebot.audio_validation.asyncio.wait_for',side_effect=short_wait),\
             patch('voicebot.audio_validation.os.killpg') as kill:
            with self.assertRaises(asyncio.TimeoutError):await _run('ffprobe',[],1024)
        kill.assert_called_once();self.assertEqual(kill.call_args.args[0],123456)

    async def test_download_limit_applies_to_received_bytes_and_removes_partial_file(self):
        created=[]
        import tempfile as module
        original=module.NamedTemporaryFile
        def temporary(*args,**kwargs):
            value=original(*args,**kwargs);created.append(Path(value.name));return value
        async def download(path,destination):destination.write(b'x'*11)
        bot=SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(file_path='safe.ogg')),
                            download_file=AsyncMock(side_effect=download))
        with patch('voicebot.media.tempfile.NamedTemporaryFile',side_effect=temporary):
            with self.assertRaises(ValueError):await download_media_to_temp(bot,MediaRef('f','u','voice',1,1,None,None),max_bytes=10)
        self.assertTrue(created);self.assertTrue(all(not p.exists() for p in created))

    async def test_decoded_duration_cannot_hide_behind_short_input_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'safe.wav';wav(source)
            outputs=[]
            async def encode(binary,args,max_bytes):
                target=Path(args[-1]);outputs.append(target);target.write_bytes(b'OggS synthetic');return b''
            with patch('voicebot.audio_validation.probe',AsyncMock(side_effect=[Decimal(1),Decimal(3)])),\
                 patch('voicebot.audio_validation._run',side_effect=encode):
                with self.assertRaises(AudioError):await prepare_audio(source,max_seconds=2,max_bytes=1024*1024)
            self.assertFalse(outputs[0].parent.exists())


class ExtendedPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Database(Path(self.tmp.name)/'safe.sqlite3',MoneySettings(global_daily_rub='100'))
        await self.db.open();await self.db.upsert_config_admins({1})
        self.media=MediaRef('file','unique','document',None,100,'safe.wav','application/octet-stream')
        cfg=load_config('config.example.yaml')
        self.ctx=AppContext(cfg,self.db,AsyncMock())
        self.ctx.prices=catalog(Path(self.tmp.name)/'prices.json',[m.provider_model for m in cfg.models.values()])
        self.ctx.audio_preparer=prepare_synthetic_audio
        self.bot=Bot('123456:TEST_ONLY',session=RecordingSession())

    async def asyncTearDown(self):
        await self.db.close();await self.bot.session.close();self.tmp.cleanup()

    def message(self, text=None, caption=None):
        return Message(message_id=10,date=datetime.now(timezone.utc),chat=Chat(id=1,type='private'),
            from_user=User(id=1,is_bot=False,first_name='Synthetic'),text=text,caption=caption,
            document=None if text else Document(file_id='file',file_unique_id='unique',file_name='safe.wav',file_size=100,mime_type='application/octet-stream')).as_(self.bot)

    async def test_permission_is_separate_admin_checked_and_grant_does_not_anchor_target(self):
        self.assertFalse(await self.db.extended_audio_allowed(1))
        self.assertFalse(await self.db.set_extended_audio(2,True,999))
        self.assertTrue(await self.db.set_extended_audio(2,True,1))
        with self.assertRaises(MoneyError):await self.db.money_balance('user',2)
        row=await self.db.access_subject('user',2)
        await self.db.change_access('user',2,'revoke',1,row['revision'])
        self.assertFalse(await self.db.extended_audio_allowed(2))

    async def test_owner_chat_message_ttl_used_and_revision_bind_selection(self):
        await self.db.set_extended_audio(1,True,1)
        now=datetime.now(timezone.utc)
        token=await self.db.create_trf_choice(1,1,10,'m',self.media,at=now)
        await self.db.bind_trf_choice([token],20)
        for owner,chat,message in ((2,1,20),(1,2,20),(1,1,21)):
            self.assertIsNone(await self.db.consume_trf_choice(token,owner,chat,message,at=now))
        self.assertIsNone(await self.db.consume_trf_choice(token,1,1,20,at=now+timedelta(minutes=6)))
        self.assertIsNotNone(await self.db.consume_trf_choice(token,1,1,20,at=now))
        self.assertIsNone(await self.db.consume_trf_choice(token,1,1,20,at=now))
        second=await self.db.create_trf_choice(1,1,10,'m',self.media)
        await self.db.bind_trf_choice([second],20)
        await self.db.set_extended_audio(1,False,1);await self.db.set_extended_audio(1,True,1)
        self.assertIsNone(await self.db.consume_trf_choice(second,1,1,20))

    async def test_caption_command_is_explicit_and_plain_audio_never_auto_processed(self):
        message=self.message(caption='/trf')
        parsed=await Command('trf')(message,self.bot)
        self.assertEqual(parsed['command'].command,'trf')
        self.assertIsNone(extract_media(message))
        self.assertEqual(extract_media(message,extended=True).kind,'document')
        await media_auto_or_private(message,self.ctx,self.bot)
        self.ctx.transcriber.transcribe.assert_not_awaited()
        await self.db.set_extended_audio(1,True,1)
        with patch('voicebot.handlers.transcribe_message_media',new_callable=AsyncMock) as run:
            await cmd_trf(message,parsed['command'],self.ctx,self.bot)
            run.assert_awaited_once();self.assertTrue(run.call_args.kwargs['extended'])

    async def test_no_permission_stops_before_download_even_for_admin(self):
        with patch('voicebot.handlers.download_media_to_temp',new_callable=AsyncMock) as download:
            await cmd_trf(self.message(caption='/trf'),CommandObject(prefix='/',command='trf'),self.ctx,self.bot)
        download.assert_not_awaited();self.ctx.transcriber.transcribe.assert_not_awaited()

    async def test_audio_rejection_refunds_money_before_any_paid_call(self):
        await self.db.set_extended_audio(1,True,1)
        source=Path(self.tmp.name)/'synthetic.wav';wav(source)
        self.ctx.audio_preparer=AsyncMock(side_effect=AudioError('synthetic invalid audio'))
        with patch('voicebot.handlers.download_media_to_temp',AsyncMock(return_value=(source,'safe.wav'))):
            await transcribe_message_media(self.message(caption='/trf'),self.message(),self.ctx,self.bot,
                                           self.ctx.config.default_model,extended=True)
        self.ctx.transcriber.transcribe.assert_not_awaited()
        self.assertEqual((await self.db.money_balance('user',1))['held_atoms'],0)
        self.assertFalse(source.exists())

    async def test_revoke_extended_after_reserve_refuses_send(self):
        await self.db.set_extended_audio(1,True,1)
        model=self.ctx.config.model()
        job=await self.db.create_job(1,1,10,'file','unique',model.alias,model.provider_model)
        quote=lambda:self.ctx.prices.estimate_audio(model.provider_model,601)
        reason=await self.db.reserve_financial_call(quote=quote,provider_model=model.provider_model,job_id=job,
                chat_id=1,user_id=1,private=True,file_unique_id='unique',model_alias=model.alias,seconds=3,
                limits=BudgetLimits(),require_extended=True)
        self.assertIsNone(reason)
        await self.db.set_extended_audio(1,False,1)
        with self.assertRaises(MoneyError):await self.db.mark_financial_sending(job,quote,True)
        self.assertTrue(await self.db.release_financial_not_sent(job))
