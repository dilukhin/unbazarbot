from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import uuid

import aiosqlite


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AccessStore:
    """Операции доступа в отдельных транзакциях SQLite, включая старые кнопки."""

    async def init_access_schema(self):
        await self.db.create_function("casefold", 1, lambda value: str(value or "").casefold())
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS private_users(
              user_id INTEGER PRIMARY KEY,
              title TEXT,
              username TEXT,
              status TEXT NOT NULL DEFAULT 'pending',
              approved_by_user_id INTEGER,
              approved_at TEXT,
              revoked_by_user_id INTEGER,
              revoked_at TEXT,
              revision INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
        """)
        legacy_requests = False
        for table, column, definition in (
            ("groups", "revision", "INTEGER NOT NULL DEFAULT 0"),
            ("access_requests", "subject_type", "TEXT NOT NULL DEFAULT 'group'"),
        ):
            rows = await (await self.db.execute(f"PRAGMA table_info({table})")).fetchall()
            if column not in {row["name"] for row in rows}:
                legacy_requests = legacy_requests or table == "access_requests"
                await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        if legacy_requests:
            # Старые заявки не содержат поколения разрешения. Сохраняем только
            # заявки ожидающих групп; после прежнего разрешения или отзыва они устарели.
            await self.db.execute("""UPDATE access_requests SET status='superseded'
                WHERE status='pending' AND subject_type='group' AND chat_id IN
                (SELECT chat_id FROM groups WHERE status!='pending')""")
        await self.db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_private_pending
            ON access_requests(subject_type, chat_id) WHERE status='pending' AND subject_type='user'""")
        await self.db.commit()

    @asynccontextmanager
    async def access_transaction(self):
        # Другие операции над общей связью не могут случайно подтвердить эту транзакцию.
        async with aiosqlite.connect(self.path, timeout=10) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    @staticmethod
    def subject_table(kind):
        if kind not in {"user", "group"}:
            raise ValueError("Unknown access subject")
        return ("private_users", "user_id") if kind == "user" else ("groups", "chat_id")

    async def ensure_private_user(self, user_id, title, username):
        stamp = now()
        async with self.access_transaction() as conn:
            await conn.execute("""INSERT INTO private_users(user_id,title,username,created_at,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                title=excluded.title,username=excluded.username,updated_at=excluded.updated_at""",
                (user_id, title[:200], username, stamp, stamp))

    async def private_user_allowed(self, user_id):
        subject = await self.access_subject("user", user_id)
        return bool(subject and subject["status"] == "approved")

    async def access_subject(self, kind, target_id):
        table, key = self.subject_table(kind)
        row = await (await self.db.execute(f"SELECT * FROM {table} WHERE {key}=?", (target_id,))).fetchone()
        return dict(row) if row else None

    async def access_page(self, kind, page=0, search="", size=8):
        if kind == "requests":
            table, condition, params = "access_requests", "status='pending'", []
            fields = "COALESCE(chat_title,'') || ' ' || COALESCE(requester_username,'')"
        else:
            table, _ = self.subject_table(kind)
            condition, params = "1=1", []
            fields = "COALESCE(title,'') || ' ' || COALESCE(username,'')"
        if search:
            condition += f" AND instr(casefold({fields}), ?) > 0"
            params.append(search[:100].casefold().lstrip("@"))
        count = await (await self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE {condition}", params)).fetchone()
        total = count[0]
        page = max(0, min(page, max(0, (total - 1) // size)))
        rows = await (await self.db.execute(
            f"SELECT * FROM {table} WHERE {condition} ORDER BY updated_at DESC, rowid DESC LIMIT ? OFFSET ?",
            params + [size, page * size]
        )).fetchall()
        return [dict(row) for row in rows], total, page

    async def pending_access_request(self, kind, target_id):
        self.subject_table(kind)
        row = await (await self.db.execute("""SELECT * FROM access_requests
            WHERE subject_type=? AND chat_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1""",
            (kind, target_id))).fetchone()
        return self._request_from_row(row) if row else None

    async def create_access_request(self, kind, target_id, title, requester_id, username, reason, cooldown=300):
        table, key = self.subject_table(kind)
        async with self.access_transaction() as conn:
            subject = await (await conn.execute(f"SELECT status,revoked_at FROM {table} WHERE {key}=?", (target_id,))).fetchone()
            if not subject or subject["status"] in {"approved", "approved_once"}:
                return None
            row = await (await conn.execute("""SELECT * FROM access_requests
                WHERE subject_type=? AND chat_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1""",
                (kind, target_id))).fetchone()
            if row:
                return self._request_from_row(row)
            last = await (await conn.execute("""SELECT updated_at FROM access_requests
                WHERE subject_type=? AND chat_id=? ORDER BY updated_at DESC LIMIT 1""", (kind, target_id))).fetchone()
            recent = [stamp for stamp in (last["updated_at"] if last else None, subject["revoked_at"]) if stamp]
            if recent and (datetime.now(timezone.utc) - max(datetime.fromisoformat(stamp) for stamp in recent)).total_seconds() < max(0, cooldown):
                return None
            request_id, stamp = uuid.uuid4().hex[:12], now()
            await conn.execute("""INSERT INTO access_requests(id,chat_id,chat_title,requester_user_id,
                requester_username,reason,status,created_at,updated_at,subject_type)
                VALUES(?,?,?,?,?,?,'pending',?,?,?)""",
                (request_id, target_id, (title or "")[:200], requester_id, username, reason[:500], stamp, stamp, kind))
            row = await (await conn.execute("SELECT * FROM access_requests WHERE id=?", (request_id,))).fetchone()
            return self._request_from_row(row, is_new=True)

    async def _admin_in_transaction(self, conn, admin_id):
        row = await (await conn.execute("SELECT is_active FROM admins WHERE user_id=?", (admin_id,))).fetchone()
        return bool(row and row["is_active"])

    async def _set_access(self, conn, kind, target_id, decision, admin_id, jobs):
        table, key = self.subject_table(kind)
        stamp = now()
        status = {"approve_forever": "approved", "approve_once": "approved_once", "reject": "rejected", "revoke": "revoked"}[decision]
        if kind == "user" and decision == "approve_once":
            raise ValueError("Private access is persistent")
        grant = decision.startswith("approve")
        assignments = "status=?,revision=revision+1,updated_at=?"
        params = [status, stamp]
        if grant:
            assignments += ",approved_by_user_id=?,approved_at=?,revoked_by_user_id=NULL,revoked_at=NULL"
            params.extend([admin_id, stamp])
        else:
            assignments += ",revoked_by_user_id=?,revoked_at=?"
            params.extend([admin_id, stamp])
        if kind == "group":
            assignments += ",approval_type=?,remaining_jobs=?,auto_enabled=0"
            params.extend(["once" if decision == "approve_once" else "forever" if grant else None,
                           max(1, jobs) if decision == "approve_once" else 0])
        params.append(target_id)
        await conn.execute(f"UPDATE {table} SET {assignments} WHERE {key}=?", params)
        # Каждое изменение делает все старые уведомления этого адресата недействительными.
        await conn.execute("""UPDATE access_requests SET status='superseded',updated_at=?
            WHERE subject_type=? AND chat_id=? AND status='pending'""", (stamp, kind, target_id))
        await conn.execute("""INSERT INTO audit_log(actor_user_id,action,chat_id,target_id,details_json,created_at)
            VALUES(?,?,?,?,?,?)""", (admin_id, decision, target_id, str(target_id), json.dumps({"subject_type": kind}), stamp))

    async def decide_access_request(self, request_id, decision, admin_id, jobs=1, expected_kind=None):
        if decision not in {"approve_once", "approve_forever", "reject"}:
            raise ValueError("Unknown decision")
        async with self.access_transaction() as conn:
            if not await self._admin_in_transaction(conn, admin_id):
                return None
            row = await (await conn.execute("SELECT * FROM access_requests WHERE id=? AND status='pending'", (request_id,))).fetchone()
            if not row or (expected_kind and row["subject_type"] != expected_kind):
                return None
            if row["subject_type"] == "user" and (decision == "approve_once" or
                    await self._admin_in_transaction(conn, row["chat_id"])):
                return None
            await self._set_access(conn, row["subject_type"], row["chat_id"], decision, admin_id, jobs)
            await conn.execute("""UPDATE access_requests SET status=?,decided_by_user_id=?,decided_at=?,updated_at=?
                WHERE id=?""", (decision, admin_id, now(), now(), request_id))
            row = await (await conn.execute("SELECT * FROM access_requests WHERE id=?", (request_id,))).fetchone()
            return self._request_from_row(row)

    async def change_access(self, kind, target_id, decision, admin_id, expected_revision):
        if decision not in {"approve_forever", "revoke"}:
            raise ValueError("Unknown decision")
        table, key = self.subject_table(kind)
        async with self.access_transaction() as conn:
            if not await self._admin_in_transaction(conn, admin_id):
                return False
            row = await (await conn.execute(f"SELECT revision FROM {table} WHERE {key}=?", (target_id,))).fetchone()
            if not row or row["revision"] != expected_revision:
                return False
            if kind == "user" and await self._admin_in_transaction(conn, target_id):
                return False
            await self._set_access(conn, kind, target_id, decision, admin_id, 0)
            return True
