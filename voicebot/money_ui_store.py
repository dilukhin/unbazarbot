"""Owner-bound confirmations and private financial history for Telegram UI."""
import json
import secrets
from datetime import datetime, timedelta, timezone

from .money import MoneyError, aware, decimal_amount, to_atoms

ACTIONS={'monthly','current','extra','debit-current','debit-extra','trf-on','trf-off','request-close'}


class MoneyUIStore:
    async def init_money_ui_schema(self):
        async with self.access_transaction() as conn:
            await conn.execute('''CREATE TABLE IF NOT EXISTS money_intents(
                token TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                message_id INTEGER, kind TEXT NOT NULL, subject_id INTEGER NOT NULL,
                payload TEXT NOT NULL, expires_at TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0)''')
            await conn.execute('''CREATE TABLE IF NOT EXISTS money_requests(
                user_id INTEGER PRIMARY KEY, state TEXT NOT NULL, created_at TEXT NOT NULL,
                handled_by INTEGER, handled_at TEXT)''')

    async def create_money_intent(self, owner, chat, kind, subject, action, amount=None, *, revision=None, period_start=None, permission_revision=None, request_stamp=None, at=None):
        self._money_subject(kind,subject)
        if owner!=chat or action not in ACTIONS or ((action.startswith('trf-') or action=='request-close') and kind!='user'):
            raise MoneyError('Invalid financial action')
        now=aware(at or datetime.now(timezone.utc))
        payload={'action':action,'revision':revision,'period':period_start,'permission_revision':permission_revision,'request_stamp':request_stamp}
        if not action.startswith('trf-') and action!='request-close':
            atoms=to_atoms(amount)
            if action!='monthly' and atoms==0:raise MoneyError('Сумма должна быть положительной.')
            payload['amount']=format(decimal_amount(amount),'f')
        token=secrets.token_hex(10)
        async with self.access_transaction() as conn:
            if not await self._admin_in_transaction(conn,owner):raise MoneyError('Administrator required')
            await conn.execute('DELETE FROM money_intents WHERE expires_at<?',(now.isoformat(),))
            await conn.execute('INSERT INTO money_intents(token,owner_id,chat_id,kind,subject_id,payload,expires_at) VALUES(?,?,?,?,?,?,?)',
                    (token,owner,chat,kind,subject,json.dumps(payload,sort_keys=True),(now+timedelta(minutes=5)).isoformat()))
        return token

    async def bind_money_intent(self, token, message_id):
        async with self.access_transaction() as conn:
            await conn.execute('UPDATE money_intents SET message_id=? WHERE token=? AND message_id IS NULL',(message_id,token))

    async def cancel_money_intent(self, token, owner, chat, message_id):
        async with self.access_transaction() as conn:
            row=await (await conn.execute('SELECT * FROM money_intents WHERE token=? AND used=0',(token,))).fetchone()
            if not row or (row['owner_id'],row['chat_id'],row['message_id'])!=(owner,chat,message_id):return None
            await conn.execute('UPDATE money_intents SET used=1 WHERE token=?',(token,))
            return row['kind'],row['subject_id']

    async def cancel_private_choices(self, owner, chat):
        if owner!=chat:return
        async with self.access_transaction() as conn:
            await conn.execute('UPDATE money_intents SET used=1 WHERE owner_id=? AND chat_id=? AND used=0',(owner,chat))
            await conn.execute('UPDATE trf_choices SET used=1 WHERE owner_id=? AND chat_id=? AND used=0',(owner,chat))

    async def apply_money_intent(self, token, owner, chat, message_id, *, at=None):
        now=aware(at or datetime.now(timezone.utc))
        try:
            async with self.access_transaction() as conn:
                if not await self._admin_in_transaction(conn,owner):return {'status':'unavailable'}
                row=await (await conn.execute('SELECT * FROM money_intents WHERE token=?',(token,))).fetchone()
                if not row or row['used'] or row['expires_at']<=now.isoformat() or (row['owner_id'],row['chat_id'],row['message_id'])!=(owner,chat,message_id):
                    return {'status':'unavailable'}
                data=json.loads(row['payload'])
                action,kind,subject=data['action'],row['kind'],row['subject_id']
                if action not in ACTIONS:raise MoneyError('Invalid financial action')
                if action=='request-close':
                    request=await (await conn.execute("SELECT created_at FROM money_requests WHERE user_id=? AND state='pending'",(subject,))).fetchone()
                    if kind!='user' or not request or request[0]!=data['request_stamp']:raise MoneyError('Stale request')
                    await conn.execute("UPDATE money_requests SET state='closed',handled_by=?,handled_at=? WHERE user_id=?",(owner,now.isoformat(),subject))
                    await conn.execute('INSERT INTO audit_log(actor_user_id,action,target_id,created_at) VALUES(?,?,?,?)',(owner,'money-request-close',str(subject),now.isoformat()))
                elif action.startswith('trf-'):
                    if kind!='user':raise MoneyError('Invalid extended subject')
                    profile=await (await conn.execute('SELECT revision FROM private_users WHERE user_id=?',(subject,))).fetchone()
                    current=profile[0] if profile else -1
                    if current!=data['permission_revision']:raise MoneyError('Stale permission confirmation')
                    if not await self.set_extended_audio(subject,action=='trf-on',owner,current if profile else None,conn=conn):
                        raise MoneyError('Permission change refused')
                else:
                    balance=await self._money_balance(conn,kind,subject,now)
                    if balance['period_start']!=data['period']:raise MoneyError('Financial period changed')
                    await self.adjust_money('ui:'+token,kind,subject,data['amount'],admin_id=owner,
                            monthly=action=='monthly',period_only=action in {'current','debit-current'},
                            debit=action.startswith('debit-'),expected_revision=data['revision'],at=now,conn=conn)
                    if kind=='user' and action in {'current','extra'}:
                        await conn.execute("UPDATE money_requests SET state='handled',handled_by=?,handled_at=? WHERE user_id=? AND state='pending'",(owner,now.isoformat(),subject))
                await conn.execute('UPDATE money_intents SET used=1 WHERE token=?',(token,))
                return {'status':'applied','kind':kind,'subject_id':subject,**data}
        except (MoneyError,ValueError,KeyError,TypeError):
            return {'status':'stale'}

    async def money_history(self, kind, subject, page=0):
        self._money_subject(kind,subject)
        if type(page) is not int or not 0<=page<=10000:raise MoneyError('Invalid history page')
        rows=await (await self.db.execute('SELECT * FROM money_journal WHERE kind=? AND subject_id=? ORDER BY created_at DESC,rowid DESC LIMIT 11 OFFSET ?',(kind,subject,page*10))).fetchall()
        return [dict(r) for r in rows[:10]],len(rows)>10

    async def financial_history(self, kind, subject):
        self._money_subject(kind,subject)
        rows=await (await self.db.execute('SELECT job_id,provider_model,state,final_rub,bound_rub,created_at FROM financial_calls WHERE kind=? AND subject_id=? ORDER BY created_at DESC,rowid DESC LIMIT 5',(kind,subject))).fetchall()
        return [dict(r) for r in rows]

    async def money_subjects(self, kind, page=0):
        self._money_subject(kind,1 if kind=='user' else -1)
        if type(page) is not int or not 0<=page<=10000:raise MoneyError('Invalid subject page')
        table,key=('private_users','user_id') if kind=='user' else ('groups','chat_id')
        rows=await (await self.db.execute(f'''SELECT a.subject_id,p.title FROM money_accounts a
            LEFT JOIN {table} p ON p.{key}=a.subject_id WHERE a.kind=? ORDER BY a.subject_id LIMIT 9 OFFSET ?''',(kind,page*8))).fetchall()
        return [dict(r) for r in rows[:8]],len(rows)>8

    async def request_money_topup(self, user_id, *, at=None):
        self._money_subject('user',user_id)
        now=aware(at or datetime.now(timezone.utc))
        async with self.access_transaction() as conn:
            row=await (await conn.execute('SELECT * FROM money_requests WHERE user_id=?',(user_id,))).fetchone()
            if row and (row['state']=='pending' or (now-datetime.fromisoformat(row['created_at'])).total_seconds()<300):return False
            await conn.execute("INSERT INTO money_requests VALUES(?,'pending',?,NULL,NULL) ON CONFLICT(user_id) DO UPDATE SET state='pending',created_at=excluded.created_at,handled_by=NULL,handled_at=NULL",(user_id,now.isoformat()))
            await conn.execute('INSERT INTO audit_log(actor_user_id,action,target_id,created_at) VALUES(?,?,?,?)',(user_id,'money-request',str(user_id),now.isoformat()))
            return True

    async def pending_money_requests(self, page=0):
        if type(page) is not int or not 0<=page<=10000:raise MoneyError('Invalid request page')
        rows=await (await self.db.execute("SELECT user_id,created_at FROM money_requests WHERE state='pending' ORDER BY created_at LIMIT 9 OFFSET ?",(page*8,))).fetchall()
        return [dict(r) for r in rows[:8]],len(rows)>8

    async def money_request(self, user_id):
        row=await (await self.db.execute("SELECT * FROM money_requests WHERE user_id=? AND state='pending'",(user_id,))).fetchone()
        return dict(row) if row else None
