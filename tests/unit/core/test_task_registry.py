"""Failure diagnostics must be bounded and release completed task graphs."""
import asyncio
from contextvars import ContextVar, copy_context
import gc
import unittest
import weakref

from core import TaskFailureRecord, TaskRegistry


class TaskRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_is_bounded_and_does_not_retain_payloads_or_owners(self):
        class Owner:
            def __repr__(self):
                raise AssertionError("diagnostics must not inspect owner contents")
        class Payload:
            pass
        registry = TaskRegistry(failure_capacity=3)
        refs = []

        async def fail():
            payload = Payload()
            refs.append(weakref.ref(payload))
            raise ValueError("private payload")

        owner = Owner()
        owner_ref = weakref.ref(owner)
        for index in range(1000):
            task = registry.spawn(fail(), name=f"failure:{index}", owner=owner, critical=False)
            await asyncio.wait((task,))
        del task, owner
        await asyncio.sleep(0)
        gc.collect()
        self.assertEqual(registry.active_count, 0)
        self.assertEqual(registry.failure_count, 1000)
        self.assertEqual(registry.failures_dropped, 997)
        self.assertEqual([row.name for row in registry.failures], [f"failure:{i}" for i in range(997, 1000)])
        self.assertTrue(all(isinstance(row, TaskFailureRecord) for row in registry.failures))
        self.assertTrue(all(row.error_type == "builtins.ValueError" for row in registry.failures))
        self.assertTrue(all(ref() is None for ref in refs))
        self.assertIsNone(owner_ref())
        self.assertNotIn("private payload", repr(registry.failures))
        await registry.close()

    async def test_callback_receives_original_exception_and_info(self):
        registry = TaskRegistry()
        original = ValueError("original")
        owner = object()
        received = []
        registry.set_failure_handler(lambda info, error: received.append((info, error)))

        async def fail():
            raise original

        with self.assertLogs("core.TaskRegistry", level="ERROR"):
            task = registry.spawn(fail(), name="critical", owner=owner)
            await asyncio.wait((task,))
        self.assertIs(received[0][0].owner, owner)
        self.assertIs(received[0][1], original)
        self.assertIsNotNone(original.__traceback__)
        await registry.close()

    async def test_context_and_expected_cancellation(self):
        registry = TaskRegistry()
        marker = ContextVar("task-registry", default="host")
        context = copy_context()
        context.run(marker.set, "runtime")
        async def read():
            return marker.get()
        self.assertEqual(await registry.spawn(read(), name="context", owner="test", context=context), "runtime")
        self.assertEqual(marker.get(), "host")
        task = registry.spawn(asyncio.Event().wait(), name="cancel", owner="test")
        await registry.cancel_owner("test")
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())
        self.assertEqual(registry.failure_count, 0)
        await registry.close()

    async def test_failure_type_label_is_bounded_for_dynamic_exceptions(self):
        registry = TaskRegistry()
        error_type = type("Failure" + "x" * 4096, (Exception,), {"__module__": "dynamic"})

        async def fail():
            raise error_type("not part of the stored summary")

        task = registry.spawn(fail(), name="metadata", owner="test", critical=False)
        await asyncio.wait((task,))
        record = registry.failures[0]
        self.assertEqual(record.error_type, f"dynamic.{error_type.__qualname__}"[:256])
        self.assertNotIn("not part of the stored summary", repr(record))
        await registry.close()

    def test_invalid_capacity(self):
        for capacity in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                TaskRegistry(failure_capacity=capacity)
