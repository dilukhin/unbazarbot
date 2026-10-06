from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class BudgetLimits:
    requests_per_minute_chat: int = 4
    requests_per_minute_user: int = 2
    daily_minutes_total: int = 120
    daily_minutes_chat: int = 60
    daily_minutes_user: int = 30
    max_inflight: int = 2


class PaidStore:
    async def init_paid_schema(self):
        async with self.access_transaction() as conn:
            await conn.execute('''CREATE TABLE IF NOT EXISTS paid_calls(
                file_unique_id TEXT NOT NULL, model_alias TEXT NOT NULL,
                job_id TEXT NOT NULL, chat_id INTEGER NOT NULL, user_id INTEGER,
                started_at TEXT NOT NULL, reserved_seconds INTEGER NOT NULL,
                state TEXT NOT NULL, cost REAL,
                PRIMARY KEY(file_unique_id,model_alias))''')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_paid_time ON paid_calls(started_at)')
            await conn.execute('CREATE TABLE IF NOT EXISTS paid_meta(key TEXT PRIMARY KEY)')
            if not await (await conn.execute("SELECT 1 FROM paid_meta WHERE key='legacy_import'")).fetchone():
                await conn.execute("""INSERT OR IGNORE INTO paid_calls
                    SELECT file_unique_id,model_alias,id,COALESCE(chat_id,0),user_id,
                    created_at,0,'interrupted',cost FROM transcription_jobs
                    WHERE status IN ('running','error','interrupted')
                    AND file_unique_id IS NOT NULL AND model_alias IS NOT NULL""")
                await conn.execute("INSERT INTO paid_meta VALUES('legacy_import')")

    async def recover_paid_calls(self):
        # Вызывать только после получения блокировки единственного процесса.
        async with self.access_transaction() as conn:
            await conn.execute("UPDATE transcription_jobs SET status='interrupted',error='Процесс прерван; исход оплаты неизвестен' WHERE id IN (SELECT job_id FROM paid_calls WHERE state='running')")
            await conn.execute("UPDATE paid_calls SET state='interrupted' WHERE state='running'")

    async def reserve_paid_call(self, *, job_id, chat_id, user_id, private,
                                file_unique_id, model_alias, seconds, limits,
                                allow_private_admins=True, require_auto=False, at=None):
        stamp = at or datetime.now(timezone.utc)
        if stamp.tzinfo is None:
            raise ValueError('Timezone is required')
        stamp = stamp.astimezone(timezone.utc)
        minute = (stamp - timedelta(minutes=1)).isoformat()
        day = stamp.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        seconds = max(1, int(seconds))
        async with self.access_transaction() as conn:
            if private:
                admin = await self._admin_in_transaction(conn, user_id)
                row = await (await conn.execute('SELECT status FROM private_users WHERE user_id=?', (user_id,))).fetchone()
                allowed = allow_private_admins if admin else bool(row and row['status']=='approved')
                once = False
            else:
                row = await (await conn.execute('SELECT status,remaining_jobs,auto_enabled FROM groups WHERE chat_id=?', (chat_id,))).fetchone()
                once = bool(row and row['status']=='approved_once' and row['remaining_jobs']>0)
                allowed = bool(row and (row['status']=='approved' or once))
                if require_auto:
                    allowed = bool(row and row['status']=='approved' and row['auto_enabled'])
            if not allowed:
                return 'Доступ отозван или одноразовое разрешение исчерпано.'
            old = await (await conn.execute('SELECT state FROM paid_calls WHERE file_unique_id=? AND model_alias=?', (file_unique_id,model_alias))).fetchone()
            if old:
                return ('Этот файл уже распознаётся или результат готов. Повторите запрос позже.' if old['state'] in {'running','done'} else
                        'Предыдущая попытка этого файла завершилась с ошибкой или неизвестным исходом оплаты. Автоматический повтор запрещён; обратитесь к администратору.')
            inflight = (await (await conn.execute("SELECT COUNT(*) FROM paid_calls WHERE state='running'")).fetchone())[0]
            if inflight >= limits.max_inflight:
                return 'Бот занят распознаванием. Повторите запрос позже.'
            for condition, params, rate, daily, label in (
                ('chat_id=?', [chat_id], limits.requests_per_minute_chat, limits.daily_minutes_chat, 'чата'),
                ('user_id=?', [user_id], limits.requests_per_minute_user, limits.daily_minutes_user, 'пользователя'),
                ('1=1', [], None, limits.daily_minutes_total, 'бота'),
            ):
                if condition=='user_id=?' and user_id is None:
                    continue
                if rate is not None:
                    count = (await (await conn.execute(f'SELECT COUNT(*) FROM paid_calls WHERE {condition} AND started_at>?', params+[minute])).fetchone())[0]
                    if count >= rate:
                        return f'Достигнут минутный лимит запросов {label}. Повторите позже.'
                used = (await (await conn.execute(f'SELECT COALESCE(SUM(reserved_seconds),0) FROM paid_calls WHERE {condition} AND started_at>=?', params+[day])).fetchone())[0]
                if used+seconds > daily*60:
                    return f'Достигнут суточный лимит минут {label}. Сброс — в 00:00 UTC.'
            await conn.execute('INSERT INTO paid_calls VALUES(?,?,?,?,?,?,?,?,NULL)',
                               (file_unique_id,model_alias,job_id,chat_id,user_id,stamp.isoformat(),seconds,'running'))
            if once:
                await conn.execute("UPDATE groups SET remaining_jobs=remaining_jobs-1,status=CASE WHEN remaining_jobs=1 THEN 'revoked' ELSE 'approved_once' END,revision=revision+1 WHERE chat_id=?", (chat_id,))
            return None

    async def finish_paid_call(self, job_id, state, cost=None):
        if state not in {'done','error','interrupted'}:
            raise ValueError('Invalid payment state')
        async with self.access_transaction() as conn:
            await conn.execute('UPDATE paid_calls SET state=?,cost=? WHERE job_id=? AND state=\'running\'', (state,cost,job_id))

    async def complete_paid_job(self, job_id, text, cost, duration_seconds):
        async with self.access_transaction() as conn:
            await conn.execute("UPDATE transcription_jobs SET status='done',transcript=?,cost=?,duration_seconds=?,updated_at=? WHERE id=?",
                               (text,cost,duration_seconds,datetime.now(timezone.utc).isoformat(),job_id))
            await conn.execute("UPDATE paid_calls SET state='done',cost=? WHERE job_id=? AND state='running'", (cost,job_id))

    async def usage_today(self, at=None):
        stamp = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        day = stamp.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        rows = await (await self.db.execute('''SELECT chat_id,model_alias,COUNT(*) AS calls,
            SUM(reserved_seconds) AS seconds,SUM(COALESCE(cost,0)) AS known_cost,
            SUM(cost IS NULL) AS unknown_cost FROM paid_calls WHERE started_at>=?
            GROUP BY chat_id,model_alias ORDER BY chat_id,model_alias''', (day,))).fetchall()
        return [dict(row) for row in rows]
