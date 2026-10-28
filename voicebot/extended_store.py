"""Separate /trf permission and short-lived owner-bound selection intents."""
import json
import secrets
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from .media import MediaRef
from .money import aware


class ExtendedStore:
    async def init_extended_schema(self):
        async with self.access_transaction() as conn:
            columns=await (await conn.execute('PRAGMA table_info(private_users)')).fetchall()
            if 'trf_allowed' not in {r['name'] for r in columns}:
                await conn.execute('ALTER TABLE private_users ADD COLUMN trf_allowed INTEGER NOT NULL DEFAULT 0')
            await conn.execute('''CREATE TABLE IF NOT EXISTS trf_choices(
                token TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                target_message_id INTEGER NOT NULL, menu_message_id INTEGER, model_alias TEXT NOT NULL,
                media_json TEXT NOT NULL, expires_at TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
                permission_revision INTEGER NOT NULL)''')

    async def extended_audio_allowed(self, user_id, conn=None):
        row=await (await (conn or self.db).execute('SELECT trf_allowed FROM private_users WHERE user_id=?',(user_id,))).fetchone()
        return bool(row and row[0])

    async def set_extended_audio(self, user_id, allowed, admin_id, expected_revision=None):
        if type(user_id) is not int or user_id<=0 or type(allowed) is not bool:
            raise ValueError('Invalid extended permission')
        async with self.access_transaction() as conn:
            if not await self._admin_in_transaction(conn,admin_id):return False
            row=await (await conn.execute('SELECT revision FROM private_users WHERE user_id=?',(user_id,))).fetchone()
            if expected_revision is not None and (not row or row[0]!=expected_revision):return False
            now=datetime.now(timezone.utc).isoformat(timespec='seconds')
            if row is None:
                await conn.execute("INSERT INTO private_users(user_id,created_at,updated_at) VALUES(?,?,?)",(user_id,now,now))
            await conn.execute('UPDATE private_users SET trf_allowed=?,revision=revision+1,updated_at=? WHERE user_id=?',(int(allowed),now,user_id))
            await conn.execute('INSERT INTO audit_log(actor_user_id,action,chat_id,target_id,details_json,created_at) VALUES(?,?,?,?,?,?)',
                               (admin_id,'extended-audio',user_id,str(user_id),json.dumps({'allowed':allowed}),now))
            return True

    async def create_trf_choice(self, owner, chat, target, alias, media, *, at=None):
        now=aware(at or datetime.now(timezone.utc))
        token=secrets.token_hex(10)
        data=asdict(media)
        if data['file_name']:data['file_name']=data['file_name'][:200]
        async with self.access_transaction() as conn:
            permission=await (await conn.execute('SELECT revision,trf_allowed FROM private_users WHERE user_id=?',(owner,))).fetchone()
            if not permission or not permission['trf_allowed']:raise ValueError('Extended permission required')
            await conn.execute('DELETE FROM trf_choices WHERE expires_at<?',(now.isoformat(),))
            await conn.execute('INSERT INTO trf_choices(token,owner_id,chat_id,target_message_id,model_alias,media_json,expires_at,permission_revision) VALUES(?,?,?,?,?,?,?,?)',
                               (token,owner,chat,target,alias,json.dumps(data,ensure_ascii=False),(now+timedelta(minutes=5)).isoformat(),permission['revision']))
        return token

    async def bind_trf_choice(self, tokens, menu_message_id):
        async with self.access_transaction() as conn:
            for token in tokens:
                await conn.execute('UPDATE trf_choices SET menu_message_id=? WHERE token=? AND menu_message_id IS NULL',(menu_message_id,token))

    async def consume_trf_choice(self, token, owner, chat, menu_message_id, *, at=None):
        now=aware(at or datetime.now(timezone.utc)).isoformat()
        async with self.access_transaction() as conn:
            row=await (await conn.execute('SELECT * FROM trf_choices WHERE token=?',(token,))).fetchone()
            if not row or row['used'] or row['expires_at']<=now or (row['owner_id'],row['chat_id'],row['menu_message_id'])!=(owner,chat,menu_message_id):return None
            if not await self.extended_audio_allowed(owner,conn=conn):return None
            permission=await (await conn.execute('SELECT revision FROM private_users WHERE user_id=?',(owner,))).fetchone()
            if permission[0]!=row['permission_revision']:return None
            await conn.execute('UPDATE trf_choices SET used=1 WHERE token=?',(token,))
            return row['model_alias'],row['target_message_id'],MediaRef(**json.loads(row['media_json']))
