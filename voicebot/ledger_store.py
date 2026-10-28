"""Transactional balances; paid provider integration belongs to #20."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .money import MAX_ATOMS, MoneyError, aware, decimal_amount, period, to_atoms


def stamp(at=None):
    return aware(at or datetime.now(timezone.utc)).isoformat(timespec='seconds')


class LedgerStore:
    async def init_money_schema(self):
        async with self.access_transaction() as conn:
            for sql in (
                '''CREATE TABLE IF NOT EXISTS money_accounts(
                    kind TEXT NOT NULL CHECK(kind IN ('user','group')), subject_id INTEGER NOT NULL,
                    anchor_at TEXT NOT NULL, anchor_source TEXT NOT NULL, last_seen TEXT NOT NULL,
                    monthly_atoms INTEGER NOT NULL CHECK(typeof(monthly_atoms)='integer' AND monthly_atoms BETWEEN 0 AND 1000000000000000000),
                    extra_atoms INTEGER NOT NULL DEFAULT 0 CHECK(typeof(extra_atoms)='integer' AND extra_atoms BETWEEN 0 AND 1000000000000000000),
                    PRIMARY KEY(kind,subject_id))''',
                '''CREATE TABLE IF NOT EXISTS money_periods(
                    kind TEXT NOT NULL, subject_id INTEGER NOT NULL, starts TEXT NOT NULL, ends TEXT NOT NULL,
                    free_atoms INTEGER NOT NULL CHECK(typeof(free_atoms)='integer' AND free_atoms BETWEEN 0 AND 1000000000000000000),
                    PRIMARY KEY(kind,subject_id,starts))''',
                '''CREATE TABLE IF NOT EXISTS money_reservations(
                    reservation_id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject_id INTEGER NOT NULL,
                    period_start TEXT NOT NULL, free_atoms INTEGER NOT NULL CHECK(free_atoms>=0),
                    extra_atoms INTEGER NOT NULL CHECK(extra_atoms>=0), bound_rub TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('held','settled','released')),
                    actual_rub TEXT, charged_atoms INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS money_journal(
                    operation_id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject_id INTEGER NOT NULL,
                    event TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)''',
                '''CREATE INDEX IF NOT EXISTS money_hold_subject ON money_reservations(kind,subject_id,state)''',
                '''CREATE TABLE IF NOT EXISTS money_meta(key TEXT PRIMARY KEY)''',
            ):
                await conn.execute(sql)
            if not await (await conn.execute("SELECT 1 FROM money_meta WHERE key='anchors-v1'")).fetchone():
                now = stamp()
                # No legacy floating-point costs are imported or charged retroactively.
                users = await (await conn.execute('SELECT user_id,created_at FROM private_users')).fetchall()
                for row in users:
                    await self._money_import(conn, 'user', row['user_id'], row['created_at'], 'private_users.created_at', now)
                # Audit can prove an earlier persistent approval than groups.approved_at.
                known = {}
                rows = await (await conn.execute("SELECT chat_id,created_at,details_json FROM audit_log WHERE action='approve_forever' ORDER BY created_at")).fetchall()
                for row in rows:
                    try:
                        details = json.loads(row['details_json'] or '{}')
                        if details.get('subject_type') == 'group':
                            moment = aware(datetime.fromisoformat(row['created_at']))
                            if moment <= datetime.fromisoformat(now):
                                previous = known.get(row['chat_id'])
                                if previous is None or moment < aware(datetime.fromisoformat(previous)):
                                    known[row['chat_id']] = row['created_at']
                    except (ValueError, TypeError, AttributeError):
                        continue
                groups = await (await conn.execute("SELECT chat_id,approved_at FROM groups WHERE approval_type='forever' OR chat_id IN (SELECT chat_id FROM audit_log WHERE action='approve_forever')")).fetchall()
                for row in groups:
                    source = 'audit.persistent-approval' if row['chat_id'] in known else 'groups.approved_at'
                    await self._money_import(conn, 'group', row['chat_id'], known.get(row['chat_id'], row['approved_at']), source, now)
                await conn.execute("INSERT INTO money_meta VALUES('anchors-v1')")

    async def _money_import(self, conn, kind, subject_id, value, source, now):
        try:
            at = aware(datetime.fromisoformat(value))
            if at > datetime.fromisoformat(now):
                raise MoneyError('Future anchor')
        except (ValueError, TypeError):
            at, source = datetime.fromisoformat(now), 'migration-new-anchor'
        await self._money_account(conn, kind, subject_id, at, source)

    @staticmethod
    def _money_subject(kind, subject_id):
        if kind not in {'user','group'} or type(subject_id) is not int or (subject_id <= 0 if kind == 'user' else subject_id >= 0):
            raise MoneyError('Invalid money subject')

    async def _money_account(self, conn, kind, subject_id, at, source):
        self._money_subject(kind, subject_id)
        now = stamp(at)
        await conn.execute('''INSERT OR IGNORE INTO money_accounts(kind,subject_id,anchor_at,anchor_source,last_seen,monthly_atoms)
            VALUES(?,?,?,?,?,?)''', (kind,subject_id,now,source,now,self.money_settings.monthly(kind)))

    async def touch_money_user(self, user_id, *, at=None):
        async with self.access_transaction() as conn:
            await self._money_account(conn, 'user', user_id, at, 'explicit-interaction')
            return await self._money_roll(conn, 'user', user_id, at)

    async def _money_entry(self, conn, op, kind, subject_id, event, payload, now):
        if not isinstance(op, str) or not 1 <= len(op) <= 200:
            raise MoneyError('Invalid operation ID')
        raw = json.dumps(payload, sort_keys=True, separators=(',',':'), ensure_ascii=False)
        old = await (await conn.execute('SELECT * FROM money_journal WHERE operation_id=?',(op,))).fetchone()
        if old:
            if (old['kind'],old['subject_id'],old['event'],old['payload_json']) != (kind,subject_id,event,raw):
                raise MoneyError('Operation ID reused with different data')
            return False
        await conn.execute('INSERT INTO money_journal VALUES(?,?,?,?,?,?)',(op,kind,subject_id,event,raw,now))
        return True

    async def _money_roll(self, conn, kind, subject_id, at=None):
        self._money_subject(kind,subject_id)
        now = stamp(at)
        account = await (await conn.execute('SELECT * FROM money_accounts WHERE kind=? AND subject_id=?',(kind,subject_id))).fetchone()
        if not account:
            raise MoneyError('Money anchor not established')
        if now < account['last_seen']:
            raise MoneyError('Ledger clock moved backwards')
        start,end = period(datetime.fromisoformat(account['anchor_at']),datetime.fromisoformat(now))
        old = await (await conn.execute('SELECT * FROM money_periods WHERE kind=? AND subject_id=? AND starts<? AND free_atoms>0',(kind,subject_id,start))).fetchall()
        for row in old:
            await self._money_entry(conn,f'expire:{kind}:{subject_id}:{row["starts"]}',kind,subject_id,'expire',{'atoms':row['free_atoms'],'period':row['starts']},now)
        await conn.execute('UPDATE money_periods SET free_atoms=0 WHERE kind=? AND subject_id=? AND starts<?',(kind,subject_id,start))
        exists = await (await conn.execute('SELECT 1 FROM money_periods WHERE kind=? AND subject_id=? AND starts=?',(kind,subject_id,start))).fetchone()
        if not exists:
            await conn.execute('INSERT INTO money_periods VALUES(?,?,?,?,?)',(kind,subject_id,start,end,account['monthly_atoms']))
            await self._money_entry(conn,f'grant:{kind}:{subject_id}:{start}',kind,subject_id,'monthly',{'atoms':account['monthly_atoms'],'period':start},now)
        await conn.execute('UPDATE money_accounts SET last_seen=? WHERE kind=? AND subject_id=?',(now,kind,subject_id))
        return start,end

    async def money_balance(self, kind, subject_id, *, at=None):
        async with self.access_transaction() as conn:
            return await self._money_balance(conn,kind,subject_id,at)

    async def _money_balance(self, conn, kind, subject_id, at=None):
        start,end = await self._money_roll(conn,kind,subject_id,at)
        account = dict(await (await conn.execute('SELECT * FROM money_accounts WHERE kind=? AND subject_id=?',(kind,subject_id))).fetchone())
        free = (await (await conn.execute('SELECT free_atoms FROM money_periods WHERE kind=? AND subject_id=? AND starts=?',(kind,subject_id,start))).fetchone())[0]
        holds = await (await conn.execute("SELECT free_atoms,extra_atoms FROM money_reservations WHERE kind=? AND subject_id=? AND state='held'",(kind,subject_id))).fetchall()
        return {**account,'period_start':start,'period_end':end,'free_atoms':free,
                'held_atoms':sum(r['free_atoms']+r['extra_atoms'] for r in holds)}

    async def reserve_money(self, reservation_id, kind, subject_id, bound_rub, *, at=None, conn=None):
        if not isinstance(reservation_id, str) or not 1 <= len(reservation_id) <= 160:
            raise MoneyError('Invalid reservation ID')
        if conn is None:
            async with self.access_transaction() as tx:
                return await self.reserve_money(reservation_id,kind,subject_id,bound_rub,at=at,conn=tx)
        rub,atoms = format(decimal_amount(bound_rub),'f'),to_atoms(bound_rub,rub=True)
        balance = await self._money_balance(conn,kind,subject_id,at)
        now = stamp(at)
        payload = {'bound_rub':rub,'atoms':atoms}
        old = await (await conn.execute('SELECT * FROM money_reservations WHERE reservation_id=?',(reservation_id,))).fetchone()
        if old:
            if (old['kind'],old['subject_id'],old['bound_rub']) != (kind,subject_id,rub):
                raise MoneyError('Reservation ID reused with different data')
            return dict(old)
        if atoms > balance['free_atoms']+balance['extra_atoms']:
            raise MoneyError('Insufficient balance')
        if atoms+balance['held_atoms'] > MAX_ATOMS:
            raise MoneyError('Outstanding reserve exceeds ledger capacity')
        await self._money_entry(conn,'reserve:'+reservation_id,kind,subject_id,'reserve',payload,now)
        free,extra = min(atoms,balance['free_atoms']),max(0,atoms-balance['free_atoms'])
        await conn.execute('UPDATE money_periods SET free_atoms=free_atoms-? WHERE kind=? AND subject_id=? AND starts=?',(free,kind,subject_id,balance['period_start']))
        await conn.execute('UPDATE money_accounts SET extra_atoms=extra_atoms-? WHERE kind=? AND subject_id=?',(extra,kind,subject_id))
        await conn.execute('''INSERT INTO money_reservations VALUES(?,?,?,?,?,?,?,'held',NULL,NULL,?,?)''',
                           (reservation_id,kind,subject_id,balance['period_start'],free,extra,rub,now,now))
        return dict(await (await conn.execute('SELECT * FROM money_reservations WHERE reservation_id=?',(reservation_id,))).fetchone())

    async def settle_money(self, reservation_id, actual_rub, *, at=None, conn=None):
        return await self._finish_money(reservation_id,actual_rub,False,at,conn)

    async def release_money(self, reservation_id, *, at=None, conn=None):
        """Only call after proving that the provider request was not sent (#20)."""
        return await self._finish_money(reservation_id,'0',True,at,conn)

    async def _finish_money(self, reservation_id, actual_rub, release, at, conn):
        if conn is None:
            async with self.access_transaction() as tx:
                return await self._finish_money(reservation_id,actual_rub,release,at,tx)
        rub,charged = format(decimal_amount(actual_rub),'f'),to_atoms(actual_rub,rub=True)
        row = await (await conn.execute('SELECT * FROM money_reservations WHERE reservation_id=?',(reservation_id,))).fetchone()
        if not row:
            raise MoneyError('Unknown reservation')
        start,_ = await self._money_roll(conn,row['kind'],row['subject_id'],at)
        state = 'released' if release else 'settled'
        if row['state'] != 'held':
            if (row['state'],row['actual_rub']) != (state,rub):
                raise MoneyError('Conflicting reservation finalization')
            return row['charged_atoms']
        if decimal_amount(actual_rub)>decimal_amount(row['bound_rub']) or charged>row['free_atoms']+row['extra_atoms']:
            raise MoneyError('Confirmed cost exceeds reserve; hold preserved')
        free_used = min(charged,row['free_atoms'])
        extra_used = charged-free_used
        refund_free,refund_extra = row['free_atoms']-free_used,row['extra_atoms']-extra_used
        if row['period_start'] == start:
            await conn.execute('UPDATE money_periods SET free_atoms=free_atoms+? WHERE kind=? AND subject_id=? AND starts=?',(refund_free,row['kind'],row['subject_id'],start))
        await conn.execute('UPDATE money_accounts SET extra_atoms=extra_atoms+? WHERE kind=? AND subject_id=?',(refund_extra,row['kind'],row['subject_id']))
        now = stamp(at)
        await self._money_entry(conn,'finish:'+reservation_id,row['kind'],row['subject_id'],state,
                                {'actual_rub':rub,'charged_atoms':charged,'released_free_atoms':refund_free if row['period_start']==start else 0,
                                 'expired_free_atoms':refund_free if row['period_start']!=start else 0,'released_extra_atoms':refund_extra},now)
        await conn.execute('UPDATE money_reservations SET state=?,actual_rub=?,charged_atoms=?,updated_at=? WHERE reservation_id=?',(state,rub,charged,now,reservation_id))
        return charged

    async def adjust_money(self, operation_id, kind, subject_id, amount_bazariki, *, admin_id, monthly=False, at=None):
        atoms = to_atoms(amount_bazariki)
        async with self.access_transaction() as conn:
            if not await self._admin_in_transaction(conn,admin_id):
                raise MoneyError('Administrator required')
            balance = await self._money_balance(conn,kind,subject_id,at)
            event = 'monthly-policy' if monthly else 'extra-grant'
            if not await self._money_entry(conn,'adjust:'+operation_id,kind,subject_id,event,{'atoms':atoms,'actor':admin_id},stamp(at)):
                return
            if monthly:
                await conn.execute('UPDATE money_accounts SET monthly_atoms=? WHERE kind=? AND subject_id=?',(atoms,kind,subject_id))
            else:
                holds = await (await conn.execute("SELECT extra_atoms FROM money_reservations WHERE kind=? AND subject_id=? AND state='held'",(kind,subject_id))).fetchall()
                if balance['extra_atoms']+atoms+sum(r[0] for r in holds)>MAX_ATOMS:
                    raise MoneyError('Balance exceeds ledger capacity')
                await conn.execute('UPDATE money_accounts SET extra_atoms=extra_atoms+? WHERE kind=? AND subject_id=?',(atoms,kind,subject_id))
