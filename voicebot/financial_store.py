"""One SQLite transaction for access, duplicate, money and global cost limits."""
from datetime import datetime, timezone
import hashlib

from .money import MoneyError, to_atoms
from .provider_costs import exact_cost, generation_id, GenerationCost


class FinancialStore:
    async def _financial_global_used(self, conn, day):
        rows=await (await conn.execute("SELECT bound_atoms,provider_atoms,state FROM financial_calls WHERE created_at>=? AND state NOT IN ('not-sent','legacy')",(day,))).fetchall()
        if any(r['state']=='overrun' and r['provider_atoms'] is None for r in rows):
            raise MoneyError('Общий бюджет приостановлен до сверки перерасхода.')
        return sum(r['provider_atoms'] if r['state']=='settled' else max(r['bound_atoms'],r['provider_atoms'] or 0) for r in rows)

    async def init_financial_schema(self):
        async with self.access_transaction() as conn:
            await conn.execute('''CREATE TABLE IF NOT EXISTS financial_calls(
                job_id TEXT PRIMARY KEY, file_unique_id TEXT NOT NULL, provider_model TEXT NOT NULL,
                kind TEXT, subject_id INTEGER, bound_rub TEXT NOT NULL, bound_atoms INTEGER NOT NULL,
                snapshot_id TEXT, checked_at TEXT, state TEXT NOT NULL, created_at TEXT NOT NULL,
                usage_rub TEXT, generation_id TEXT, generation_rub TEXT, final_rub TEXT,
                provider_atoms INTEGER, one_time_consumed INTEGER NOT NULL DEFAULT 0)''')
            await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS financial_file_model ON financial_calls(file_unique_id,provider_model) WHERE state!='not-sent'")
            await conn.execute('CREATE INDEX IF NOT EXISTS financial_time ON financial_calls(created_at)')
            await conn.execute('CREATE TABLE IF NOT EXISTS frozen_models(provider_model TEXT PRIMARY KEY,reason TEXT NOT NULL,job_id TEXT NOT NULL,created_at TEXT NOT NULL)')
            for name,definition in (('extended_required','INTEGER NOT NULL DEFAULT 0'),('extended_revision','INTEGER')):
                columns=await (await conn.execute('PRAGMA table_info(financial_calls)')).fetchall()
                if name not in {r['name'] for r in columns}:
                    await conn.execute(f'ALTER TABLE financial_calls ADD COLUMN {name} {definition}')
            columns=await (await conn.execute('PRAGMA table_info(transcription_jobs)')).fetchall()
            if 'cost_rub' not in {r['name'] for r in columns}:
                await conn.execute('ALTER TABLE transcription_jobs ADD COLUMN cost_rub TEXT')
            if not await (await conn.execute("SELECT 1 FROM money_meta WHERE key='financial-legacy-v1'")).fetchone():
                await conn.execute('''INSERT OR IGNORE INTO financial_calls(job_id,file_unique_id,provider_model,bound_rub,bound_atoms,state,created_at)
                    SELECT p.job_id,p.file_unique_id,COALESCE(j.provider_model,'legacy-unknown'), '0',0,'legacy',p.started_at
                    FROM paid_calls p LEFT JOIN transcription_jobs j ON j.id=p.job_id''')
                await conn.execute("INSERT INTO money_meta VALUES('financial-legacy-v1')")

    async def reserve_financial_call(self, *, quote, provider_model, require_extended=False, **kwargs):
        now=kwargs.get('at') or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise MoneyError('Timezone required')
        now=now.astimezone(timezone.utc)
        job_id,user_id,chat_id=kwargs['job_id'],kwargs['user_id'],kwargs['chat_id']
        kind,subject=('group',chat_id) if kwargs.get('require_auto') else ('user',user_id)
        try:
            async with self.access_transaction() as conn:
                if kind=='user' and (type(user_id) is not int or user_id<=0):
                    raise MoneyError('Нельзя определить личный счёт отправителя.')
                self._money_subject(kind,subject)
                extended_revision=None
                if require_extended:
                    if kwargs.get('require_auto') or not await self.extended_audio_allowed(user_id,conn=conn):
                        raise MoneyError('Отдельное разрешение /trf недоступно.')
                    extended_revision=(await (await conn.execute('SELECT revision FROM private_users WHERE user_id=?',(user_id,))).fetchone())[0]
                estimate=quote()
                if not estimate.available or estimate.max_rub is None:
                    raise MoneyError('Проверенная оценка стоимости недоступна.')
                if await (await conn.execute('SELECT 1 FROM frozen_models WHERE provider_model=?',(provider_model,))).fetchone():
                    raise MoneyError('Модель приостановлена до проверки стоимости.')
                if await (await conn.execute("SELECT 1 FROM financial_calls WHERE file_unique_id=? AND (provider_model=? OR provider_model='legacy-unknown') AND state!='not-sent'",(kwargs['file_unique_id'],provider_model))).fetchone():
                    raise MoneyError('Предыдущая попытка этого файла уже учтена; повторная оплата запрещена.')
                once=False
                if not kwargs['private']:
                    row=await (await conn.execute('SELECT status FROM groups WHERE chat_id=?',(chat_id,))).fetchone()
                    once=bool(row and row['status']=='approved_once')
                storage_key='routerai:'+hashlib.sha256(provider_model.encode('utf-8')).hexdigest()
                legacy_kwargs={**kwargs,'model_alias':storage_key}
                reason=await self.reserve_paid_call(**legacy_kwargs,conn=conn)
                if reason:
                    return reason
                await conn.execute('UPDATE paid_calls SET display_alias=? WHERE job_id=?',(kwargs['model_alias'],job_id))
                bound=to_atoms(estimate.max_rub,rub=True)
                day=now.replace(hour=0,minute=0,second=0,microsecond=0).isoformat()
                used=await self._financial_global_used(conn,day)
                if used+bound>to_atoms(self.money_settings.global_daily_rub,rub=True):
                    raise MoneyError('Достигнут общий денежный лимит бота.')
                if kind=='user':
                    await self._money_account(conn,kind,subject,now,'explicit-paid-call')
                await self.reserve_money(job_id,kind,subject,estimate.max_rub,at=now,conn=conn)
                await conn.execute('''INSERT INTO financial_calls(job_id,file_unique_id,provider_model,kind,subject_id,bound_rub,bound_atoms,snapshot_id,checked_at,state,created_at,one_time_consumed)
                    VALUES(?,?,?,?,?,?,?,?,?,'reserved',?,?)''',
                    (job_id,kwargs['file_unique_id'],provider_model,kind,subject,format(estimate.max_rub,'f'),bound,
                     estimate.snapshot_id,estimate.checked_at.isoformat() if estimate.checked_at else None,now.isoformat(),int(once)))
                await conn.execute('UPDATE financial_calls SET extended_required=?,extended_revision=? WHERE job_id=?',(int(require_extended),extended_revision,job_id))
                return None
        except MoneyError as exc:
            return 'Недостаточно базариков для предварительного резерва.' if str(exc)=='Insufficient balance' else str(exc)

    async def financial_call(self, job_id):
        row=await (await self.db.execute('SELECT * FROM financial_calls WHERE job_id=?',(job_id,))).fetchone()
        return dict(row) if row else None

    async def financial_diagnostics(self):
        day=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).isoformat()
        rows=await (await self.db.execute("SELECT state,COUNT(*) AS count FROM financial_calls WHERE created_at>=? AND state!='legacy' GROUP BY state",(day,))).fetchall()
        frozen=await (await self.db.execute('SELECT provider_model FROM frozen_models ORDER BY created_at LIMIT 10')).fetchall()
        return {row['state']:row['count'] for row in rows},[row[0] for row in frozen]

    async def mark_financial_sending(self, job_id, quote, allow_private_admins):
        async with self.access_transaction() as conn:
            row=await (await conn.execute('SELECT * FROM financial_calls WHERE job_id=?',(job_id,))).fetchone()
            estimate=quote()
            if not row or row['state'] not in {'reserved','sending'} or not estimate.available or estimate.max_rub is None or estimate.max_rub>exact_cost(row['bound_rub']):
                raise MoneyError('Tariff changed before send')
            if datetime.fromisoformat(row['created_at']).astimezone(timezone.utc).date()!=datetime.now(timezone.utc).date():
                raise MoneyError('Global billing day changed before send')
            day=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).isoformat()
            if await self._financial_global_used(conn,day)>to_atoms(self.money_settings.global_daily_rub,rub=True):
                raise MoneyError('Global budget changed before send')
            if await (await conn.execute('SELECT 1 FROM frozen_models WHERE provider_model=?',(row['provider_model'],))).fetchone():
                raise MoneyError('Model frozen')
            if not await self.formatting_access_allowed(job_id,allow_private_admins,conn=conn):
                raise MoneyError('Access changed before send')
            if row['extended_required']:
                owner=await (await conn.execute('SELECT revision,trf_allowed FROM private_users WHERE user_id=?',(row['subject_id'],))).fetchone()
                if not owner or not owner['trf_allowed'] or owner['revision']!=row['extended_revision']:
                    raise MoneyError('Extended permission changed before send')
            await conn.execute("UPDATE financial_calls SET state='sending' WHERE job_id=?",(job_id,))

    async def adjust_reserved_seconds(self, job_id, seconds, limits):
        async with self.access_transaction() as conn:
            row=await (await conn.execute('SELECT * FROM paid_calls WHERE job_id=?',(job_id,))).fetchone()
            if not row or row['state']!='running' or type(seconds) is not int or seconds<1:
                raise MoneyError('Audio reservation unavailable')
            day=datetime.fromisoformat(row['started_at']).replace(hour=0,minute=0,second=0,microsecond=0).isoformat()
            for condition,params,cap in (('chat_id=?',[row['chat_id']],limits.daily_minutes_chat),
                    ('user_id=?',[row['user_id']],limits.daily_minutes_user),('1=1',[],limits.daily_minutes_total)):
                if condition=='user_id=?' and row['user_id'] is None:continue
                used=(await (await conn.execute(f'SELECT COALESCE(SUM(reserved_seconds),0) FROM paid_calls WHERE {condition} AND started_at>=? AND job_id!=?',params+[day,job_id])).fetchone())[0]
                if used+seconds>cap*60:raise MoneyError('Фактическая длительность превышает доступный минутный бюджет.')
            await conn.execute('UPDATE paid_calls SET reserved_seconds=? WHERE job_id=?',(seconds,job_id))

    async def release_financial_not_sent(self, job_id, *, proven_not_sent=False):
        async with self.access_transaction() as conn:
            row=await (await conn.execute('SELECT * FROM financial_calls WHERE job_id=?',(job_id,))).fetchone()
            if not row or row['state'] not in ({'reserved','sending'} if proven_not_sent else {'reserved'}):
                return False
            await self.release_money(job_id,conn=conn)
            paid=await (await conn.execute('SELECT * FROM paid_calls WHERE job_id=?',(job_id,))).fetchone()
            if paid and row['one_time_consumed']:
                await conn.execute("UPDATE groups SET remaining_jobs=remaining_jobs+1,status='approved_once',revision=revision+1 WHERE chat_id=? AND revision=? AND status IN ('revoked','approved_once')",(paid['chat_id'],paid['access_revision']))
            await conn.execute('DELETE FROM paid_calls WHERE job_id=?',(job_id,))
            await conn.execute("UPDATE financial_calls SET state='not-sent' WHERE job_id=?",(job_id,))
            return True

    async def record_financial_response(self, job_id, result):
        cost=exact_cost(result.cost)
        identity=generation_id(getattr(result,'generation_id',None))
        async with self.access_transaction() as conn:
            await conn.execute("UPDATE transcription_jobs SET status='done',transcript=?,duration_seconds=? WHERE id=?",(result.text,result.duration_seconds,job_id))
            await conn.execute("UPDATE paid_calls SET state='done' WHERE job_id=?",(job_id,))
            await conn.execute("UPDATE financial_calls SET state='reported',usage_rub=?,generation_id=? WHERE job_id=? AND state IN ('reserved','sending')",(format(cost,'f') if cost is not None else None,identity,job_id))

    async def finalize_financial_response(self, job_id, generation=GenerationCost('unavailable')):
        async with self.access_transaction() as conn:
            row=await (await conn.execute('SELECT * FROM financial_calls WHERE job_id=?',(job_id,))).fetchone()
            if not row or row['state']=='settled':
                return
            usage=exact_cost(row['usage_rub'])
            remote=exact_cost(generation.cost) if generation.status=='verified' else None
            observed=[v for v in (usage,remote) if v is not None]
            overrun=any(v>exact_cost(row['bound_rub']) for v in observed)
            provider_atoms=None
            if observed:
                try:
                    provider_atoms=to_atoms(max(observed),rub=True)
                except MoneyError:
                    overrun=True
            state='mismatch' if generation.status=='mismatch' or (usage is not None and remote is not None and usage!=remote) else 'unknown'
            cost=remote if remote is not None else usage
            if overrun:
                state='overrun'
                await conn.execute('INSERT OR IGNORE INTO frozen_models VALUES(?,?,?,?)',(row['provider_model'],'cost-exceeded-reserve',job_id,datetime.now(timezone.utc).isoformat()))
            elif state!='mismatch' and cost is not None:
                await self.settle_money(job_id,cost,conn=conn)
                state='settled'
                await conn.execute('UPDATE transcription_jobs SET cost_rub=? WHERE id=?',(format(cost,'f'),job_id))
            await conn.execute('UPDATE financial_calls SET state=?,generation_rub=?,final_rub=?,provider_atoms=? WHERE job_id=?',
                               (state,format(remote,'f') if remote is not None else None,format(cost,'f') if state=='settled' else None,provider_atoms,job_id))

    async def interrupt_financial_call(self, job_id, identity=None):
        async with self.access_transaction() as conn:
            await conn.execute("UPDATE financial_calls SET state='unknown',generation_id=COALESCE(?,generation_id) WHERE job_id=? AND state IN ('sending','reserved','reported')",(generation_id(identity),job_id))

    async def reconcile_financial_call(self, job_id, transcriber):
        """Bounded metadata-only reconciliation; never resends audio."""
        row=await self.financial_call(job_id)
        if row and row['state'] in {'unknown','mismatch','overrun'} and row['generation_id']:
            details=await transcriber.generation_cost(row['generation_id'],row['provider_model'])
            await self.finalize_financial_response(job_id,details)
        return await self.financial_call(job_id)

    async def recover_financial_calls(self):
        rows=await (await self.db.execute("SELECT job_id FROM financial_calls WHERE state='reserved'")).fetchall()
        for row in rows:
            await self.release_financial_not_sent(row['job_id'])
        async with self.access_transaction() as conn:
            await conn.execute("UPDATE financial_calls SET state='unknown' WHERE state IN ('sending','reported')")
