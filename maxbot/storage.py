"""MAX-only persistent state. Never point this at the Telegram SQLite file."""
from __future__ import annotations

import json
import asyncio
from pathlib import Path

import aiosqlite


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS events (
  event_key TEXT PRIMARY KEY, payload TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending', error TEXT
);
CREATE TABLE IF NOT EXISTS groups (
  chat_id INTEGER PRIMARY KEY, title TEXT,
  state TEXT NOT NULL DEFAULT 'pending', remaining INTEGER NOT NULL DEFAULT 0,
  auto_enabled INTEGER NOT NULL DEFAULT 0, model TEXT
);
CREATE TABLE IF NOT EXISTS requests (
  chat_id INTEGER PRIMARY KEY, requester_id INTEGER,
  state TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS jobs (
  chat_id INTEGER NOT NULL, media_mid TEXT NOT NULL, model TEXT NOT NULL,
  state TEXT NOT NULL, transcript TEXT, error TEXT,
  PRIMARY KEY(chat_id, media_mid, model)
);
"""


class MaxStorage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.conn: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        await self.conn.executescript(SCHEMA)
        # A crash after an external call may have spent money. Never retry it blindly.
        await self.conn.execute("UPDATE events SET state='interrupted' WHERE state='running'")
        await self.conn.execute("UPDATE jobs SET state='interrupted' WHERE state='running'")
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if self.conn is None:
            raise RuntimeError("Storage is closed")
        return self.conn

    async def enqueue(self, key: str, payload: dict) -> bool:
        async with self.lock:
            cur = await self.db.execute(
                "INSERT OR IGNORE INTO events(event_key,payload) VALUES (?,?)",
                (key, json.dumps(payload, ensure_ascii=False)),
            )
            await self.db.commit()
            return cur.rowcount == 1

    async def next_event(self) -> tuple[str, dict] | None:
        async with self.lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cur = await self.db.execute(
                    "SELECT event_key,payload FROM events WHERE state='pending' ORDER BY rowid LIMIT 1"
                )
                row = await cur.fetchone()
                if row:
                    await self.db.execute("UPDATE events SET state='running' WHERE event_key=?", (row[0],))
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
        return (row[0], json.loads(row[1])) if row else None

    async def finish_event(self, key: str, error: str | None = None) -> None:
        await self.db.execute(
            "UPDATE events SET state=?,error=? WHERE event_key=?",
            ("error" if error else "done", (error or "")[:300], key),
        )
        await self.db.commit()

    async def ensure_group(self, chat_id: int, title: str | None = None) -> None:
        await self.db.execute(
            "INSERT INTO groups(chat_id,title) VALUES (?,?) ON CONFLICT(chat_id) "
            "DO UPDATE SET title=COALESCE(excluded.title,groups.title)", (chat_id, title)
        )
        await self.db.commit()

    async def group(self, chat_id: int) -> dict | None:
        cur = await self.db.execute(
            "SELECT state,remaining,auto_enabled,model,title FROM groups WHERE chat_id=?", (chat_id,)
        )
        row = await cur.fetchone()
        return dict(zip(("state", "remaining", "auto_enabled", "model", "title"), row)) if row else None

    async def request(self, chat_id: int, requester_id: int | None) -> bool:
        await self.ensure_group(chat_id)
        cur = await self.db.execute(
            "INSERT INTO requests(chat_id,requester_id,state) VALUES (?,?,'pending') "
            "ON CONFLICT(chat_id) DO UPDATE SET requester_id=excluded.requester_id,state='pending' "
            "WHERE requests.state!='pending'", (chat_id, requester_id)
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def pending(self) -> list[tuple[int, int | None]]:
        cur = await self.db.execute("SELECT chat_id,requester_id FROM requests WHERE state='pending' ORDER BY rowid LIMIT 50")
        return await cur.fetchall()

    async def decide(self, chat_id: int, decision: str) -> bool:
        states = {"once": ("once", 1), "always": ("approved", 0), "reject": ("rejected", 0)}
        if decision not in states:
            return False
        async with self.lock:
            return await self._decide_locked(chat_id, states[decision], decision)

    async def _decide_locked(self, chat_id: int, selected: tuple[str, int], decision: str) -> bool:
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            cur = await self.db.execute("SELECT state FROM requests WHERE chat_id=?", (chat_id,))
            row = await cur.fetchone()
            if not row or row[0] != "pending":
                await self.db.rollback()
                return False
            state, remaining = selected
            await self.db.execute(
                "UPDATE groups SET state=?,remaining=?,auto_enabled=0 WHERE chat_id=?",
                (state, remaining, chat_id)
            )
            await self.db.execute("UPDATE requests SET state=? WHERE chat_id=?", (decision, chat_id))
            await self.db.commit()
            return True
        except BaseException:
            await self.db.rollback()
            raise

    async def revoke(self, chat_id: int) -> None:
        await self.db.execute(
            "UPDATE groups SET state='revoked',remaining=0,auto_enabled=0 WHERE chat_id=?", (chat_id,)
        )
        await self.db.commit()

    async def set_auto(self, chat_id: int, enabled: bool) -> bool:
        cur = await self.db.execute(
            "UPDATE groups SET auto_enabled=? WHERE chat_id=? AND state='approved'",
            (int(enabled), chat_id)
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def set_model(self, chat_id: int, model: str) -> None:
        await self.db.execute("UPDATE groups SET model=? WHERE chat_id=?", (model, chat_id))
        await self.db.commit()

    async def cached(self, chat_id: int, mid: str, model: str) -> str | None:
        cur = await self.db.execute(
            "SELECT transcript FROM jobs WHERE chat_id=? AND media_mid=? AND model=? AND state='done'",
            (chat_id, mid, model)
        )
        row = await cur.fetchone()
        return row[0] if row else None

    async def reserve(self, chat_id: int, mid: str, model: str, private: bool) -> bool:
        """Reserve a paid job and (for one-time grants) its sole credit atomically."""
        async with self.lock:
            return await self._reserve_locked(chat_id, mid, model, private)

    async def _reserve_locked(self, chat_id: int, mid: str, model: str, private: bool) -> bool:
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            cur = await self.db.execute(
                "SELECT state,remaining FROM groups WHERE chat_id=?", (chat_id,)
            )
            group = await cur.fetchone()
            if not private and (not group or
                    (group[0] != "approved" and not (group[0] == "once" and group[1] > 0))):
                await self.db.rollback()
                return False
            cur = await self.db.execute(
                "INSERT OR IGNORE INTO jobs(chat_id,media_mid,model,state) VALUES (?,?,?,'running')",
                (chat_id, mid, model)
            )
            if cur.rowcount != 1:
                await self.db.rollback()
                return False
            if not private and group[0] == "once":
                await self.db.execute(
                    "UPDATE groups SET remaining=remaining-1,state='revoked' WHERE chat_id=?", (chat_id,)
                )
            await self.db.commit()
            return True
        except BaseException:
            await self.db.rollback()
            raise

    async def finish_job(self, chat_id: int, mid: str, model: str, text: str | None, error: str | None = None) -> None:
        await self.db.execute(
            "UPDATE jobs SET state=?,transcript=?,error=? WHERE chat_id=? AND media_mid=? AND model=?",
            ("error" if error else "done", text, (error or "")[:300], chat_id, mid, model)
        )
        await self.db.commit()
