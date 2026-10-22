import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from core import APP_PATHS, Bot, OwnerInUseError
from hooks.extensions import install_extensions
from hooks.extensions.notes import NotesStore, STORE, StorageBusyError, StorageCapacityError
from shared.extensions import ExtensionsConfig, NotesConfig
from shared.paths import RuntimePaths
from adapters.onebot_fields import register_onebot_fields
from tests.unit.hooks.test_extensions import ReplyClient, envelope


WHO = ("test", "group", int, 1)


class NotesTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_write_failure_rolls_back_and_never_acknowledges_success(self):
        bot, client = Bot(), ReplyClient()
        self.addAsyncCleanup(bot.stop, "abort")
        bot.service(APP_PATHS, RuntimePaths(Path(self.temporary.name)))
        register_onebot_fields(bot, "test")
        await install_extensions(bot, ExtensionsConfig.parse({"enabled": ["commands", "notes"]}))
        await bot.start()
        store = bot.services.get(STORE)
        await bot.emit(envelope("/note set original", client))

        def reject_write(action, *args):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT else sqlite3.SQLITE_OK
        await store._call(lambda: store._connection.set_authorizer(reject_write))
        await bot.emit(envelope("/note set replacement", client))
        self.assertEqual(client.replies, ["保存成功", "操作失败：存储不可用或超出容量"])
        self.assertEqual(await store.get(WHO, "room"), "original")
        self.assertEqual(store.inflight, 0)
        await store._call(lambda: store._connection.set_authorizer(None))
        await bot.emit(envelope("/note set recovered", client))
        self.assertEqual(client.replies[-1], "保存成功")
        self.assertEqual(await store.get(WHO, "room"), "recovered")

    async def test_reply_failure_is_not_reported_as_a_storage_failure(self):
        bot = Bot()
        self.addAsyncCleanup(bot.stop, "abort")
        bot.service(APP_PATHS, RuntimePaths(Path(self.temporary.name)))
        register_onebot_fields(bot, "test")
        await install_extensions(bot, ExtensionsConfig.parse({"enabled": ["commands", "notes"]}))
        await bot.start()
        class FailingClient:
            calls = 0
            async def reply(self, ctx, text):
                self.calls += 1
                raise ValueError("reply transport failed")
        client = FailingClient()
        with self.assertRaises(Exception):
            await bot.emit(envelope("/note set saved", client))
        self.assertEqual(client.calls, 1)
        self.assertEqual(await bot.services.get(STORE).get(WHO, "room"), "saved")

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "storage"
        self.store = NotesStore(self.directory, NotesConfig(max_entries=2, max_bytes=16, max_inflight=1))
        self.addAsyncCleanup(self.store.stop, "abort")

    async def test_commit_restart_isolation_limits_and_delete(self):
        self.assertFalse(self.directory.exists())
        await self.store.setup(None)
        await self.store.set(WHO, "room", "hello")
        await self.store.set(("test", "group", str, "1"), "room", "string")
        with self.assertRaises(StorageCapacityError):
            await self.store.set(WHO, "other", "no")
        with self.assertRaises(StorageCapacityError):
            await self.store.set(WHO, "room", "中" * 6)
        self.assertIsNone(await self.store.get(("other", "group", int, 1), "room"))
        self.assertIsNone(await self.store.get(WHO, "other"))
        await self.store.stop()
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        restarted = NotesStore(self.directory, self.store.config)
        self.addAsyncCleanup(restarted.stop)
        await restarted.setup(None)
        self.assertEqual(await restarted.get(WHO, "room"), "hello")
        self.assertTrue(await restarted.delete(WHO, "room"))
        self.assertFalse(await restarted.delete(WHO, "room"))
        await restarted.set(WHO, "other", "accepted")

    async def test_cancel_keeps_charge_and_stop_waits_for_worker(self):
        await self.store.setup(None)
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        original = self.store._set
        def slow(*args):
            entered.set()
            gate.wait(2)
            return original(*args)
        self.store._set = slow
        call = asyncio.create_task(self.store.set(WHO, "room", "saved"))
        while not entered.is_set():
            await asyncio.sleep(.001)
        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call
        self.assertEqual(self.store.inflight, 1)
        with self.assertRaises(StorageBusyError):
            await self.store.get(WHO, "room")
        stop = asyncio.create_task(self.store.stop())
        await asyncio.sleep(.01)
        self.assertFalse(stop.done())
        gate.set()
        await asyncio.wait_for(stop, 2)
        self.assertEqual(self.store.inflight, 0)
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM notes").fetchone()[0], "saved")

    async def test_default_admission_counts_queued_operations_and_drains_all_64(self):
        self.store.config = NotesConfig()
        await self.store.setup(None)
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        original = self.store._set
        def slow(*args):
            entered.set()
            if not gate.wait(5):
                raise AssertionError("test did not release storage worker")
            return original(*args)
        self.store._set = slow
        calls = [asyncio.create_task(self.store.set(WHO, f"room-{index}", "saved")) for index in range(64)]
        try:
            async with asyncio.timeout(3):
                while self.store.inflight < 64 or not entered.is_set():
                    await asyncio.sleep(.001)
            with self.assertRaises(StorageBusyError):
                await asyncio.wait_for(self.store.set(WHO, "overflow", "rejected"), .5)
            stop = asyncio.create_task(self.store.stop())
            await asyncio.sleep(.01)
            self.assertFalse(stop.done())
        finally:
            gate.set()
        await asyncio.gather(*calls)
        await asyncio.wait_for(stop, 5)
        self.assertEqual(self.store.inflight, 0)
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 64)
            self.assertIsNone(connection.execute("SELECT value FROM notes WHERE session='overflow'").fetchone())

    async def test_cancelled_setup_cleans_its_thread(self):
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        original = self.store._open
        def slow():
            entered.set()
            gate.wait(2)
            original()
        self.store._open = slow
        setup = asyncio.create_task(self.store.setup(None))
        while not entered.is_set():
            await asyncio.sleep(.001)
        setup.cancel()
        gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await setup
        self.assertIsNone(self.store._executor)
        self.assertIsNone(self.store._connection)

    async def test_unknown_version_and_corruption_retain_original_bytes(self):
        self.directory.mkdir()
        with closing(sqlite3.connect(self.store.path)) as connection:
            connection.execute("PRAGMA user_version=999")
        before = self.store.path.read_bytes()
        with self.assertRaises(ValueError):
            await self.store.setup(None)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertIsNone(self.store._executor)
        self.store.path.write_bytes(b"corrupt database")
        store = NotesStore(self.directory, NotesConfig())
        with self.assertRaises(sqlite3.DatabaseError):
            await store.setup(None)
        self.assertEqual(store.path.read_bytes(), b"corrupt database")
        self.assertIsNone(store._executor)

    async def test_command_ack_after_commit_failure_and_owner_protection(self):
        bot, client = Bot(), ReplyClient()
        self.addAsyncCleanup(bot.stop, "abort")
        bot.service(APP_PATHS, RuntimePaths(Path(self.temporary.name)))
        register_onebot_fields(bot, "test")
        installed = await install_extensions(bot, ExtensionsConfig.parse({"enabled": ["commands", "notes"],
                                                                         "notes": {"max_bytes": 8}}))
        await bot.start()
        store = bot.services.get(STORE)
        await bot.emit(envelope("/note set saved", client))
        with closing(sqlite3.connect(store.path)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM notes").fetchone()[0], "saved")
        self.assertEqual(client.replies, ["保存成功"])
        await bot.emit(envelope("/note set too many bytes", client))
        self.assertTrue(client.replies[-1].startswith("操作失败"))
        with self.assertRaises(OwnerInUseError):
            await installed.scopes["notes"].unload()
        await installed.scopes["commands"].unload()
        await installed.scopes["notes"].unload()
        self.assertIsNone(store._executor)
