import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from tests.test_access import RecordingSession
from tests.tariff_fixtures import catalog
from voicebot.config import load_config
from voicebot.db import Database
from voicebot.handlers import AppContext, cmd_help, cmd_status
from voicebot.money import MoneySettings, MoneyError, to_atoms, MAX_ATOMS
from voicebot.paid_store import BudgetLimits
from voicebot.money_text import amount, balance_text
from voicebot.money_ui import (balance_command,balance_callback,money_admin_callback,money_amount,
                               cancel_money_input,helphelp,help_callback,router as money_router)


class MoneyUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Database(Path(self.tmp.name)/'safe.sqlite3',MoneySettings(global_daily_rub='100'))
        await self.db.open();await self.db.upsert_config_admins({1})
        await self.db.register_admin_private_chat(1,1,None,'Synthetic administrator')
        await self.db.touch_money_user(2)
        self.session=RecordingSession();self.bot=Bot('123456:TEST_ONLY',session=self.session)
        cfg=load_config('config.example.yaml')
        self.ctx=AppContext(cfg,self.db,AsyncMock())
        self.ctx.prices=catalog(Path(self.tmp.name)/'prices.json',[m.provider_model for m in cfg.models.values()])
        self.storage=MemoryStorage()
        self.state=FSMContext(self.storage,StorageKey(bot_id=123456,chat_id=1,user_id=1))

    async def asyncTearDown(self):
        await self.db.close();await self.bot.session.close();await self.storage.close();self.tmp.cleanup()

    def message(self,user=1,chat=None,text='synthetic',number=10):
        chat=user if chat is None else chat
        return Message(message_id=number,date=datetime.now(timezone.utc),chat=Chat(id=chat,type='private' if chat>0 else 'group',title='Synthetic'),
                       from_user=User(id=user,is_bot=False,first_name='Synthetic'),text=text).as_(self.bot)

    def callback(self,data,user=1,chat=None,number=10):
        return CallbackQuery(id='synthetic',from_user=User(id=user,is_bot=False,first_name='Synthetic'),
                             chat_instance='synthetic',data=data,message=self.message(user,chat,number=number)).as_(self.bot)

    async def intent(self,action='extra',value='1.25',**kwargs):
        b=await self.db.money_balance('user',2)
        token=await self.db.create_money_intent(1,1,'user',2,action,value,revision=b['quota_revision'],period_start=b['period_start'],**kwargs)
        await self.db.bind_money_intent(token,20)
        return token

    async def test_exact_display_never_calls_positive_fraction_zero(self):
        self.assertEqual(amount(0),'0 Б')
        self.assertEqual(amount(1),'меньше 0,000001 Б')
        self.assertEqual(amount(to_atoms('1000')),'1000 Б')
        self.assertEqual(amount(MAX_ATOMS*2),'2000000000 Б')

    async def test_invalid_subject_ids_fail_before_any_financial_write(self):
        for kind,subject in (('user',0),('group',1),('user',2**63)):
            with self.subTest(kind=kind,subject=subject),self.assertRaises(MoneyError):
                await self.db.create_money_intent(1,1,kind,subject,'extra','1')
        self.assertEqual((await (await self.db.db.execute('SELECT COUNT(*) FROM money_intents')).fetchone())[0],0)

    async def test_group_balance_and_forged_personal_button_do_not_leak_history(self):
        with patch.object(self.db,'money_history',wraps=self.db.money_history) as history:
            await balance_command(self.message(2,-10,'/balance'),self.ctx)
            await balance_callback(self.callback('bal:1:history:0',user=2),self.ctx,self.bot)
            await balance_callback(self.callback('bal:2:history:0',user=2,chat=-10),self.ctx,self.bot)
            history.assert_not_awaited()
        text=str(self.session.calls)
        self.assertNotIn('Бесплатные:',text)
        self.assertNotIn('История операций:',text)

    async def test_confirmation_binds_owner_chat_message_and_replay(self):
        token=await self.intent()
        await self.db.upsert_config_admins({1,3})
        for owner,chat,message in ((3,3,20),(1,-10,20),(1,1,21)):
            self.assertEqual((await self.db.apply_money_intent(token,owner,chat,message))['status'],'unavailable')
        self.assertEqual((await self.db.apply_money_intent(token,1,1,20))['status'],'applied')
        self.assertEqual((await self.db.apply_money_intent(token,1,1,20))['status'],'unavailable')
        self.assertEqual((await self.db.money_balance('user',2))['extra_atoms'],to_atoms('1.25'))

    async def test_expired_stale_quota_and_removed_administrator_do_not_apply(self):
        now=datetime.now(timezone.utc)
        expired=await self.intent(at=now-timedelta(minutes=6))
        self.assertEqual((await self.db.apply_money_intent(expired,1,1,20))['status'],'unavailable')
        stale=await self.intent()
        await self.db.adjust_money('other','user',2,'1',admin_id=1)
        self.assertEqual((await self.db.apply_money_intent(stale,1,1,20))['status'],'stale')
        removed=await self.intent()
        await self.db.upsert_config_admins(set())
        self.assertEqual((await self.db.apply_money_intent(removed,1,1,20))['status'],'unavailable')
        self.assertEqual((await self.db.money_balance('user',2))['extra_atoms'],to_atoms('1'))

    async def test_cancel_button_and_command_invalidate_old_confirmations(self):
        token=await self.intent()
        self.assertIsNotNone(await self.db.cancel_money_intent(token,1,1,20))
        self.assertEqual((await self.db.apply_money_intent(token,1,1,20))['status'],'unavailable')
        second=await self.intent()
        await cancel_money_input(self.message(text='/cancel'),self.ctx,self.state)
        self.assertEqual((await self.db.apply_money_intent(second,1,1,20))['status'],'unavailable')
        self.assertEqual((await self.db.money_balance('user',2))['extra_atoms'],0)

    async def test_current_grant_future_quota_and_extra_have_distinct_expiry(self):
        for action,value in (('monthly','50'),('current','25'),('extra','10')):
            token=await self.intent(action,value)
            self.assertEqual((await self.db.apply_money_intent(token,1,1,20))['status'],'applied')
        b=await self.db.money_balance('user',2)
        self.assertEqual((b['free_atoms'],b['extra_atoms']),(to_atoms('1025'),to_atoms('10')))
        b=await self.db.money_balance('user',2,at=datetime.fromisoformat(b['period_end']))
        self.assertEqual((b['free_atoms'],b['extra_atoms']),(to_atoms('50'),to_atoms('10')))

    async def test_midnight_period_change_invalidates_still_unexpired_confirmation(self):
        anchor=datetime(2024,1,31,9,tzinfo=timezone.utc)
        before=datetime(2024,2,28,20,59,tzinfo=timezone.utc)
        await self.db.touch_money_user(5,at=anchor)
        balance=await self.db.money_balance('user',5,at=before)
        token=await self.db.create_money_intent(1,1,'user',5,'current','100',revision=balance['quota_revision'],
                                               period_start=balance['period_start'],at=before)
        await self.db.bind_money_intent(token,20)
        result=await self.db.apply_money_intent(token,1,1,20,at=before+timedelta(minutes=2))
        self.assertEqual(result['status'],'stale')
        balance=await self.db.money_balance('user',5,at=before+timedelta(minutes=2))
        self.assertEqual(balance['free_atoms'],to_atoms('1000'))

    async def test_correction_cannot_take_reserved_or_make_negative_balance(self):
        await self.db.reserve_money('synthetic','user',2,'5')
        token=await self.intent('debit-current','501')
        self.assertEqual((await self.db.apply_money_intent(token,1,1,20))['status'],'stale')
        b=await self.db.money_balance('user',2)
        self.assertEqual((b['free_atoms'],b['held_atoms']),(to_atoms('500'),to_atoms('500')))
        token=await self.intent('debit-extra','1')
        self.assertEqual((await self.db.apply_money_intent(token,1,1,20))['status'],'stale')

    async def test_permission_confirmation_does_not_grant_money_or_survive_revision_change(self):
        token=await self.db.create_money_intent(1,1,'user',3,'trf-on',permission_revision=-1)
        await self.db.bind_money_intent(token,20)
        self.assertEqual((await self.db.apply_money_intent(token,1,1,20))['status'],'applied')
        self.assertTrue(await self.db.extended_audio_allowed(3))
        with self.assertRaises(MoneyError):await self.db.money_balance('user',3)
        row=await self.db.access_subject('user',3)
        token=await self.db.create_money_intent(1,1,'user',3,'trf-off',permission_revision=row['revision'])
        await self.db.bind_money_intent(token,20)
        await self.db.set_extended_audio(3,True,1)
        self.assertEqual((await self.db.apply_money_intent(token,1,1,20))['status'],'stale')

    async def test_refill_request_is_idempotent_and_can_be_closed_or_funded(self):
        self.assertTrue(await self.db.request_money_topup(2))
        self.assertFalse(await self.db.request_money_topup(2))
        request=await self.db.money_request(2)
        token=await self.db.create_money_intent(1,1,'user',2,'request-close',request_stamp=request['created_at'])
        await self.db.bind_money_intent(token,20)
        self.assertEqual((await self.db.apply_money_intent(token,1,1,20))['status'],'applied')
        self.assertIsNone(await self.db.money_request(2))
        self.assertEqual((await self.db.money_balance('user',2))['extra_atoms'],0)

    async def test_non_admin_and_group_cannot_open_financial_controls(self):
        await money_admin_callback(self.callback('mu:edit:user:2:extra',user=2),self.ctx,self.state,self.bot)
        await money_admin_callback(self.callback('mu:edit:user:2:extra',chat=-10),self.ctx,self.state,self.bot)
        rows=await (await self.db.db.execute('SELECT * FROM money_intents')).fetchall()
        self.assertEqual(rows,[])
        self.assertIsNone(await self.state.get_state())

    async def test_help_is_short_and_full_help_has_public_sections_without_account_data(self):
        await cmd_help(self.message(2,-10,'/help'),self.ctx)
        self.assertLess(len(self.session.calls[-1].text),700)
        await helphelp(self.message(2,-10,'/helphelp'))
        await help_callback(self.callback('hh:money',user=2,chat=-10))
        text=str(self.session.calls)
        self.assertIn('0,01',text);self.assertNotIn('Бесплатные:',text)

    async def test_history_contains_operations_and_models_without_transcript(self):
        token=await self.intent()
        await self.db.apply_money_intent(token,1,1,20)
        await self.db.ensure_private_user(2,'Synthetic',None)
        request=await self.db.create_access_request('user',2,'Synthetic',2,None,'synthetic',0)
        await self.db.decide_access_request(request.id,'approve_forever',1)
        model=self.ctx.config.model()
        quote=lambda:self.ctx.prices.estimate_audio(model.provider_model,601)
        job=await self.db.create_job(2,2,1,'file','unique',model.alias,model.provider_model)
        reason=await self.db.reserve_financial_call(quote=quote,provider_model=model.provider_model,job_id=job,
                chat_id=2,user_id=2,private=True,file_unique_id='unique',model_alias=model.alias,seconds=1,limits=BudgetLimits())
        self.assertIsNone(reason)
        await self.db.mark_financial_sending(job,quote,True)
        await self.db.record_financial_response(job,SimpleNamespace(text='PRIVATE_TRANSCRIPT',cost=Decimal('0.01'),duration_seconds=1))
        await self.db.finalize_financial_response(job)
        await balance_callback(self.callback('bal:2:history:0',user=2),self.ctx,self.bot)
        self.assertIn('Дополнительное начисление',str(self.session.calls))
        self.assertIn(model.provider_model,str(self.session.calls))
        self.assertIn('0.01',str(self.session.calls))
        self.assertNotIn('PRIVATE_TRANSCRIPT',str(self.session.calls))

    async def test_refill_notification_is_once_and_funding_resolves_pending_request(self):
        for _ in range(2):await balance_callback(self.callback('bal:2:request',user=2),self.ctx,self.bot)
        notices=[call for call in self.session.calls if (getattr(call,'text','') or '').startswith('Запрос пополнения от')]
        self.assertEqual(len(notices),1)
        self.assertEqual(str(notices[0].chat_id),'1')
        token=await self.intent()
        self.assertEqual((await self.db.apply_money_intent(token,1,1,20))['status'],'applied')
        self.assertIsNone(await self.db.money_request(2))

    async def test_notification_failure_keeps_request_in_private_admin_queue(self):
        failed_bot=SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('synthetic delivery failure')))
        await balance_callback(self.callback('bal:2:request',user=2),self.ctx,failed_bot)
        rows,_=await self.db.pending_money_requests()
        self.assertEqual([row['user_id'] for row in rows],[2])

    async def test_dispatcher_amount_then_confirm_routes_to_one_atomic_operation(self):
        dp=Dispatcher();dp.include_router(money_router)
        try:
            await dp.feed_update(self.bot,Update(update_id=1,callback_query=self.callback('mu:edit:user:2:extra')),ctx=self.ctx)
            await dp.feed_update(self.bot,Update(update_id=2,message=self.message(text='1,25',number=11)),ctx=self.ctx)
            row=await (await self.db.db.execute('SELECT token,message_id FROM money_intents ORDER BY rowid DESC LIMIT 1')).fetchone()
            self.assertIsNotNone(row)
            callback=self.callback('mu:confirm:'+row['token'],number=row['message_id'])
            await dp.feed_update(self.bot,Update(update_id=3,callback_query=callback),ctx=self.ctx)
            await dp.feed_update(self.bot,Update(update_id=4,callback_query=callback),ctx=self.ctx)
            self.assertEqual((await self.db.money_balance('user',2))['extra_atoms'],to_atoms('1.25'))
        finally:await dp.storage.close()
