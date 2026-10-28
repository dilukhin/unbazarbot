from tests.tariff_fixtures import catalog as synthetic_catalog
import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal
from voicebot.money import MoneySettings
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from voicebot.config import load_config
from voicebot.formatter import FormatterConfig, FormatResult, RouterAIFormatter, same_words
from voicebot.handlers import AppContext, transcribe_message_media
from test_budget import DatabaseCase


class FormatterTests(unittest.IsolatedAsyncioTestCase):
    def config(self,**kwargs):
        return replace(FormatterConfig(True,'p',{'p':'provider/model'}),**kwargs)

    async def call(self,response,*,config=None,status=200):
        original=httpx.AsyncClient
        requests=[]
        def handle(request):
            requests.append(request);return httpx.Response(status,json=response)
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handle),**kwargs)
        with patch('voicebot.formatter.httpx.AsyncClient',side_effect=client):
            result=await RouterAIFormatter('TEST_ONLY',config or self.config(),'https://example.invalid').format('привет мир это тест')
        return result,requests

    async def test_success_adds_punctuation_without_changing_words(self):
        result,requests=await self.call({'choices':[{'message':{'content':'Привет, мир!\n\nЭто тест.'}}],'usage':{'cost':0.1}})
        self.assertTrue(result.accepted)
        self.assertEqual(result.cost,Decimal('0.1'))
        self.assertEqual(len(requests),1)
        self.assertTrue(requests[0].url.path.endswith('/chat/completions'))

    async def test_added_removed_reordered_words_fall_back_but_account_cost(self):
        for text in ('Привет, прекрасный мир! Это тест.','Привет, мир!','Мир, привет! Это тест.'):
            result,_=await self.call({'text':text,'usage':{'cost':0.2}})
            self.assertFalse(result.accepted)
            self.assertEqual(result.text,'привет мир это тест')
            self.assertEqual(result.cost,Decimal('0.2'))

    async def test_disabled_and_large_input_do_not_call_provider(self):
        for cfg in (self.config(enabled=False),self.config(max_input_chars=2)):
            result,requests=await self.call({},config=cfg)
            self.assertEqual(requests,[])
            self.assertFalse(result.attempted)

    async def test_http_failure_returns_raw_without_private_details(self):
        result,requests=await self.call({'secret':'PRIVATE'},status=500)
        self.assertEqual(result.text,'привет мир это тест')
        self.assertFalse(result.accepted)
        self.assertTrue(result.attempted)
        self.assertNotIn('PRIVATE',result.error)
        self.assertEqual(len(requests),1)

    def test_numbers_and_cyrillic_words_cannot_change(self):
        self.assertTrue(same_words('ёлка 12','Ёлка, 12!'))
        self.assertFalse(same_words('ёлка 12','елка 13'))
        self.assertFalse(same_words('3.14','3,14'))
        self.assertFalse(same_words('user_name','user name'))
        self.assertFalse(same_words('что-то','что то'))


class FormatterPipelineTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        cfg=replace(load_config('config.example.yaml'),formatter=FormatterConfig(True,'p',{'p':'provider/model'}))
        self.db.money_settings=MoneySettings(global_daily_rub='100')
        self.ctx=AppContext(cfg,self.db,AsyncMock(),AsyncMock())
        self.ctx.prices=synthetic_catalog(Path(self.tmp.name)/"prices.json", [m.provider_model for m in cfg.models.values()])
        self.ctx.transcriber.transcribe.return_value=SimpleNamespace(text='привет мир',cost=Decimal('0.1'),duration_seconds=3)
        self.ctx.formatter.format.return_value=FormatResult('Привет, мир!',0.2,True,True)
        self.message=SimpleNamespace(chat=SimpleNamespace(id=1,type='private'),from_user=SimpleNamespace(id=1),message_id=1,
            voice=SimpleNamespace(file_id='f',file_unique_id='f',duration=3,file_size=10,mime_type='audio/ogg'),
            answer=AsyncMock())

    async def download(self,*args):
        path=Path(self.tmp.name)/'voice.ogg';path.write_bytes(b'test');return path,'voice.ogg'

    async def transcribe(self,raw=False):
        with patch('voicebot.handlers.download_media_to_temp',side_effect=self.download):
            await transcribe_message_media(self.message,self.message,self.ctx,AsyncMock(),self.ctx.config.default_model,raw=raw)

    async def test_unreserved_formatter_is_skipped_and_raw_cache_survives(self):
        await self.transcribe();await self.transcribe()
        cached=await self.db.cached_transcript('f',self.ctx.config.default_model)
        self.assertEqual(cached['transcript'],'привет мир')
        self.assertIsNone(cached['formatted_transcript'])
        self.ctx.formatter.format.assert_not_awaited()
        self.ctx.transcriber.transcribe.assert_awaited_once()
        stats=await self.db.usage_today()
        self.assertEqual(stats[0]['known_cost_rub'],Decimal('0.1'))
        self.assertEqual(stats[0]['unknown_cost'],0)
        self.message.answer.reset_mock();await self.transcribe(raw=True)
        self.assertIn('привет мир',str(self.message.answer.call_args_list))

    async def test_disabled_or_changed_formatter_uses_raw_cache_without_payment(self):
        await self.transcribe()
        self.message.answer.reset_mock()
        self.ctx.config=replace(self.ctx.config,formatter=replace(self.ctx.config.formatter,default_model='different'))
        await self.transcribe()
        self.assertIn('привет мир',str(self.message.answer.call_args_list))
        self.ctx.formatter.format.assert_not_awaited()

    async def test_interruption_after_raw_save_preserves_stt_without_formatter_payment(self):
        original=self.db.record_financial_response
        async def save(*args,**kwargs):
            await original(*args,**kwargs)
            raise asyncio.CancelledError()
        with patch.object(self.db,'record_financial_response',side_effect=save):
            with self.assertRaises(asyncio.CancelledError):
                await self.transcribe()
        self.assertIsNotNone(await self.db.cached_transcript('f',self.ctx.config.default_model))
        await self.db.recover_paid_calls()
        await self.transcribe()
        self.ctx.formatter.format.assert_not_awaited()
        self.ctx.transcriber.transcribe.assert_awaited_once()

    async def test_revoke_after_stt_prevents_extra_paid_formatting(self):
        async def transcribe(*args,**kwargs):
            await self.db.upsert_config_admins(set())
            return SimpleNamespace(text='привет мир',cost=Decimal('0.1'),duration_seconds=3)
        self.ctx.transcriber.transcribe.side_effect=transcribe
        await self.transcribe()
        self.ctx.formatter.format.assert_not_awaited()
        self.assertIsNotNone(await self.db.cached_transcript('f',self.ctx.config.default_model))
