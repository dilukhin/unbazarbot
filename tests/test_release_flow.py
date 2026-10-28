"""End-to-end synthetic media/HTTP/accounting; no real service requests."""
import base64
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import wave

import httpx
from aiogram import Bot
from aiogram.types import Chat, Document, Message, User, Voice

from scripts.media_doctor import check_media
from tests.test_access import RecordingSession
from tests.tariff_fixtures import catalog
from voicebot.audio_validation import prepare_audio
from voicebot.config import load_config
from voicebot.db import Database
from voicebot.handlers import AppContext, media_auto_or_private, transcribe_message_media
from voicebot.money import MoneySettings, to_atoms
from voicebot.stt_routerai import RouterAITranscriber


@unittest.skipUnless(all(shutil.which(name) for name in ('ffmpeg','ffprobe','prlimit')), 'Media tools unavailable')
class ReleaseFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.db=Database(self.root/'safe.sqlite3',MoneySettings(group_monthly_bazariki='0',global_daily_rub='10'))
        await self.db.open();await self.db.upsert_config_admins({1})
        await self.db.ensure_group(-10,'Synthetic')
        req=await self.db.create_access_request('group',-10,'Synthetic',1,None,'synthetic',0)
        await self.db.decide_access_request(req.id,'approve_forever',1)
        await self.db.set_group_auto(-10,True)
        self.config=load_config('config.example.yaml')
        self.ctx=AppContext(self.config,self.db,RouterAITranscriber('TEST_ONLY'))
        self.ctx.prices=catalog(self.root/'prices.json',[m.provider_model for m in self.config.models.values()])
        self.bot=Bot('123456:TEST_ONLY',session=RecordingSession())
        self.downloads=[];self.requests=[]

    async def asyncTearDown(self):
        await self.db.close();await self.bot.session.close();self.tmp.cleanup()

    def message(self, *, extended=False):
        return Message(message_id=10,date=datetime.now(timezone.utc),chat=Chat(id=-10,type='group'),
            from_user=User(id=1,is_bot=False,first_name='Synthetic'),
            voice=None if extended else Voice(file_id='synthetic',file_unique_id='safe-unique',duration=1),
            document=Document(file_id='synthetic',file_unique_id='safe-unique',file_size=32044,
                file_name='synthetic.wav',mime_type='audio/wav') if extended else None).as_(self.bot)

    async def download(self,*args,**kwargs):
        source=self.root/('download-'+str(len(self.downloads))+'.wav');self.downloads.append(source)
        with wave.open(str(source),'wb') as output:
            output.setnchannels(1);output.setsampwidth(2);output.setframerate(16000)
            output.writeframes(b'\0\0'*16000)
        return source,'synthetic.wav'

    def provider(self,request):
        self.requests.append(request)
        model=self.config.model().provider_model
        if request.method=='POST':
            self.assertEqual(request.url.path,'/api/v1/audio/transcriptions')
            body=json.loads(request.content)
            self.assertEqual(body['model'],model)
            audio=base64.b64decode(body['input_audio']['data'],validate=True)
            self.assertTrue(audio.startswith(b'OggS'));self.assertEqual(body['input_audio']['format'],'ogg')
            return httpx.Response(200,content=b'{"text":"artificial acceptance phrase","usage":{"cost":0.00012345678901,"seconds":1}}',
                headers={'X-Generation-Id':'gen_synthetic'})
        self.assertEqual(request.method,'GET');self.assertEqual(request.url.path,'/api/v1/generation')
        self.assertEqual(request.url.params['id'],'gen_synthetic')
        return httpx.Response(200,json={'data':{'id':'gen_synthetic','model':model,'total_cost':'0.00012345678901'}})

    async def test_zero_group_silent_manual_personal_exact_charge_restart_and_free_cache(self):
        original=httpx.AsyncClient
        with patch('voicebot.handlers.download_media_to_temp',side_effect=self.download) as download,\
             patch('httpx.AsyncClient',side_effect=lambda **kwargs:original(transport=httpx.MockTransport(self.provider),**kwargs)):
            message=self.message()
            await media_auto_or_private(message,self.ctx,self.bot)
            self.assertEqual(self.bot.session.calls,[]);download.assert_not_called();self.assertEqual(self.requests,[])
            self.assertTrue((await self.db.get_group(-10)).auto_enabled)
            await transcribe_message_media(message,message,self.ctx,self.bot,self.config.default_model)
            self.assertEqual(len(self.requests),2);self.assertEqual(len(self.downloads),1)
            row=dict(await (await self.db.db.execute('SELECT * FROM financial_calls')).fetchone())
            self.assertEqual((row['kind'],row['subject_id'],row['state']),('user',1,'settled'))
            self.assertEqual(row['final_rub'],'0.00012345678901');self.assertEqual(row['generation_id'],'gen_synthetic')
            self.assertGreater(Decimal(row['bound_rub']),Decimal(row['final_rub']))
            balance=await self.db.money_balance('user',1)
            self.assertEqual(balance['free_atoms'],to_atoms('1000')-to_atoms('0.00012345678901',rub=True))
            self.assertEqual(balance['held_atoms'],0)
            self.assertEqual((await self.db.money_balance('group',-10))['free_atoms'],0)
            self.assertFalse(self.downloads[0].exists())
            await self.db.close();await self.db.open()
            await self.db.recover_paid_calls();await self.db.recover_financial_calls()
            await transcribe_message_media(message,message,self.ctx,self.bot,self.config.default_model)
            self.assertEqual(len(self.requests),2);self.assertEqual(len(self.downloads),1)
            self.assertEqual(await self.db.money_balance('user',1),balance)
            self.assertIn('из кеша',self.bot.session.calls[-1].text)

    async def test_trf_revoke_during_real_audio_preparation_refunds_without_provider(self):
        await self.db.set_extended_audio(1,True,1)
        prepared_paths=[]
        async def prepare_then_revoke(*args,**kwargs):
            result=await prepare_audio(*args,**kwargs);prepared_paths.append(result.path)
            await self.db.set_extended_audio(1,False,1)
            return result
        self.ctx.audio_preparer=prepare_then_revoke
        with patch('voicebot.handlers.download_media_to_temp',side_effect=self.download),\
             patch('voicebot.stt_routerai.httpx.AsyncClient') as provider:
            message=self.message(extended=True)
            await transcribe_message_media(message,message,self.ctx,self.bot,self.config.default_model,extended=True)
        provider.assert_not_called()
        row=await (await self.db.db.execute('SELECT state FROM financial_calls')).fetchone()
        self.assertEqual(row['state'],'not-sent')
        balance=await self.db.money_balance('user',1)
        self.assertEqual((balance['free_atoms'],balance['held_atoms']),(to_atoms('1000'),0))
        self.assertTrue(prepared_paths);self.assertTrue(all(not p.exists() for p in prepared_paths+self.downloads))

    async def test_standalone_operator_media_preflight_uses_same_resource_profile(self):
        with patch('httpx.AsyncClient') as network:
            self.assertEqual(await check_media(),{'media':'ok','formats':['ogg','mp3','wav','flac'],'network':False})
        network.assert_not_called()


if __name__=='__main__':unittest.main()
