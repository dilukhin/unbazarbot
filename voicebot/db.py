from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import uuid

import aiosqlite

from .access_store import AccessStore
from .paid_store import PaidStore


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class GroupAccess:
    chat_id: int
    title: str | None
    status: str
    approval_type: str | None
    default_model: str | None
    auto_enabled: bool
    remaining_jobs: int


@dataclass
class AccessRequest:
    id: str
    chat_id: int
    chat_title: str | None
    requester_user_id: int | None
    requester_username: str | None
    reason: str
    status: str
    created_at: str
    updated_at: str
    subject_type: str = "group"
    is_new: bool = False


class Database(AccessStore, PaidStore):
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.init_schema()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self.conn is None:
            raise RuntimeError("Database is not open")
        return self.conn

    async def init_schema(self) -> None:
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS admins(
              user_id INTEGER PRIMARY KEY,
              username TEXT,
              first_name TEXT,
              private_chat_id INTEGER,
              is_active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS groups(
              chat_id INTEGER PRIMARY KEY,
              title TEXT,
              username TEXT,
              status TEXT NOT NULL DEFAULT 'pending',
              approval_type TEXT,
              approved_by_user_id INTEGER,
              approved_at TEXT,
              revoked_by_user_id INTEGER,
              revoked_at TEXT,
              default_model TEXT,
              auto_enabled INTEGER NOT NULL DEFAULT 0,
              remaining_jobs INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS access_requests(
              id TEXT PRIMARY KEY,
              chat_id INTEGER NOT NULL,
              chat_title TEXT,
              requester_user_id INTEGER,
              requester_username TEXT,
              reason TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              decided_by_user_id INTEGER,
              decided_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_access_requests_chat_status
              ON access_requests(chat_id, status);

            CREATE TABLE IF NOT EXISTS transcription_jobs(
              id TEXT PRIMARY KEY,
              chat_id INTEGER,
              user_id INTEGER,
              message_id INTEGER,
              file_id TEXT,
              file_unique_id TEXT,
              model_alias TEXT,
              provider_model TEXT,
              status TEXT NOT NULL,
              transcript TEXT,
              cost REAL,
              duration_seconds REAL,
              error TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_cache
              ON transcription_jobs(file_unique_id, model_alias, status);

            CREATE TABLE IF NOT EXISTS audit_log(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              actor_user_id INTEGER,
              action TEXT NOT NULL,
              chat_id INTEGER,
              target_id TEXT,
              details_json TEXT,
              created_at TEXT NOT NULL
            );
            """
        )
        await self.db.commit()
        await self.init_access_schema()
        await self.init_paid_schema()

    async def upsert_config_admins(self, admin_user_ids: set[int]) -> None:
        now = utcnow()
        for user_id in admin_user_ids:
            await self.db.execute(
                """
                INSERT INTO admins(user_id, is_active, created_at, updated_at)
                VALUES(?, 1, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                  is_active=1,
                  updated_at=excluded.updated_at
                """,
                (user_id, now, now),
            )
        await self.db.commit()

    async def is_admin(self, user_id: int | None) -> bool:
        if not user_id:
            return False
        row = await (await self.db.execute(
            "SELECT is_active FROM admins WHERE user_id=?", (user_id,)
        )).fetchone()
        return bool(row and row["is_active"])

    async def register_admin_private_chat(
        self, user_id: int, private_chat_id: int, username: str | None, first_name: str | None
    ) -> None:
        now = utcnow()
        await self.db.execute(
            """
            INSERT INTO admins(user_id, username, first_name, private_chat_id, is_active, created_at, updated_at)
            VALUES(?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username,
              first_name=excluded.first_name,
              private_chat_id=excluded.private_chat_id,
              is_active=1,
              updated_at=excluded.updated_at
            """,
            (user_id, username, first_name, private_chat_id, now, now),
        )
        await self.db.commit()

    async def admin_private_chats(self) -> list[int]:
        rows = await (await self.db.execute(
            "SELECT private_chat_id FROM admins WHERE is_active=1 AND private_chat_id IS NOT NULL"
        )).fetchall()
        return [int(row["private_chat_id"]) for row in rows]

    async def ensure_group(
        self,
        chat_id: int,
        title: str | None,
        username: str | None = None,
        default_model: str | None = None,
    ) -> GroupAccess:
        now = utcnow()
        await self.db.execute(
            """
            INSERT INTO groups(chat_id, title, username, status, default_model, created_at, updated_at)
            VALUES(?, ?, ?, 'pending', ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
              title=COALESCE(excluded.title, groups.title),
              username=COALESCE(excluded.username, groups.username),
              default_model=COALESCE(groups.default_model, excluded.default_model),
              updated_at=excluded.updated_at
            """,
            (chat_id, title, username, default_model, now, now),
        )
        await self.db.commit()
        return await self.get_group(chat_id)  # type: ignore[return-value]

    async def get_group(self, chat_id: int) -> GroupAccess | None:
        row = await (await self.db.execute(
            "SELECT * FROM groups WHERE chat_id=?", (chat_id,)
        )).fetchone()
        if not row:
            return None
        return GroupAccess(
            chat_id=int(row["chat_id"]),
            title=row["title"],
            status=row["status"],
            approval_type=row["approval_type"],
            default_model=row["default_model"],
            auto_enabled=bool(row["auto_enabled"]),
            remaining_jobs=int(row["remaining_jobs"] or 0),
        )

    async def list_groups(self) -> list[GroupAccess]:
        rows = await (await self.db.execute(
            "SELECT * FROM groups ORDER BY updated_at DESC LIMIT 100"
        )).fetchall()
        return [
            GroupAccess(
                chat_id=int(row["chat_id"]),
                title=row["title"],
                status=row["status"],
                approval_type=row["approval_type"],
                default_model=row["default_model"],
                auto_enabled=bool(row["auto_enabled"]),
                remaining_jobs=int(row["remaining_jobs"] or 0),
            )
            for row in rows
        ]

    async def set_group_model(self, chat_id: int, model_alias: str) -> None:
        await self.db.execute(
            "UPDATE groups SET default_model=?, updated_at=? WHERE chat_id=?",
            (model_alias, utcnow(), chat_id),
        )
        await self.db.commit()

    async def set_group_auto(self, chat_id: int, enabled: bool) -> bool:
        async with self.access_transaction() as conn:
            cursor = await conn.execute(
                "UPDATE groups SET auto_enabled=?, updated_at=? WHERE chat_id=?" +
                (" AND status='approved'" if enabled else ""),
                (1 if enabled else 0, utcnow(), chat_id),
            )
            return bool(cursor.rowcount)

    async def consume_one_time_job_if_needed(self, chat_id: int) -> bool:
        async with self.access_transaction() as conn:
            row = await (await conn.execute("SELECT status,remaining_jobs FROM groups WHERE chat_id=?", (chat_id,))).fetchone()
            if not row:
                return False
            if row["status"] == "approved":
                return True
            if row["status"] != "approved_once" or row["remaining_jobs"] <= 0:
                return False
            remaining = row["remaining_jobs"] - 1
            await conn.execute(
                "UPDATE groups SET remaining_jobs=?,status=?,revision=revision+1,updated_at=? WHERE chat_id=?",
                (remaining, "revoked" if remaining == 0 else "approved_once", utcnow(), chat_id),
            )
            return True

    @staticmethod
    def _request_from_row(row, is_new=False) -> AccessRequest:
        return AccessRequest(
            id=row["id"], chat_id=int(row["chat_id"]), chat_title=row["chat_title"],
            requester_user_id=row["requester_user_id"], requester_username=row["requester_username"],
            reason=row["reason"], status=row["status"], created_at=row["created_at"],
            updated_at=row["updated_at"], subject_type=row["subject_type"], is_new=is_new,
        )

    async def create_or_get_pending_request(self, chat_id, chat_title, requester_user_id,
                                            requester_username, reason, cooldown=300):
        return await self.create_access_request("group", chat_id, chat_title, requester_user_id,
                                                requester_username, reason, cooldown)

    async def get_request(self, request_id):
        row = await (await self.db.execute("SELECT * FROM access_requests WHERE id=?", (request_id,))).fetchone()
        return self._request_from_row(row) if row else None

    async def list_pending_requests(self):
        rows, _, _ = await self.access_page("requests", size=50)
        return [self._request_from_row(row) for row in rows]

    async def decide_request(self, request_id, decision, admin_user_id, one_time_jobs):
        # Старые уведомления могут управлять только группами.
        return await self.decide_access_request(request_id, decision, admin_user_id, one_time_jobs,
                                                expected_kind="group")

    async def revoke_group(self, chat_id, admin_user_id):
        row = await self.access_subject("group", chat_id)
        return bool(row and await self.change_access("group", chat_id, "revoke", admin_user_id, row["revision"]))

    async def cached_transcript(self, file_unique_id: str, model_alias: str) -> dict[str, Any] | None:
        row = await (await self.db.execute(
            """
            SELECT * FROM transcription_jobs
            WHERE file_unique_id=? AND model_alias=? AND status='done' AND transcript IS NOT NULL
            ORDER BY created_at DESC LIMIT 1
            """,
            (file_unique_id, model_alias),
        )).fetchone()
        if not row:
            return None
        return dict(row)

    async def create_job(
        self,
        chat_id: int,
        user_id: int | None,
        message_id: int | None,
        file_id: str,
        file_unique_id: str,
        model_alias: str,
        provider_model: str,
    ) -> str:
        job_id = uuid.uuid4().hex[:12]
        now = utcnow()
        await self.db.execute(
            """
            INSERT INTO transcription_jobs(
              id, chat_id, user_id, message_id, file_id, file_unique_id, model_alias,
              provider_model, status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
            """,
            (job_id, chat_id, user_id, message_id, file_id, file_unique_id, model_alias, provider_model, now, now),
        )
        await self.db.commit()
        return job_id

    async def finish_job(
        self,
        job_id: str,
        transcript: str,
        cost: float | None,
        duration_seconds: float | None,
    ) -> None:
        await self.db.execute(
            """
            UPDATE transcription_jobs
            SET status='done', transcript=?, cost=?, duration_seconds=?, updated_at=?
            WHERE id=?
            """,
            (transcript, cost, duration_seconds, utcnow(), job_id),
        )
        await self.db.commit()

    async def fail_job(self, job_id: str, error: str) -> None:
        await self.db.execute(
            "UPDATE transcription_jobs SET status='error', error=?, updated_at=? WHERE id=?",
            (error[:1000], utcnow(), job_id),
        )
        await self.db.commit()

    async def audit(
        self,
        actor_user_id: int | None,
        action: str,
        chat_id: int | None,
        target_id: str | None,
        details: dict[str, Any],
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO audit_log(actor_user_id, action, chat_id, target_id, details_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (actor_user_id, action, chat_id, target_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )


