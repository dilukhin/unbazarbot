import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from voicebot.db import Database
from voicebot.money import MoneySettings, MoneyError, to_atoms
from voicebot.paid_store import BudgetLimits
from voicebot.provider_costs import GenerationCost, generation_cost
from voicebot.stt_routerai import RouterAITranscriber, ProviderCallError
from voicebot.tariffs import Estimate


class ProviderCostTests(unittest.IsolatedAsyncioTestCase):
    async def test_decimal_json_and_generation_id_preserve_exact_cost(self):
        requests=[]
        async def handle(request):
            requests.append(request)
            return httpx.Response(200,content=b'{"text":"synthetic","usage":{"cost":0.000000000000123456789,"seconds":2}}',headers={'X-Generation-Id':'gen_synthetic'})
        original=httpx.AsyncClient
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'audio.ogg';p.write_bytes(b'synthetic')
            with patch('httpx.AsyncClient',side_effect=lambda **kw:original(transport=httpx.MockTransport(handle),**kw)):
                before=AsyncMock()
                result=await RouterAITranscriber('TEST_ONLY').transcribe(p,model='synthetic/asr',audio_format='ogg',before_send=before)
        before.assert_awaited_once()
        self.assertEqual(result.cost,Decimal('0.000000000000123456789'))
        self.assertEqual(result.generation_id,'gen_synthetic')
        self.assertEqual(result.duration_seconds,2)
        self.assertEqual(len(requests),1)

    async def test_metadata_retry_is_bounded_and_never_posts_audio(self):
        requests=[]
        def handle(request):
            requests.append(request)
            if len(requests)==1:return httpx.Response(404)
            return httpx.Response(200,content=b'{"data":{"id":"gen_synthetic","model":"synthetic/asr","total_cost":0.000000000001}}')
        original=httpx.AsyncClient
        with patch('httpx.AsyncClient',side_effect=lambda **kw:original(transport=httpx.MockTransport(handle),**kw)):
            result=await generation_cost('TEST_ONLY','https://routerai.ru/api/v1','gen_synthetic','synthetic/asr')
        self.assertEqual(result,GenerationCost('verified',Decimal('0.000000000001')))
        self.assertEqual(len(requests),2)
        self.assertTrue(all(r.method=='GET' and r.url.path=='/api/v1/generation' for r in requests))

    async def test_wrong_generation_identity_and_model_rejected(self):
        original=httpx.AsyncClient
        for key in ('id','model'):
            payload={'data':{'id':'gen_synthetic','model':'synthetic/asr','total_cost':'0.1'}}
            payload['data'][key]='other'
            with self.subTest(key=key),patch('httpx.AsyncClient',side_effect=lambda **kw:original(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=payload)),**kw)):
                self.assertEqual((await generation_cost('TEST_ONLY','https://routerai.ru/api/v1','gen_synthetic','synthetic/asr')).status,'mismatch')

    async def test_pre_send_refusal_makes_no_http_request(self):
        calls=[];original=httpx.AsyncClient
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'audio.ogg';p.write_bytes(b'synthetic')
            with patch('httpx.AsyncClient',side_effect=lambda **kw:original(transport=httpx.MockTransport(lambda r:calls.append(r) or httpx.Response(500)),**kw)):
                with self.assertRaises(ProviderCallError) as caught:
                    await RouterAITranscriber('TEST_ONLY').transcribe(p,model='synthetic/asr',audio_format='ogg',before_send=AsyncMock(side_effect=MoneyError('synthetic refusal')))
        self.assertFalse(caught.exception.sent)
        self.assertEqual(calls,[])


class FinancialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Database(Path(self.tmp.name)/'synthetic.sqlite3',MoneySettings(global_daily_rub='10'))
        await self.db.open();await self.db.upsert_config_admins({1})
        self.quote=lambda:Estimate('available',max_rub=Decimal('1'),snapshot_id='sha256:synthetic',checked_at=datetime.now(timezone.utc))
        self.limits=BudgetLimits(100,100,120,120,120,20)

    async def asyncTearDown(self):
        await self.db.close();self.tmp.cleanup()

    async def reserve(self, key='file', *, user=1,chat=1,auto=False,alias='asr',provider='synthetic/asr'):
        job=await self.db.create_job(chat,user,1,key,key,alias,provider)
        reason=await self.db.reserve_financial_call(quote=self.quote,provider_model=provider,job_id=job,
                    chat_id=chat,user_id=user,private=chat>0,file_unique_id=key,model_alias=alias,seconds=2,
                    limits=self.limits,require_auto=auto)
        return job,reason

    async def response(self, job, cost='0.1', generation=GenerationCost('unavailable')):
        await self.db.mark_financial_sending(job,self.quote,True)
        await self.db.record_financial_response(job,SimpleNamespace(text='synthetic',cost=Decimal(cost) if cost is not None else None,duration_seconds=2,generation_id='gen_synthetic'))
        await self.db.finalize_financial_response(job,generation)

    async def call_row(self, job):
        return dict(await (await self.db.db.execute('SELECT * FROM financial_calls WHERE job_id=?',(job,))).fetchone())

    async def test_exact_charge_and_repeated_finalization_only_once(self):
        job,reason=await self.reserve();self.assertIsNone(reason)
        await self.response(job,'0.0000000000001')
        await self.db.finalize_financial_response(job)
        b=await self.db.money_balance('user',1)
        self.assertEqual(b['free_atoms'],to_atoms('1000')-1)
        self.assertEqual(b['held_atoms'],0)
        row=await self.call_row(job)
        self.assertEqual((row['state'],row['final_rub']),('settled','0.0000000000001'))

    async def test_missing_usage_can_settle_from_verified_generation(self):
        job,_=await self.reserve()
        await self.response(job,None,GenerationCost('verified',Decimal('0.3')))
        self.assertEqual((await self.call_row(job))['final_rub'],'0.3')

    async def test_saved_generation_can_reconcile_unknown_without_resending_audio(self):
        job,_=await self.reserve()
        await self.response(job,None)
        client=SimpleNamespace(generation_cost=AsyncMock(return_value=GenerationCost('verified',Decimal('0.05'))),transcribe=AsyncMock())
        row=await self.db.reconcile_financial_call(job,client)
        self.assertEqual((row['state'],row['final_rub']),('settled','0.05'))
        client.transcribe.assert_not_awaited()
        client.generation_cost.assert_awaited_once_with('gen_synthetic','synthetic/asr')

    async def test_unknown_mismatch_and_overrun_hold_and_overrun_freezes_model(self):
        for key,usage,details,state in (
            ('unknown',None,GenerationCost('unavailable'),'unknown'),
            ('mismatch','0.1',GenerationCost('verified',Decimal('0.2')),'mismatch'),
            ('overrun','1.1',GenerationCost('verified',Decimal('1.1')),'overrun'),
        ):
            job,reason=await self.reserve(key);self.assertIsNone(reason)
            await self.response(job,usage,details)
            self.assertEqual((await self.call_row(job))['state'],state)
        b=await self.db.money_balance('user',1)
        self.assertEqual(b['held_atoms'],to_atoms('300'))
        self.assertGreaterEqual(b['free_atoms'],0)
        self.assertIn('приостановлена',(await self.reserve('frozen'))[1])
        await self.db.close();await self.db.open();await self.db.recover_paid_calls();await self.db.recover_financial_calls()
        self.assertEqual((await self.db.money_balance('user',1))['held_atoms'],to_atoms('300'))

    async def test_global_and_personal_limits_rollback_once_right_and_legacy_reserve(self):
        await self.db.ensure_group(-10,'Synthetic')
        req=await self.db.create_access_request('group',-10,'Synthetic',1,None,'synthetic',0)
        await self.db.decide_access_request(req.id,'approve_once',1)
        self.db.money_settings=MoneySettings(global_daily_rub='0')
        job,reason=await self.reserve(chat=-10)
        self.assertIn('общий',reason)
        self.assertEqual((await self.db.get_group(-10)).remaining_jobs,1)
        self.assertIsNone(await (await self.db.db.execute('SELECT 1 FROM paid_calls WHERE job_id=?',(job,))).fetchone())
        self.db.money_settings=MoneySettings(global_daily_rub='10')
        self.quote=lambda:Estimate('available',max_rub=Decimal('11'))
        job,reason=await self.reserve('expensive',chat=-10)
        self.assertIsNotNone(reason)
        self.assertEqual((await self.db.get_group(-10)).remaining_jobs,1)

    async def test_proven_not_sent_refunds_once_right_and_allows_retry(self):
        await self.db.ensure_group(-10,'Synthetic')
        req=await self.db.create_access_request('group',-10,'Synthetic',1,None,'synthetic',0)
        await self.db.decide_access_request(req.id,'approve_once',1)
        job,reason=await self.reserve(chat=-10);self.assertIsNone(reason)
        await self.db.mark_financial_sending(job,self.quote,True)
        self.assertFalse(await self.db.release_financial_not_sent(job))
        self.assertTrue(await self.db.release_financial_not_sent(job,proven_not_sent=True))
        self.assertEqual((await self.db.get_group(-10)).remaining_jobs,1)
        self.assertEqual((await self.db.money_balance('user',1))['held_atoms'],0)
        self.assertIsNone((await self.reserve(chat=-10))[1])

    async def test_actual_model_prevents_duplicate_across_aliases_and_cache_is_not_remapped(self):
        job,_=await self.reserve(alias='one')
        await self.response(job,'0')
        self.assertIsNotNone(await self.db.cached_transcript('file','two','synthetic/asr'))
        self.assertIsNone(await self.db.cached_transcript('file','one','synthetic/other'))
        self.assertIsNotNone((await self.reserve(alias='two'))[1])
        self.assertIsNone((await self.reserve(alias='one',provider='synthetic/other'))[1])

    async def test_manual_group_uses_user_auto_uses_group_even_if_user_has_no_funds(self):
        await self.db.ensure_group(-10,'Synthetic')
        req=await self.db.create_access_request('group',-10,'Synthetic',1,None,'synthetic',0)
        await self.db.decide_access_request(req.id,'approve_forever',1)
        await self.db.set_group_auto(-10,True)
        manual,reason=await self.reserve('manual',chat=-10);self.assertIsNone(reason)
        self.assertEqual((await self.call_row(manual))['kind'],'user')
        auto,reason=await self.reserve('auto',chat=-10,auto=True,user=None);self.assertIsNone(reason)
        self.assertEqual((await self.call_row(auto))['kind'],'group')
        self.assertIsNotNone((await self.reserve('anonymous',chat=-10,user=None))[1])

    async def test_revoke_or_price_growth_after_reserve_refuses_sending(self):
        job,_=await self.reserve()
        grown=lambda:Estimate('available',max_rub=Decimal('2'))
        with self.assertRaises(MoneyError):await self.db.mark_financial_sending(job,grown,True)
        await self.db.upsert_config_admins(set())
        with self.assertRaises(MoneyError):await self.db.mark_financial_sending(job,self.quote,True)
        self.assertTrue(await self.db.release_financial_not_sent(job))

    async def test_overrun_stops_other_pending_call_when_global_cap_is_exceeded(self):
        self.db.money_settings=MoneySettings(global_daily_rub='2')
        first,_=await self.reserve('first')
        second,_=await self.reserve('second',provider='synthetic/other')
        await self.response(first,'3')
        with self.assertRaises(MoneyError):await self.db.mark_financial_sending(second,self.quote,True)
        self.assertTrue(await self.db.release_financial_not_sent(second))

    async def test_concurrent_global_limit_and_same_actual_model_are_atomic(self):
        self.db.money_settings=MoneySettings(global_daily_rub='1')
        rows=await asyncio.gather(self.reserve('a'),self.reserve('b'))
        self.assertEqual(sum(reason is None for job,reason in rows),1)
        self.assertEqual((await self.db.money_balance('user',1))['held_atoms'],to_atoms('100'))

    async def test_empty_group_budget_keeps_auto_flag_manual_personal_still_works(self):
        self.db.money_settings=MoneySettings(group_monthly_bazariki='0',global_daily_rub='10')
        await self.db.ensure_group(-10,'Synthetic')
        req=await self.db.create_access_request('group',-10,'Synthetic',1,None,'synthetic',0)
        await self.db.decide_access_request(req.id,'approve_forever',1)
        await self.db.set_group_auto(-10,True)
        self.assertIsNotNone((await self.reserve('auto-empty',chat=-10,auto=True))[1])
        self.assertTrue((await self.db.get_group(-10)).auto_enabled)
        self.assertIsNone((await self.reserve('manual-funded',chat=-10))[1])

    async def test_restart_releases_only_proven_reserved_calls_and_keeps_sending_unknown(self):
        reserved,_=await self.reserve('not-sent')
        sending,_=await self.reserve('may-have-sent')
        await self.db.mark_financial_sending(sending,self.quote,True)
        await self.db.close();await self.db.open()
        await self.db.recover_paid_calls();await self.db.recover_financial_calls()
        self.assertEqual((await self.call_row(reserved))['state'],'not-sent')
        self.assertEqual((await self.call_row(sending))['state'],'unknown')
        self.assertEqual((await self.db.money_balance('user',1))['held_atoms'],to_atoms('100'))
        self.assertIsNone((await self.reserve('not-sent'))[1])
        self.assertIsNotNone((await self.reserve('may-have-sent'))[1])

    async def test_empty_personal_budget_does_not_block_funded_auto_group(self):
        self.db.money_settings=MoneySettings(user_monthly_bazariki='0',global_daily_rub='10')
        await self.db.ensure_group(-10,'Synthetic')
        req=await self.db.create_access_request('group',-10,'Synthetic',1,None,'synthetic',0)
        await self.db.decide_access_request(req.id,'approve_forever',1)
        await self.db.set_group_auto(-10,True)
        self.assertIsNotNone((await self.reserve('manual-empty',chat=-10))[1])
        self.assertIsNone((await self.reserve('auto-funded',chat=-10,auto=True))[1])
