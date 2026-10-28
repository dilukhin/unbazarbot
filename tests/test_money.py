import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import Chat, Message, User, Voice

from voicebot.db import Database
from voicebot.money import MoneyError, MoneySettings, bazariki, period, to_atoms
from voicebot.money_middleware import ExplicitMoneyInteraction


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


JAN = at('2024-01-31T09:00:00')
FEB = at('2024-02-28T21:00:00')  # 29 February, midnight Moscow.
MAR = at('2024-03-30T21:00:00')


class ExactMoneyTests(unittest.TestCase):
    def test_round_once_up_without_float_or_ambient_precision(self):
        with localcontext() as ctx:
            ctx.prec = 3
            self.assertEqual(to_atoms('0.000000000001', rub=True), 1)
            self.assertEqual(to_atoms('0', rub=True), 0)
            self.assertEqual(to_atoms('1.234567890001', rub=True),123456789001)
            self.assertEqual(bazariki(123456789001),Decimal('123.456789001'))
        for value in (0.1,True,'NaN','Infinity','-1','1e1000000','1e-1000000'):
            with self.subTest(value=value),self.assertRaises(MoneyError):to_atoms(value)
        with self.assertRaises(MoneyError):MoneySettings(user_monthly_bazariki=0.1)

    def test_anchor_day_survives_short_month_and_moscow_boundary(self):
        self.assertEqual(period(JAN,FEB)[0],'2024-02-28T21:00:00+00:00')
        self.assertEqual(period(JAN,MAR)[0],'2024-03-30T21:00:00+00:00')
        self.assertEqual(period(at('2023-01-31T00:00:00'),at('2023-02-28T22:00:00'))[1],'2023-03-30T21:00:00+00:00')
        self.assertEqual(period(JAN,at('2024-02-28T20:59:59'))[0],'2024-01-30T21:00:00+00:00')
        with self.assertRaises(MoneyError):period(JAN,datetime(2024,2,1))


class LedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'synthetic.sqlite3'
        self.db = Database(self.path)
        await self.db.open()
        await self.db.upsert_config_admins({1})
        await self.db.touch_money_user(101,at=JAN)

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def balance(self, when=JAN):
        return await self.db.money_balance('user',101,at=when)

    async def test_concurrent_monthly_grants_and_restart_are_idempotent(self):
        await asyncio.gather(*(self.db.money_balance('user',101,at=FEB) for _ in range(12)))
        await self.db.close();await self.db.open()
        b=await self.balance(FEB)
        self.assertEqual(b['free_atoms'],to_atoms('1000'))
        rows=await (await self.db.db.execute("SELECT * FROM money_journal WHERE event='monthly'")).fetchall()
        self.assertEqual(len(rows),2)
        self.assertEqual((await self.balance(MAR))['period_start'],'2024-03-30T21:00:00+00:00')

    async def test_free_first_extra_persists_and_actual_fraction_is_exact(self):
        await self.db.adjust_money('topup','user',101,'10',admin_id=1,at=JAN)
        await self.db.reserve_money('job','user',101,'10.05',at=JAN)
        b=await self.balance();self.assertEqual(b['free_atoms'],0)
        self.assertEqual(b['extra_atoms'],to_atoms('5'))
        self.assertEqual(await self.db.settle_money('job','9.999999999999',at=JAN),to_atoms('9.999999999999',rub=True))
        b=await self.balance();self.assertEqual(b['free_atoms'],0)
        self.assertEqual(b['extra_atoms'],to_atoms('10'))
        b=await self.balance(FEB)
        self.assertEqual(b['free_atoms'],to_atoms('1000'))
        self.assertEqual(b['extra_atoms'],to_atoms('10'))

    async def test_reserve_duplicate_and_conflicting_finalization(self):
        await asyncio.gather(*(self.db.reserve_money('same','user',101,'0.000000000001',at=JAN) for _ in range(8)))
        self.assertEqual((await self.balance())['held_atoms'],1)
        await self.db.settle_money('same','0.000000000001',at=JAN)
        await self.db.settle_money('same','0.000000000001',at=JAN)
        self.assertEqual((await self.balance())['free_atoms'],to_atoms('1000')-1)
        with self.assertRaises(MoneyError):await self.db.release_money('same',at=JAN)
        with self.assertRaises(MoneyError):await self.db.reserve_money('same','user',101,'0.01',at=JAN)

    async def test_competing_reserves_cannot_make_negative_balance(self):
        result=await asyncio.gather(*(self.db.reserve_money(f'job{i}','user',101,'6',at=JAN) for i in range(2)),return_exceptions=True)
        self.assertEqual(sum(isinstance(r,MoneyError) for r in result),1)
        b=await self.balance();self.assertEqual(b['free_atoms'],to_atoms('400'))
        self.assertEqual(b['held_atoms'],to_atoms('600'))

    async def test_old_free_refund_expires_extra_refund_survives(self):
        await self.db.adjust_money('topup','user',101,'100',admin_id=1,at=JAN)
        await self.db.reserve_money('old','user',101,'11',at=JAN)
        b=await self.balance(FEB);self.assertEqual(b['held_atoms'],to_atoms('1100'))
        await self.db.release_money('old',at=FEB)
        b=await self.balance(FEB)
        self.assertEqual((b['free_atoms'],b['extra_atoms'],b['held_atoms']),(to_atoms('1000'),to_atoms('100'),0))
        rows=await (await self.db.db.execute("SELECT free_atoms FROM money_periods ORDER BY starts")).fetchall()
        self.assertEqual(rows[0][0],0)

    async def test_overrun_and_unknown_outcome_preserve_hold(self):
        await self.db.reserve_money('unknown','user',101,'1',at=JAN)
        with self.assertRaises(MoneyError):await self.db.settle_money('unknown','1.000000000001',at=JAN)
        await self.db.close();await self.db.open()
        b=await self.balance(JAN)
        self.assertEqual((b['held_atoms'],b['free_atoms']),(to_atoms('100'),to_atoms('900')))

    async def test_adjustments_require_admin_and_unique_payload(self):
        with self.assertRaises(MoneyError):await self.db.adjust_money('bad','user',101,'1',admin_id=999,at=JAN)
        for _ in range(2):await self.db.adjust_money('topup','user',101,'0.5',admin_id=1,at=JAN)
        with self.assertRaises(MoneyError):await self.db.adjust_money('topup','user',101,'1',admin_id=1,at=JAN)
        await self.db.adjust_money('quota','user',101,'50',admin_id=1,monthly=True,at=JAN)
        self.assertEqual((await self.balance())['free_atoms'],to_atoms('1000'))
        b=await self.balance(FEB);self.assertEqual(b['free_atoms'],to_atoms('50'))
        self.assertEqual(b['extra_atoms'],to_atoms('0.5'))

    async def test_transaction_can_include_other_checks_and_cancel_atomically(self):
        async with self.db.access_transaction() as conn:
            try:
                await self.db.reserve_money('denied','user',101,'11',at=JAN,conn=conn)
            except MoneyError:
                pass  # #20 may handle a denial inside its larger transaction.
        row=await (await self.db.db.execute("SELECT 1 FROM money_journal WHERE operation_id='reserve:denied'")).fetchone()
        self.assertIsNone(row)
        with self.assertRaises(asyncio.CancelledError):
            async with self.db.access_transaction() as conn:
                await self.db.reserve_money('cancel','user',101,'2',at=JAN,conn=conn)
                raise asyncio.CancelledError()
        b=await self.balance();self.assertEqual((b['free_atoms'],b['held_atoms']),(to_atoms('1000'),0))
        with self.assertRaises(MoneyError):await self.balance(at('2024-01-31T08:59:59'))

    async def test_group_persistent_approval_anchors_once_without_reset_on_revoke(self):
        await self.db.ensure_group(-101,'Synthetic')
        async with self.db.access_transaction() as conn:
            await self.db._set_access(conn,'group',-101,'approve_once',1,1)
        with self.assertRaises(MoneyError):await self.db.money_balance('group',-101)
        async with self.db.access_transaction() as conn:
            await self.db._set_access(conn,'group',-101,'approve_forever',1,0)
        original=await self.db.money_balance('group',-101)
        self.assertEqual(original['free_atoms'],to_atoms('10000'))
        async with self.db.access_transaction() as conn:
            await self.db._set_access(conn,'group',-101,'revoke',1,0)
            await self.db._set_access(conn,'group',-101,'approve_forever',1,0)
        self.assertEqual((await self.db.money_balance('group',-101))['anchor_at'],original['anchor_at'])

    async def test_migration_uses_proven_dates_without_retroactive_charges(self):
        # Simulate the existing pre-ledger schema on an artificial database.
        await self.db.close()
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM money_meta")
            conn.execute("INSERT INTO private_users(user_id,status,created_at,updated_at) VALUES(202,'revoked','2023-05-15T10:00:00+00:00','2023-05-15T10:00:00+00:00')")
            conn.execute("INSERT INTO private_users(user_id,status,created_at,updated_at) VALUES(203,'pending','invalid','invalid')")
        await self.db.open()
        b=await self.db.money_balance('user',202)
        self.assertEqual(b['anchor_at'],'2023-05-15T10:00:00+00:00')
        self.assertEqual(b['free_atoms'],to_atoms('1000'))
        self.assertEqual((await self.db.money_balance('user',203))['anchor_source'],'migration-new-anchor')
        rows=await (await self.db.db.execute("SELECT * FROM money_journal WHERE subject_id=202 AND event='monthly'")).fetchall()
        self.assertEqual(len(rows),1)


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_bot_and_unrecognised_group_commands_do_not_anchor(self):
        db=SimpleNamespace(touch_money_user=AsyncMock())
        data={'ctx':SimpleNamespace(db=db,config=SimpleNamespace(bot_username='unbazarbot'))}
        middleware=ExplicitMoneyInteraction();handler=AsyncMock()
        base=dict(message_id=1,date=datetime.now(timezone.utc),chat=Chat(id=-101,type='group'),
                  from_user=User(id=101,is_bot=False,first_name='Synthetic'))
        for text in ('/help@other_bot','/unknown'):
            await middleware(handler,Message(**base,text=text),data)
        db.touch_money_user.assert_not_awaited()
        await middleware(handler,Message(**base,text='/tr@unbazarbot'),data)
        db.touch_money_user.assert_awaited_once_with(101)

    async def test_passive_group_voice_does_not_anchor_but_commands_and_private_do(self):
        db=SimpleNamespace(touch_money_user=AsyncMock())
        data={'ctx':SimpleNamespace(db=db)}
        handler=AsyncMock(return_value='handled')
        base=dict(message_id=1,date=datetime.now(timezone.utc),from_user=User(id=101,is_bot=False,first_name='Synthetic'))
        message=Message(**base,chat=Chat(id=-101,type='group'),voice=Voice(file_id='safe',file_unique_id='safe',duration=1))
        middleware=ExplicitMoneyInteraction()
        await middleware(handler,message,data);db.touch_money_user.assert_not_awaited()
        command=Message(**base,chat=message.chat,text='/help')
        await middleware(handler,command,data);db.touch_money_user.assert_awaited_once_with(101)
        db.touch_money_user.reset_mock()
        private=Message(**base,chat=Chat(id=101,type='private'),text='hello')
        await middleware(handler,private,data);db.touch_money_user.assert_awaited_once_with(101)
