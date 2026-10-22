"""Commit-before-ack notes on one owned SQLite thread with bounded admission."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

from core import APP_PATHS, ServiceKey
from fields import MESSAGE_TYPE, USER_ID
from .model import CommandSpec, Contribution, identity


STORE = ServiceKey("business.notes.store")
COMMANDS = ServiceKey("business.notes.commands")
_COLUMNS = ("adapter", "scene", "user_type", "user_id", "session", "value")


class StorageBusyError(RuntimeError):
    pass


class StorageCapacityError(ValueError):
    pass


class NotesStore:
    def __init__(self, directory: Path, config):
        self.path = Path(directory) / "notes.sqlite3"
        self.config = config
        self._executor = None
        self._connection = None
        self._operations = set()
        self._close_future = None
        self._accepting = False
        self._entries = 0

    @property
    def inflight(self):
        return len(self._operations)

    async def setup(self, runtime):
        if self._executor is not None:
            raise RuntimeError("notes store is already set up")
        self._close_future = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tiffany-notes")
        try:
            future = self._schedule(self._open)
            await asyncio.shield(future)
            self._settled(future)
        except BaseException:
            # A cancelled caller cannot interrupt SQLite: settle the worker
            # before closing it, even when setup never returned to Runtime.
            await self.stop("abort")
            raise
        self._accepting = True

    def _schedule(self, function, *args):
        future = asyncio.wrap_future(self._executor.submit(function, *args))
        self._operations.add(future)
        future.add_done_callback(self._settled)
        return future

    def _settled(self, future):
        self._operations.discard(future)
        if not future.cancelled():
            future.exception()  # Observe failures even if the waiter cancelled.

    async def _call(self, function, *args):
        if not self._accepting or self.inflight >= self.config.max_inflight:
            raise StorageBusyError("notes store is stopped or full")
        # Shield preserves both the operation and its admission charge until
        # the actual thread operation completes, not until its waiter exits.
        future = self._schedule(function, *args)
        try:
            return await asyncio.shield(future)
        finally:
            if future.done():
                self._settled(future)

    async def stop(self, mode="drain"):
        self._accepting = False
        if self._executor is None:
            return
        if self._operations:
            await asyncio.shield(asyncio.gather(*tuple(self._operations), return_exceptions=True))
        if self._close_future is None:
            self._close_future = self._schedule(self._close)
        await asyncio.shield(self._close_future)
        self._settled(self._close_future)
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False)

    async def teardown(self):
        await self.stop("abort")

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=2.0)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if version not in (0, 1) or version == 0 and tables:
                raise ValueError("unsupported notes database version; original file retained")
            if version == 1:
                columns = connection.execute("PRAGMA table_info(notes)").fetchall()
                if tuple(row[1] for row in columns) != _COLUMNS or tuple(row[5] for row in columns) != (1, 2, 3, 4, 5, 0):
                    raise ValueError("invalid notes schema; original file retained")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            if version == 0:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("CREATE TABLE notes (adapter TEXT NOT NULL, scene TEXT NOT NULL, "
                                       "user_type TEXT NOT NULL, user_id TEXT NOT NULL, session TEXT NOT NULL, "
                                       "value TEXT NOT NULL, PRIMARY KEY(adapter,scene,user_type,user_id,session))")
                    connection.execute("PRAGMA user_version=1")
            self._entries = connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        except BaseException:
            connection.close()
            raise
        self._connection = connection

    def _close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @staticmethod
    def _key(who, session):
        if (not isinstance(who, tuple) or len(who) != 4 or who[2] not in (int, str)
                or type(who[3]) is not who[2] or not isinstance(session, str) or not session):
            raise ValueError("notes require a native user identity and stable session")
        return who[0], who[1], who[2].__name__, str(who[3]), session

    async def get(self, who, session):
        return await self._call(self._get, self._key(who, session))

    async def set(self, who, session, value):
        if not isinstance(value, str) or len(value.encode("utf-8")) > self.config.max_bytes:
            raise StorageCapacityError("note exceeds configured UTF-8 byte limit")
        return await self._call(self._set, self._key(who, session), value)

    async def delete(self, who, session):
        return await self._call(self._delete, self._key(who, session))

    def _get(self, key):
        row = self._connection.execute("SELECT value FROM notes WHERE adapter=? AND scene=? AND user_type=? AND user_id=? AND session=?", key).fetchone()
        return None if row is None else row[0]

    def _set(self, key, value):
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            exists = self._get(key) is not None
            if not exists and self._entries >= self.config.max_entries:
                raise StorageCapacityError("notes entry capacity reached")
            connection.execute("INSERT INTO notes VALUES (?,?,?,?,?,?) ON CONFLICT(adapter,scene,user_type,user_id,session) DO UPDATE SET value=excluded.value", (*key, value))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        if not exists:
            self._entries += 1

    def _delete(self, key):
        with self._connection:
            cursor = self._connection.execute("DELETE FROM notes WHERE adapter=? AND scene=? AND user_type=? AND user_id=? AND session=?", key)
        self._entries -= cursor.rowcount
        return bool(cursor.rowcount)


async def reply_note(ctx, command):
    who, session = identity(ctx), ctx.envelope.session_id
    if who is None or not session:
        await ctx.reply("操作失败：缺少用户或会话身份")
        return
    store = ctx.service(STORE)
    args = command.args
    if not args or args[0] not in ("set", "get", "delete") or (args[0] == "set" and len(args) < 2) or (args[0] != "set" and len(args) != 1):
        await ctx.reply("用法: note set <内容> | note get | note delete")
        return
    try:
        if args[0] == "set":
            await store.set(who, session, " ".join(args[1:]))
            response = "保存成功"
        elif args[0] == "get":
            value = await store.get(who, session)
            response = "暂无笔记" if value is None else value
        else:
            removed = await store.delete(who, session)
            response = "删除成功" if removed else "暂无笔记"
    except (sqlite3.Error, StorageBusyError, ValueError):
        response = "操作失败：存储不可用或超出容量"
    await ctx.reply(response)


def install(scope, config):
    paths = scope.bot.services.get(APP_PATHS)
    scope.service(STORE, NotesStore(paths.storage, config), dependencies=(APP_PATHS,))
    scope.service(COMMANDS, Contribution((CommandSpec("note", reply_note,
                  needs=(USER_ID, MESSAGE_TYPE), uses=(STORE,)),)), dependencies=(STORE,))
    return COMMANDS
